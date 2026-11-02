"""The orchestrator: one Run per challenge, one long-lived main-agent session per
Run, and a bounded pool of worker sessions.

Lifecycle of a run:

    queued -> running -> (solved | failed | needs_input | stopped)

Inside a running run:

    main agent (conversation + dispatch)
        |-- dispatch_task(role, objective, context)
        |       `-> worker session (cwd = workspace/work)
        |              writes facts / flags through structured tools
        |-- wait_tasks(ids)          <- blocks the tool call, not the event loop
        |-- propose_flag(...)        <- automatically dispatches an independent verifier
        `-- finish_run(status, ...)  <- the run stops accepting work

Concurrency is bounded twice: ``limits.workers_per_run`` per run and
``limits.max_sessions`` globally (every session is a ``claude`` subprocess).
A run that exceeds its wall clock or dollar budget is stopped the same way an
operator stop is: workers are cancelled, and the run closes with an honest
summary.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from . import caselib, notes, patrol, prompts, re_tools, swarm, textmatch, worker_tools
from .backend import (
    AgentBackend,
    AgentEvent,
    SessionSpec,
    auth_error_message,
    cli_missing_message,
    gateway_error,
    spawn_error_message,
)
from .challenge import challenge_block, no_proxy_value
from .config import AppConfig
from .db import Store
from .events import EventBus
from .guard import build_hooks
from .models import (
    BASIS_LABEL,
    BASIS_VALUES,
    CLOSING_ROLES,
    EVIDENCE_ACTION_KEYS,
    EVIDENCE_RECORD_KEY,
    EVIDENCE_RESULT_KEYS,
    NEGATIVE_GAP_KEY,
    RE_CATEGORIES,
    RE_ROLES,
    ROLES,
    ROLE_SKILL,
    Challenge,
    Fact,
    FactKind,
    FlagStatus,
    RunStatus,
    TaskStatus,
    VERDICTS,
    grounding_gap,
    negative_gap,
)
from .tools import TOOL_NAMES as ORCHESTRATOR_TOOLS
from .tools import OrchestratorTools, build_sdk_mcp_server
from .workspace import SandboxStatus, Workspace

#: Wake-up sentinel for the main loop's chat queue. Distinct from ``None`` (stop).
_WAKE = object()

#: The token counters a price table bills on.
#:
#: A result event's ``usage`` is **this call's** tokens, not the session's
#: cumulative total — the opposite of ``result.total_cost_usd``, which *is*
#: cumulative. Verified on run 9: four lead results carried 22,773 / 22,739 /
#: 7,439 / 1,727 input tokens, a sequence that is not monotonic and so cannot be
#: cumulative, and their sum matched the session's final ``cost-state``
#: ``modelUsage`` to the token. Resuming does not re-report the inherited
#: transcript either (task 137 -> 140: 887,940 + 659,955 = 1,547,895 against a
#: final 1,589,273).
#:
#: So usage is additive and needs no baseline. The earlier code carried one
#: anyway, on the assumption that usage was cumulative like the cost figure, and
#: booked ``max(0, this - previous)`` — which silently dropped most of a
#: multi-turn lead's spend whenever a turn read back less than the one before.
_USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "output_tokens")


def _usage_totals(usage: Any) -> dict[str, int] | None:
    """The billing-relevant counters, or ``None`` when the event carries none."""
    if not isinstance(usage, dict):
        return None
    totals = {k: int(usage.get(k) or 0) for k in _USAGE_KEYS}
    return totals if any(totals.values()) else None


#: Share of the new text's tokens an earlier fact must already contain.
_RESTATE_COVERAGE = 0.60
#: How much a record's ``basis`` is worth. A restatement that *upgrades* the
#: grounding — "I inferred the key length" later checked by running it — is not
#: a restatement: dropping it would leave the board saying "inferred" forever.
_BASIS_RANK = {"": 0, "inferred": 1, "external": 2, "derived": 3, "observed": 4}

#: Statuses that end a background task. ``task_notification`` only ever sends
#: the first three; ``task_updated`` sends the raw status, which also includes
#: ``killed``. ``pending``/``running``/``paused`` are not terminal.
_BACKGROUND_DONE = {"completed", "failed", "stopped", "killed"}


def _fact_tokens(text: str) -> set[str]:
    return textmatch.tokens(text)


def _fact_hard_tokens(text: str) -> set[str]:
    return textmatch.hard_tokens(text)


#: How much of a quoted command or output the digest carries. Enough to re-run
#: the step and to recognise a copy-pasted value; the full record stays in the
#: UI and the database.
_EVIDENCE_EXCERPT = 220
#: Total characters of evidence the digest carries. The digest is injected into
#: every worker prompt on every dispatch, so it cannot grow with the board: the
#: newest grounded facts are the ones a worker is about to build on, and the
#: rest stay in the UI and the database.
_EVIDENCE_BUDGET = 2400


def _clip(text: str, limit: int = _EVIDENCE_EXCERPT) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


#: How a verifier's conclusion is written when it is not passed as a structured
#: field. Deliberately tolerant: the strict ``VERDICT: x`` form was missed by 40%
#: of verifiers, which then left a flag in ``candidate`` with no automated
#: decision attached to it.
_VERDICT_RE = re.compile(
    r"(?:VERDICT|结论|判定)\**\s*[:：=]\s*\**\s*"
    r"(reproduced|refuted|inconclusive|复现成功|复现|驳回|推翻|无法确定|不确定)",
    re.IGNORECASE,
)

_VERDICT_WORDS = {
    "reproduced": "reproduced",
    "复现成功": "reproduced",
    "复现": "reproduced",
    "refuted": "refuted",
    "驳回": "refuted",
    "推翻": "refuted",
    "inconclusive": "inconclusive",
    "无法确定": "inconclusive",
    "不确定": "inconclusive",
}

#: The direction roles a triage conclusion may pick. Anything else (a typo, a
#: category this stack has no worker for) is discarded rather than written to
#: the challenge row, where it would silently break ``caselib``'s matching.
_DIRECTIONS = tuple(r for r in ROLES if r not in ("triage", "verifier", "writer"))

#: Turns a worker must have taken before its role is evidence of a challenge's
#: direction. A task cancelled by the close on its first turn is the close's
#: doing, not a judgement about the challenge; filing the card under it would
#: penalise the next challenge that shares that direction.
MIN_DIRECTION_TURNS = 10

#: A run that has emitted no event of any kind for this long is a zombie —
#: every live session produces events constantly, so the silence itself is
#: the diagnosis. Longer than the lead's own stall clock (900 s) because the
#: reaper is the backstop for the case where those clocks did not fire.
_ZOMBIE_SILENCE_S = 1200

#: The closing turn after a time ceiling goes through the possibly-hung lead
#: client; it gets this long before the close proceeds without it.
_CEILING_SUMMARY_TIMEOUT_S = 240

#: How often the board coordinator looks for a startable direction. Fast enough
#: that a posted hypothesis is not measurably slower than ``dispatch_task``,
#: slow enough that a whole bench run costs a few hundred cheap SQL reads.
_BOARD_TICK_S = 2.0

#: How long the coordinator leaves a family alone after a dispatch for it was
#: refused. The refusal reasons (out of budget, endpoint rejecting sessions, no
#: session slot) are minute-scale, not tick-scale; without this the tick loop
#: re-emits the same refusal event every two seconds for the rest of the run,
#: and the operator reads a wall of the same error as a new failure each time.
_BOARD_REFUSAL_BACKOFF_S = 60.0

#: How many settled directions the board digest keeps showing. Enough to cover
#: the directions a run typically retires, small enough that a long run's digest
#: does not turn into a graveyard the lead has to read past every turn.
CLOSED_DIRECTIONS_LIMIT = 12

#: How triage states its conclusion. The prompt asks for ``category=<cat>`` as
#: the first finding; live runs have also written ``category=crypto：…`` (CJK
#: punctuation straight after the token) and put the answer only in the Chinese
#: summary. Findings are the prescribed channel and reach the blackboard, so
#: they are scanned before the summary prose.
_CATEGORY_PATTERNS = (
    re.compile(r"(?:^|\n)\s*\**category\**\s*[:=]\s*([a-z_\-]+)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*分类\s*[:：]\s*([a-z_\-]+)", re.IGNORECASE),
)


def _triage_category(findings: list[str], summary: str) -> str:
    for text in [*findings, summary]:
        for pattern in _CATEGORY_PATTERNS:
            for match in pattern.finditer(text):
                candidate = match.group(1).lower().replace("-", "_")
                if candidate in _DIRECTIONS:
                    return candidate
    return ""


#: A pasted challenge's first line is usually a URL followed by the whole task
#: prose, and that blob becomes ``challenge.name`` — the run title, the rail
#: row and the browser tab all render it. The normalizer rebuilds it as
#: 「方向 · 主机 · 短语」 from the parts the run already knows.
_URLISH = re.compile(r"https?://\S*")
# A host has a dot in the name or an explicit port — otherwise every English
# word in a pasted prose ("pwn", "web") would match as one.
_HOSTPORT = re.compile(
    r"(?:https?://)?([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)+(?::\d{1,5})?"
    r"|(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?"
    r"|[A-Za-z0-9.-]+:\d{1,5})"
)
_NC_TARGET = re.compile(r"(?:nc|ncat)\s+(?:--?\w+\s+)*([A-Za-z0-9.-]+)\s+(\d{1,5})")
_PHRASE_SPLIT = re.compile(r"[，。！？：:；;（）()\[\]【】\n\r]")
#: A name at or under this length is already human ("easy-rev", "古典密码") and
#: never gets normalized; the shape guard lives in SQL too, but the cheap
#: check here keeps the composer from running at all.
_BLOB_NAME_CHARS = 40
_PHRASE_CHARS = 16
_TITLE_CHARS = 46


def _auto_title(category: str, name: str, description: str, remote: str) -> str:
    """「方向 · 主机 · 短语」 for a challenge whose title is a pasted blob.

    The host comes from the title first, then the prose, then a ``nc host
    port`` remote. The phrase is the first punctuation-free segment of the
    prose (the title minus its URL as a fallback); filler like "一道..." is
    accepted as-is because dropping it would need judgement the platform
    does not have. Empty parts are simply omitted.
    """
    for source in (name, description):
        match = _HOSTPORT.search(source or "")
        if match:
            host = match.group(1)
            break
    else:
        match = _NC_TARGET.search(remote or "")
        host = f"{match.group(1)}:{match.group(2)}" if match else ""
    # The phrase is the first punctuation-free segment of the prose, the
    # title-minus-URL as a fallback. It gets whatever the title budget has
    # left after the direction and the host — a phrase cut mid-way by a hard
    # cap reads worse than a short one, and below 4 characters it reads as
    # noise, so it is dropped entirely.
    segment = ""
    for source in (description, _URLISH.sub(" ", name or "")):
        candidate = _PHRASE_SPLIT.split(_URLISH.sub(" ", source or "").strip(), 1)[0].strip()
        if len(candidate) >= 4:
            segment = candidate
            break
    parts = [category] + ([host] if host else [])
    base = " · ".join(parts) + " · "
    budget = _TITLE_CHARS - len(base)
    if segment and budget >= 4:
        parts.append(segment[:min(_PHRASE_CHARS, budget)])
    return " · ".join(parts)[:_TITLE_CHARS]


async def _close_stream(stream: Any, timeout: float = 5.0) -> None:
    """Close a session stream whose model call went silent.

    Abandoning the async generator only queues its cleanup for the garbage
    collector, and until then the `claude` subprocess behind it is still alive.
    A resumed attempt has to open a new session, so the old one is closed here
    rather than left to timing. The timeout matters: closing a generator that is
    suspended inside a hung call can itself hang.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await asyncio.wait_for(aclose(), timeout=timeout)
    except Exception:  # noqa: BLE001 - the stream is already dead either way
        pass


def _evidence_suffix(fact: Fact) -> str:
    """The action and raw result a fact rests on, for its digest line.

    A blackboard entry that reads as checked but does not say what was run is
    exactly what a later worker cannot verify, and in practice the next worker
    either re-runs it or, worse, builds on it. The prompts ask for the evidence;
    this puts it where the next worker reads the claim.
    """
    if fact.basis not in ("observed", "derived"):
        gap = str((fact.evidence or {}).get("grounding_gap") or "")
        if not gap:
            return ""
        declared = BASIS_LABEL.get(str(fact.evidence.get("declared_basis") or ""), "")
        return f"\n  原标「{declared}」已降为推测（{gap}）"
    ev = fact.evidence or {}
    action = next(
        (str(ev[k]).strip() for k in EVIDENCE_ACTION_KEYS if str(ev.get(k) or "").strip()), ""
    )
    result = next(
        (str(ev[k]).strip() for k in EVIDENCE_RESULT_KEYS if str(ev.get(k) or "").strip()), ""
    )
    parts = []
    if action:
        parts.append(f"证据·命令: {_clip(action)}")
    if result:
        parts.append(f"证据·输出: {_clip(result)}")
    record = str(ev.get(EVIDENCE_RECORD_KEY) or "").strip()
    if record:
        parts.append(f"证据·记录: {record}（re_evidence_get 可取回）")
    return f"\n  {' | '.join(parts)}" if parts else ""


#: Words a report uses when it is naming a value it *ruled out*. A worker quotes
#: decoys and wrong answers deliberately; the same strings must not become
#: candidates just because they match the flag regex.
_NEGATIVE_MARKERS = (
    "decoy", "诱饵", "wrong", "错误", "不是", "not this", "not the flag", "near-miss",
    "近似", "少一个", "多一个", "fake", "假", "placeholder", "示例", "example", "refuted",
    "推翻", "negative", "对照", "无效", "invalid", "incorrect",
)


def _looks_like_negative(window: str) -> bool:
    lowered = window.lower()
    return any(marker in lowered for marker in _NEGATIVE_MARKERS)


class SessionPool:
    """Global cap on concurrent Claude sessions."""

    def __init__(self, size: int) -> None:
        self._sem = asyncio.Semaphore(max(1, size))
        self.active = 0

    async def __aenter__(self) -> "SessionPool":
        await self._sem.acquire()
        self.active += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.active -= 1
        self._sem.release()


@dataclass
class RunContext:
    """Everything a Run needs that is not derived from it."""

    store: Store
    bus: EventBus
    cfg: AppConfig
    backend: AgentBackend
    pool: SessionPool
    workspaces_root: Path
    skills_dir: Path
    sandbox: SandboxStatus
    #: Directories no session may read or search. The benchmark sets this to its
    #: own folder: the manifest holds the expected answer there, and a worker
    #: that grepped the host for the challenge name was handed it.
    sealed_roots: list[Path] = field(default_factory=list)


class Run:
    def __init__(self, ctx: RunContext, run_id: int, challenge: Any, *, adopt: bool = False) -> None:
        self.ctx = ctx
        self.id = run_id
        self.challenge = challenge
        self.store = ctx.store
        self.bus = ctx.bus
        self.cfg = ctx.cfg
        #: Skill directories this run's sessions can see, as installed into the
        #: workspace (not what the library offered). Filled below for a new and
        #: an adopted run alike.
        self._skills: list[str] = []
        if adopt:
            # Re-attaching to a run whose workspace already exists (a second
            # process confirming a flag). Copying the challenge again would
            # overwrite what the workers produced.
            self.workspace = Workspace.existing(ctx.workspaces_root, run_id, challenge.name)
        else:
            # A challenge typed into the web UI has no source directory.
            # `Path("")` is `Path(".")`, which would silently copy the whole
            # server working directory into the run, so the empty case must be
            # an explicit None.
            source = Path(challenge.source_dir) if challenge.source_dir else None
            self.workspace = Workspace.create(
                ctx.workspaces_root, run_id, challenge.name, source
            )
            self.store.set_run_workspace(run_id, str(self.workspace.root))
        # Skills live at <cwd>/.claude/skills because the sessions run with
        # cwd=work and setting_sources=["project"]. Run this for an adopted
        # workspace too: if the library was restored while the run was sitting
        # in the DB, the re-attached writers should get the skills as well.
        self._skills = self.workspace.install_skills(ctx.skills_dir)
        if not self._skills:
            # Zero skills is a run that silently loses the technique index:
            # the sessions still start, and nobody notices until a worker
            # improvises a technique the skill library already documents.
            # The vendored tree was emptied once (11 dirs, 0 files) and every
            # run after it installed nothing while `doctor` said 已找到.
            self._emit(
                "run.warning",
                {
                    "warning": (
                        f"技能库为空：{ctx.skills_dir} 下没有含 SKILL.md 的目录，"
                        "本次运行的会话不会有任何技能。"
                    ),
                    "skills_dir": str(ctx.skills_dir),
                },
            )
        else:
            # "The library has files" is not the same as "every role's skill is
            # here". Each worker is allowed exactly one skill (ROLE_SKILL), so a
            # single missing directory costs that whole direction its technique
            # index without any other sign: the session starts, the allowlist
            # names a skill that does not exist, and the model just works blind.
            missing = self.workspace.missing_skills(ROLE_SKILL.values())
            if missing:
                self._emit(
                    "run.warning",
                    {
                        "warning": (
                            "角色技能缺失：" + "、".join(missing)
                            + " 没有装进工作区，对应 worker 会在没有技能的情况下启动。"
                        ),
                        "missing": missing,
                        "skills": self._skills,
                    },
                )

        self.tools = OrchestratorTools(self)
        self.tools.question_handler = self._ask_human

        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._done: dict[int, asyncio.Event] = {}
        # Restatement check runs on every fact write; re-reading and
        # re-tokenising the whole blackboard each time is O(n^2) over a run.
        self._fact_index: list[tuple[Fact, set[str], set[str]]] | None = None
        self._results: dict[int, dict[str, Any]] = {}
        #: task id -> live progress counters, so the lead can tell "still
        #: working" from "dead" while a task runs. Observed live: the lead saw
        #: ``cost_usd 0.0`` for every running task and dispatched a second
        #: reverse worker as a hedge, paying twice for one direction.
        self._live: dict[int, dict[str, Any]] = {}
        #: ``(when, model, message)`` for sessions the endpoint refused with its
        #: own error (503 / model_not_found / auth). Two inside
        #: ``gateway_outage``'s window stop new dispatches: retrying a model the
        #: endpoint does not serve is the same certain failure, paid again. Six
        #: tasks in runs 83/84 died that way, three minutes each.
        self._gateway_failures: list[tuple[float, str, str]] = []
        #: Models already warned about, so the event rail says it once.
        self._gateway_warned: set[str] = set()
        #: ``(when, hint)`` for sessions the *machine* refused — a missing CLI
        #: binary, a spawn failure. Same rule as the endpoint: two in a row and
        #: further dispatches are refused, because each one dies in a second
        #: and the run pays for the discovery either way.
        self._infra_failures: list[tuple[float, str]] = []
        self._infra_warned: set[str] = set()
        #: task id -> what the lead saw on its previous ``wait_tasks``. ``tools``
        #: alone cannot answer "is it still working": the lead wakes every few
        #: minutes and reads the same number twice, which looks identical to a
        #: frozen worker. The delta against this snapshot is the answer.
        self._lead_seen: dict[int, dict[str, Any]] = {}
        #: task id -> its tool-call count when it last recorded a fact. The gap
        #: is what `limits.no_progress_tools` measures, when that opt-in guard is
        #: enabled. Off by default: distinct work is not waste.
        self._tools_at_fact: dict[int, int] = {}
        #: task id -> the last tool call's fingerprint, and how many times in a
        #: row it has repeated. This is the guard that stays on: issuing the same
        #: command over and over is waste no matter how deep the analysis is.
        self._last_call: dict[int, tuple[str, str]] = {}
        self._repeats: dict[int, int] = {}
        #: Run-level progress: one trace per finished task, plus the per-task set
        #: of distinct tool-result digests. A task that adds neither a fact nor a
        #: new observation moved nothing, however many turns it burned; the
        #: task-level guards cannot see that, because they die with the task.
        self._traces: list[patrol.TaskTrace] = []
        self._outputs: dict[int, set[int]] = {}
        self._patrol = patrol.PatrolState()
        self._patrol_tasks: set[int] = set()
        #: Board-driven dispatch (蜂群 B). ``_board_task`` is the coordinator's
        #: asyncio task. ``_board_starts`` counts how many times the board has
        #: started a worker for each family — used only to cap retries of a
        #: refuted direction, never to decide whether a family is busy (that is
        #: read from live tasks). Empty when ``limits.swarm == "off"``, which is
        #: the default and the old shape.
        self._board_task: asyncio.Task[None] | None = None
        self._board_starts: dict[str, int] = {}
        #: family -> monotonic deadline before which the coordinator will not try
        #: that family again. A refused dispatch (budget, endpoint outage, no
        #: session slot) does not heal inside one tick, and the coordinator
        #: ticks every two seconds: without this, a run that is out of money
        #: writes a refusal event every two seconds for as long as it is alive.
        self._board_backoff: dict[str, float] = {}
        #: One outstanding "the board started work" message at a time, plus the
        #: ids it covered so a later batch can be folded into it.
        self._board_wake_pending = False
        self._board_wake_ids: list[int] = []
        #: The exact text of that outstanding message. The latch is cleared by
        #: the queue handing it to the lead, not by a prefix match — a chat line
        #: that quoted it could otherwise release the latch unread.
        self._board_wake_text: str | None = None
        #: Whether the current empty-board stretch has already been announced.
        #: One ``board.idle`` per stretch, not one per tick.
        self._board_idle_announced = False
        self._verifier_flags: dict[int, int] = {}
        self._pending_verifiers: dict[int, int] = {}
        #: task id -> the ceiling its session was started with. Budget has to be
        #: reserved at dispatch, not only spent: three workers dispatched in the
        #: same tick each read ``run_budget - cost_usd`` as the whole remainder
        #: and took a full ``role_budget_usd`` apiece. Measured on run 18 —
        #: 3 x $8 + 2 x $6 = $36 of ceilings against a $30 run budget, which
        #: closed at $35.78 having overshot by the last worker's whole spend.
        self._committed: dict[int, float] = {}
        #: task id -> the cumulative spend the CLI last reported for that
        #: session. ``total_cost_usd`` is cumulative *including* the transcript a
        #: resumed session was handed, so the run is charged the delta only.
        self._worker_base: dict[int, float] = {}
        #: Budget granted outside the run's own ceiling, for a session the
        #: operator asked for after the run closed (the on-demand writeup). A
        #: run that ended at its dollar ceiling would otherwise hand that writer
        #: a zero ceiling, and it would never start.
        self._extra_budget: float = 0.0
        #: The board's reservation ledger (``swarm.Budget``). Exists so the 15%
        #: kept for the closing roles cannot be spent by an exploring worker
        #: that the board started on its own; the run pool's ledger
        #: (``_committed``) remains the authority on everything else. Created
        #: only in board mode — an off-mode run must not grow a second ledger.
        self._board_budget = (
            swarm.Budget(ceiling=float(self.cfg.limits.run_budget_usd))
            if self.swarm_enabled
            else None
        )
        #: task id -> (session id, what that transcript already cost). Set by
        #: ``dispatch(resume_task_id=...)``, consumed by ``_run_task``.
        self._resume: dict[int, tuple[str, float]] = {}
        #: Task ids that already received their post-ceiling wrap-up turn.
        self._wrapped: set[int] = set()
        #: Task ids allowed to run after the run stopped accepting work (the
        #: writeup, dispatched when the operator confirms a flag post-close).
        self._post_stop_ok: set[int] = set()
        self._worker_slots = asyncio.Semaphore(max(1, ctx.cfg.limits.workers_per_run))
        #: Rendered case-library block; ``""`` once computed as empty. Lazily
        #: built by ``_case_block`` because the challenge never changes mid-run.
        self._case_block_cache: str | None = None
        #: Note files this run's dispatch index listed, so the close can charge
        #: an outcome to the ones the run actually applied (see ``notes.balance``).
        self._shown_notes: list[Path] = []
        self._stop = asyncio.Event()
        self._accepting = True
        #: The run is close enough to its dollar ceiling that opening new ground
        #: is no longer worth it, but closing roles (verifier, writer) still are.
        self._soft_stopped = False
        self._finish_status = ""
        self._finish_summary = ""
        self._main_client: Any = None
        self._main_task: asyncio.Task[None] | None = None
        #: The lead's session id and the cumulative spend the CLI reports for
        #: it. Seeded from the run row when re-attaching after a restart, so a
        #: resumed session is charged only for what it adds.
        row = self.store.get_run(self.id)
        self._main_session = row.main_session_id if row is not None else ""
        self._main_cost = row.main_cost_usd if row is not None else 0.0
        self.cost_usd = row.cost_usd if row is not None else 0.0
        #: Set once the lead's session is open (or has definitively failed to
        #: open). A reopened run answers chat only after this, so a message is
        #: never accepted by a loop that is about to die on a bad resume.
        self._session_ready = asyncio.Event()
        self._session_error = ""
        #: The CLI reported an authentication failure. Sticky for the life of
        #: the loop: credentials do not change while the process runs, so every
        #: later turn would fail the same way.
        self._auth_failed = False
        self._watchdog: asyncio.Task[None] | None = None
        self._chat_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._question_future: asyncio.Future[str] | None = None
        self._question_text = ""
        self._started = time.time()
        #: Wall-clock timestamp of the last event this run emitted, any kind.
        #: The watchdog's silence check reads it: a run that has produced
        #: nothing for many minutes is a zombie even when its own task tree
        #: believes it is alive.
        self._last_event_ts = time.time()
        self._closed = False
        #: Text already streamed in the current turn, so the final ``result``
        #: message is not appended twice and the UI can tell a streamed reply
        #: from one that arrived whole.
        self._stream_buf = ""
        #: Whether this turn produced a text block. The turn's ``result`` is the
        #: fallback only when it did not.
        self._turn_had_text = False
        #: True while a lead turn is streaming. An operator message that lands
        #: during one has to cut it short, or it sits in the queue behind a
        #: tool call that can run for minutes.
        self._lead_turn_active = False
        #: The lead's turn number, incremented as each turn starts, and the turn
        #: an operator message cut short. Matching numbers at the turn's result
        #: is what tells "the CLI aborted because I asked" from "the session
        #: died on its own" — both arrive as ``error_during_execution``.
        self._lead_turn = 0
        self._interrupt_target = -1
        #: The control request that cut the current lead turn, if any. Held so
        #: the loop can cancel it instead of leaving it pending on a dead
        #: session.
        self._interrupt_task: asyncio.Task[None] | None = None
        #: Whether the lead's last turn ended because of that interrupt. Read by
        #: ``_session_spent`` before the subtype check.
        self._lead_aborted = False
        #: Whether the lead's last turn produced no event at all before it was
        #: cut. An interrupt that lands on a turn the lead never got to read is
        #: re-queued rather than treated as answered.
        self._lead_turn_empty = False
        self._operator_closing = False
        self.turns = 0

    # ------------------------------------------------------------------ state
    @property
    def accepting_work(self) -> bool:
        return self._accepting and not self._stop.is_set()

    @property
    def accepting_exploration(self) -> bool:
        """Whether the run may still open new ground.

        False once the dollar budget is close to spent. Sessions already running
        are left to land, and the closing roles stay available: a run that has a
        flag candidate but no money to verify it has wasted everything it spent.
        """
        return self.accepting_work and not self._soft_stopped

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def elapsed(self) -> float:
        return time.time() - self._started

    def clock(self) -> dict[str, Any]:
        """Where the run stands against its two ceilings.

        The lead has no clock and no ledger of its own: it saw ``$0/$6`` — its
        own session's budget — and read it as the run's, and it guessed
        "~1200s used" when 2100s had passed. Both errors push it to wait longer
        than it can afford. Every tool that returns to the lead carries this.
        """
        limits = self.cfg.limits
        elapsed = int(self.elapsed)
        remaining = int(limits.run_timeout_s - elapsed)
        return {
            "elapsed_s": elapsed,
            "remaining_s": remaining,
            "run_cost_usd": round(self.cost_usd, 2),
            "run_budget_usd": limits.run_budget_usd,
            "budget_left_usd": round(limits.run_budget_usd - self.cost_usd, 2),
            "workers_running": sum(
                1
                for tid, t in self._tasks.items()
                if not t.done() and tid not in self._post_stop_ok
            ),
            "workers_allowed": limits.workers_per_run,
        }

    def _emit(self, kind: str, payload: dict[str, Any], task_id: int | None = None) -> None:
        self._last_event_ts = time.time()
        self.bus.publish(self.id, kind, payload, task_id=task_id)

    # -------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self.store.set_run_status(self.id, RunStatus.RUNNING)
        self._emit("run.started", {"run_id": self.id, "challenge": self.challenge.name})
        self._main_task = asyncio.create_task(self._main_loop())
        self._watchdog = asyncio.create_task(self._budget_watchdog())
        self._start_board_loop()

    async def reopen(self) -> None:
        """Bring a closed run back to life so an operator message can be served.

        The transcript already exists on disk, so the main session resumes with
        all of its context; only the run's clock restarts. This is what makes
        the chat box meaningful on a run that was interrupted by a restart or
        closed by its own ceiling: without it the page can only be read, and the
        operator has to start a fresh run to say one more sentence.
        """
        self._closed = False
        self._stop = asyncio.Event()
        self._started = time.time()
        self._accepting = True
        self._finish_status = ""
        self._finish_summary = ""
        self._operator_closing = False
        self._session_error = ""
        # A reopened run gets a fresh session, so the previous loop's auth
        # verdict does not carry over: the operator may have set the token and
        # restarted the server before opening the chat.
        self._auth_failed = False
        # Cleared here, not only in the loop: ``wait_until_listening`` runs
        # before the new task gets its first turn, and a stale "ready" from the
        # previous loop would report success against a session that is not open.
        self._session_ready.clear()
        self._post_stop_ok.clear()
        # An interrupt still in flight from the previous loop belongs to a
        # session that is about to be closed; keeping its target would make the
        # new loop's first turn look aborted.
        self._interrupt_target = -1
        self._lead_aborted = False
        if self._interrupt_task is not None and not self._interrupt_task.done():
            self._interrupt_task.cancel()
        self._interrupt_task = None
        # A closed run's loop may still be unwinding from its own shutdown; two
        # loops on one session would interleave their turns. Wait it out first.
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
            try:
                await asyncio.wait({self._main_task}, timeout=5)
            except Exception:  # noqa: BLE001 - reopening must not raise
                pass
        self.store.set_run_status(self.id, RunStatus.RUNNING)
        # Orphan reaping. A restart kills the old process's worker sessions
        # without finishing their task rows, and a revived lead that reads
        # those rows sees "running but silent" ghosts — observed live on run
        # 82: the lead assumed a ghost was stuck, force-dispatched a duplicate
        # over it, and the two workers collided. Any non-terminal row that
        # this process does not own is a ghost; the honest close is stopped,
        # and the warning tells the lead to read the board instead of
        # re-dispatching the same objective.
        reaped: list[int] = []
        for t in self.store.list_tasks(self.id):
            if t.terminal or t.id in self._tasks:
                continue
            self.store.finish_task(
                t.id,
                TaskStatus.STOPPED,
                {"summary": "服务重启时被中断，复活运行时收割"},
                "服务重启前的遗留任务",
            )
            self._emit(
                "task.finished",
                {"task_id": t.id, "role": t.role, "status": TaskStatus.STOPPED,
                 "error": "服务重启前的遗留任务"},
                task_id=t.id,
            )
            reaped.append(t.id)
        if reaped:
            self._emit(
                "run.warning",
                {"message": f"复活时收割了重启遗留的任务 {reaped}：它们的进展以黑板为准，"
                            "不要为同样的目标重新派工。"},
            )
        self._emit(
            "run.reopened",
            {"run_id": self.id, "session": bool(self._main_session)},
        )
        self._main_task = asyncio.create_task(self._main_loop(announce=False))
        self._watchdog = asyncio.create_task(self._budget_watchdog())
        self._start_board_loop()

    def _parallelism(self) -> dict[str, Any]:
        """Worker-seconds against wall-clock, as a ratio and a peak.

        ``ratio < 1`` means workers overlapped less than one-at-a-time would
        suggest; ``peak == 1`` says the run was serial no matter how many tasks
        it dispatched. Both numbers are needed: a run can dispatch six tasks and
        still have a peak of one.
        """
        worker_s, tasks, peak = self.store.task_seconds(self.id)
        wall = max(1.0, float(self.elapsed))
        return {
            "worker_seconds": round(worker_s, 1),
            "wall_seconds": round(wall, 1),
            "ratio": round(worker_s / wall, 2),
            "peak_concurrent": peak,
            "tasks": tasks,
        }

    async def shutdown(self, status: str, summary: str = "", reason: str = "") -> None:
        """Stop accepting work, cancel workers, close the main session. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._accepting = False
        self._stop.set()
        # How much of this run actually overlapped. Recorded at close, when the
        # task clock is final: the lead is told to fan out before its first
        # wait, run 18 measured 0.94x anyway, and a number per run is what turns
        # that into something a change can be judged against.
        self._emit("run.parallelism", self._parallelism())
        for task_id, task in list(self._tasks.items()):
            if task_id in self._post_stop_ok:
                continue
            if not task.done():
                task.cancel()
        for event in self._done.values():
            event.set()
        current = asyncio.current_task()
        # The clock calls shutdown from the watchdog's own task. Cancelling it
        # there cancels the caller: the CancelledError lands on the first
        # ``await`` below, skips the status write, and the run stays open with
        # no ``run.finished`` event. Observed live on run 5: the watchdog fired
        # at the 90-minute mark, the lead answered with finish_run, and the
        # database still read ``needs_input`` with an empty ``finished_at``.
        if self._watchdog and self._watchdog is not current:
            self._watchdog.cancel()
        # The board coordinator polls; stopping the run is its exit condition,
        # but a tick may be halfway through a dispatch. Cancel it here rather
        # than let it start one more worker into a closing run.
        if self._board_task and self._board_task is not current:
            self._board_task.cancel()
            self._board_task = None
        cancelled = False
        try:
            if self._main_task and not self._main_task.done() and self._main_task is not current:
                # Do not cancel the task that is calling shutdown: the main loop
                # closes the run from its own `finally`, and self-cancellation
                # would turn a clean close into a CancelledError.
                self._main_task.cancel()
                # Wait for it to unwind the `async with` around the session.
                # Exiting the client from both sides at once is what left a live
                # run stuck in `running`: shutdown hung on `__aexit__` and never
                # wrote a terminal status. asyncio.wait (not await) so the task's
                # own CancelledError is not raised here.
                try:
                    await asyncio.wait({self._main_task}, timeout=10)
                except Exception:  # noqa: BLE001 - closing must not raise
                    pass
            if self._main_client is not None:
                # Only reached if the main loop never got to its own `finally`.
                client = self._main_client
                self._main_client = None
                try:
                    await asyncio.wait_for(client.__aexit__(None, None, None), timeout=10)
                except Exception:  # noqa: BLE001 - a hung close must not block the write
                    pass
        except asyncio.CancelledError:
            # Someone cancelled the closing task itself. The run is over either
            # way, and a status row is what the operator and the UI read.
            cancelled = True
        finally:
            status = status or RunStatus.FAILED
            self.store.fail_running_tasks(self.id, TaskStatus.STOPPED, "运行已结束")
            self.store.set_run_status(self.id, status, summary=summary, reason=reason)
            # Distil this run into a case card before announcing the close: the
            # operator's next move is usually to start another challenge, and a
            # card written after the event would be missing for that one.
            case = None
            try:
                # File the direction before distilling: the card copies the
                # category, so a writeback after ``save`` would only help the
                # next run and not this card.
                self._writeback_direction()
            except Exception:  # noqa: BLE001 - memory must not block a close
                pass
            try:
                case = caselib.save(self)
            except Exception:  # noqa: BLE001 - memory must not block a close
                case = None
            # Charge the run's outcome to the cards it was shown. A card that
            # keeps appearing before runs that lose must lose its place, or the
            # library only ever grows louder.
            #
            # A verified flag is a verdict even when the run itself did not close
            # as ``solved``: the benchmark stops every run at the answer and has
            # no operator to confirm, so keying on the run status alone credited
            # nothing and the card that guided the run stayed at 0-0. A stopped
            # run with no verified flag stays uncounted — an operator stop says
            # nothing about the cards.
            attributed = 0
            notes_credited = 0
            try:
                flags = self.store.list_flags(self.id)
                solved = any(
                    f.status in (FlagStatus.VERIFIED, FlagStatus.CONFIRMED) for f in flags
                )
                if status in (RunStatus.SOLVED, RunStatus.FAILED) or solved:
                    attributed = caselib.attribute(self.store, self.id, solved)
                    # Same rule for the technique notes. A note is credited only
                    # when the run was shown it *and* its vocabulary appears in
                    # what the run wrote — being listed is exposure, not use. A
                    # resumed process has an empty in-memory list, so the run
                    # row is the fallback.
                    shown = self._shown_notes or [
                        Path(p) for p in self.store.get_run_notes(self.id)
                    ]
                    notes_credited = notes.balance(self.store, self.id, shown, solved)
            except Exception:  # noqa: BLE001 - attribution must not block a close
                pass
            payload = {"run_id": self.id, "status": status, "summary": summary, "reason": reason,
                       "cost_usd": round(self.cost_usd, 4)}
            if case is not None:
                payload["case_id"] = case.id
            if attributed:
                payload["cases_credited"] = attributed
            if notes_credited:
                payload["notes_credited"] = notes_credited
            self._emit("run.finished", payload)
        if cancelled:
            raise asyncio.CancelledError()

    def request_finish(self, status: str, summary: str) -> None:
        """Record the lead's intent to end the run.

        ``needs_input`` is *not* terminal: the run keeps its session so the
        operator's answer (or a flag confirmation) can still be delivered.
        ``solved``/``failed`` stop accepting new tasks; the actual close happens
        in :meth:`_main_loop`'s ``finally`` (or in :meth:`shutdown`).

        ``finish_run`` is called from inside the lead's own tool call, so the
        close must not happen synchronously: shutting the session down from
        within one of its handlers would kill the turn that is still streaming
        the result. The main loop notices the flag between turns instead.
        """
        allowed = {RunStatus.SOLVED, RunStatus.FAILED, RunStatus.NEEDS_INPUT}
        try:
            status = RunStatus(status)
        except ValueError:
            status = RunStatus.FAILED
        if status not in allowed:
            status = RunStatus.FAILED
        if status == RunStatus.SOLVED:
            # The lead does not get the last word on victory. A verifier-
            # reproduced flag already parks the run in needs_input; a lead
            # calling finish_run(solved) on top of that (observed on the
            # lcg_step experiment run — the gate was documented as "the
            # operator confirms" and the lead stepped over it) would close
            # the run without a human decision. Coerced to needs_input: the
            # operator's confirm is what turns a run solved.
            status = RunStatus.NEEDS_INPUT
        self._finish_status = status
        self._finish_summary = summary
        self._accepting = False
        if status == RunStatus.NEEDS_INPUT:
            self.store.set_run_status(self.id, RunStatus.NEEDS_INPUT, summary=summary)
            self._emit("run.needs_input", {"summary": summary})
        # Wake the main loop so a finished run closes promptly. The sentinel is
        # distinct from the ``None`` stop signal: a ``needs_input`` run must stay
        # alive for the operator's answer, so waking it must not end it.
        try:
            self._chat_queue.put_nowait(_WAKE)
        except asyncio.QueueFull:  # pragma: no cover - unbounded queue
            pass

    # ------------------------------------------------------------- main agent
    async def _main_loop(self, announce: bool = True) -> None:
        """Serve the lead's turns until the run closes.

        ``announce`` is the kickoff turn. A reopened run already has a
        transcript and a conversation, so its first turn is the operator's
        message — re-sending "Start working on this challenge" would ask it to
        solve the challenge a second time from scratch.
        """
        self._session_ready.clear()
        spec = self._main_spec()
        if self._main_session:
            # Reopen path (or a restart): continue the transcript the operator
            # was already talking to.
            spec = replace(spec, resume_session_id=self._main_session)
        try:
            client = self.ctx.backend.open_client(spec)
            async with client as session:
                self._main_client = session
                # Resume errors surface on the first turn, not on open: the CLI
                # starts, then reports "No conversation found". Opening the
                # session is enough to say chat can be served; if the turn dies,
                # its own error path closes the run and the user sees why.
                self._session_ready.set()
                final: dict[str, Any] = {}
                retries = 0
                if announce:
                    final = await self._stream(
                        session, self._kickoff_text(), task_id=None, role="main"
                    )
                while not self._stop.is_set() and not self._session_spent(final):
                    # `needs_input` keeps the loop alive: the operator's next
                    # message (an answer, or a flag confirmation) resumes work.
                    # An unauthenticated session is the exception: every turn
                    # returns the same synthetic login prompt, so "needs_input"
                    # would wait on a queue nothing can serve.
                    if (
                        self._finish_status
                        and self._finish_status != RunStatus.NEEDS_INPUT
                    ) or self._auth_failed:
                        break
                    item = await self._chat_queue.get()
                    if item is None:
                        break
                    if item is _WAKE:
                        continue
                    final = await self._stream(session, item, task_id=None, role="main")
                    # The lead has now read the board's hand-off. Only the
                    # message that was itself the hand-off clears the latch:
                    # matching on the text would let any chat line that happens
                    # to quote it swallow the notice the next dispatch relies on.
                    if self._board_wake_pending and item == self._board_wake_text:
                        self._board_wake_pending = False
                        self._board_wake_ids = []
                        self._board_wake_text = None
                    if not self._lead_aborted:
                        retries = 0
                    if self._lead_aborted and self._lead_turn_empty:
                        # The turn was cut before the lead read the message:
                        # an interrupt that reached the CLI just after its own
                        # turn had ended gets applied to the next one. Putting
                        # the text back keeps the operator's words from being
                        # eaten by a race they cannot see. Safe to retry
                        # forever — a turn that produced nothing has nothing to
                        # lose — but the counter stops a session that aborts
                        # every turn from spinning silently.
                        retries += 1
                        if retries <= 3:
                            await self._chat_queue.put(item)
                            continue
                        self._emit(
                            "session.error",
                            {"role": "main", "error": "主 agent 连续多回合未能读取操作员消息"},
                        )
                if self._session_spent(final) and not self._finish_status:
                    if self._auth_failed:
                        # Not a ceiling: the session never authenticated, and
                        # saying "hit its budget/turn ceiling" would send the
                        # operator looking at the wrong knob.
                        self.request_finish(
                            RunStatus.FAILED,
                            self._session_error
                            or "模型鉴权失败，运行无法继续（检查启动服务的环境变量或 config.toml）",
                        )
                    # The lead's own ceiling, not the run's. Say which one: the
                    # operator needs to know the run stopped because the lead
                    # ran out of room, not because the challenge was solved.
                    elif not self._auth_failed:
                        subtype = str(final.get("subtype") or "")
                        self.request_finish(
                            RunStatus.FAILED,
                            f"主 agent 撞上{'预算' if 'budget' in subtype else '轮数'}上限"
                            f"（{subtype}），运行结束。已派出的工单结论仍在黑板上。",
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._session_error = f"{type(exc).__name__}: {exc}"
            self._emit("run.error", {"error": self._session_error})
            if not self._finish_status:
                # A child that never started is the machine's problem, not the
                # run's: say so instead of printing an NTSTATUS code.
                hint = spawn_error_message(str(exc))
                self.request_finish(
                    RunStatus.FAILED, f"主 agent 异常：{hint or exc}"
                )
        finally:
            self._session_ready.set()
            self._main_client = None
            if self._interrupt_task is not None and not self._interrupt_task.done():
                # Its control request belongs to the session that is closing.
                self._interrupt_task.cancel()
            self._interrupt_task = None
            # A run whose main loop ended without an explicit finish_run is still
            # over. Marking it FAILED with a reason beats leaving it "running"
            # forever, which is what an operator would have to clean up by hand.
            #
            # Exception: an operator confirmation owns the close. It has just
            # dispatched the writer and is waiting for it; closing here would
            # cancel the writeup mid-turn (observed live).
            if not self._operator_closing and not self._closed and (
                self._finish_status != RunStatus.NEEDS_INPUT
            ):
                status = self._finish_status or RunStatus.FAILED
                summary = self._finish_summary or (
                    "主 agent 会话结束，但没有调用 finish_run"
                    if not self._stop.is_set()
                    else "已停止"
                )
                await self.shutdown(status, summary, "主 agent 会话结束")

    def _session_spent(self, final: dict[str, Any]) -> bool:
        """True when the session that produced ``final`` can serve no more turns.

        The CLI stops a session at its ceiling and every later turn returns the
        same error immediately, so continuing to wait on the chat queue strands
        the run in ``running`` forever.

        An interrupt is the exception: the operator cutting a turn in to speak
        ends it with the same ``error_during_execution`` subtype, and treating
        that as a dead session would close the run the first time they typed.
        """
        if self._lead_aborted:
            return False
        if self._auth_failed:
            # Every later turn returns the same synthetic login prompt. Waiting
            # on the chat queue strands the run in "running" while the operator
            # types messages nobody reads.
            return True
        subtype = str(final.get("subtype") or "")
        return bool(final) and (
            subtype.startswith("error_max_budget")
            or subtype.startswith("error_max_turns")
            or subtype.startswith("error_during_execution")
        )

    async def wait_until_listening(self, timeout: float = 8.0) -> bool:
        """Wait for the main session to open, so a chat message is not queued
        against a loop that never got that far."""
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return bool(self._main_client) and not self._closed

    async def _stream(self, session: Any, text: str, task_id: int | None, role: str) -> dict[str, Any]:
        """Consume one turn of a session, persisting every event.

        Costs reported by the SDK are cumulative per session, so only the delta
        is booked; otherwise a long chat would be charged once per turn.

        Returns the turn's final result payload. The caller needs it: a session
        that ended because it hit ``max_budget_usd`` cannot serve another turn,
        and a main loop that does not notice waits on its queue forever.
        """
        last_cost = 0.0
        if task_id is None:
            # The lead's client is long-lived (and may be a resumed transcript),
            # so its reported total is cumulative for the whole session, not for
            # this turn. Seeding from what was already booked keeps a second chat
            # turn from charging the first one again. Workers get a fresh
            # one-shot session per task, so their total starts at zero.
            last_cost = self._main_cost
        final: dict[str, Any] = {}
        # Each turn streams its own reply; the buffer is what the final message
        # is compared against (and what the UI replaces when it lands).
        self._stream_buf = ""
        self._turn_had_text = False
        is_lead = task_id is None
        turn = 0
        produced = 0
        if is_lead:
            self._lead_turn += 1
            turn = self._lead_turn
            self._lead_turn_active = True
            self._lead_aborted = False
            self._lead_turn_empty = True
        inflight: set[str] = set()
        stream = session.send(text)
        iterator = stream.__aiter__()
        try:
            while True:
                # Same two clocks as a worker: silence while a tool runs is the
                # tool's business, silence with nothing running is the model's.
                # The lead's own tools include `wait_tasks`, which blocks for up
                # to 120 s server-side, so one budget for both would report a
                # working turn as a stall.
                budget = (
                    self.cfg.limits.tool_wait_timeout_s
                    if inflight
                    else self.cfg.limits.stall_timeout_s
                )
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=budget)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # No automatic retry here, unlike a worker. A worker's stall
                    # leaves nothing behind, so replaying it is free; the lead's
                    # turn has side effects — dispatch_task, propose_flag — that
                    # have already landed by the time the model goes quiet.
                    # Re-sending the prompt would execute them twice. So: end the
                    # turn, name the cause, and leave the run open for the
                    # operator. `final` stays empty, which `_session_spent` reads
                    # as "session still usable".
                    self._emit(
                        "session.stalled",
                        {
                            "role": role,
                            "silent_s": budget,
                            "produced": produced,
                            "message": (
                                f"{role} 模型 {budget} 秒没有响应，本回合已中断。"
                                "运行仍然打开——确认网关恢复后继续发消息即可。"
                            ),
                        },
                        task_id,
                    )
                    if is_lead:
                        self._stream_buf = ""
                    break
                if event.kind == "tool_use":
                    inflight.add(str(event.payload.get("id") or ""))
                elif event.kind == "tool_result":
                    inflight.discard(str(event.payload.get("id") or ""))
                if event.kind == "result":
                    cost = max(0.0, float(event.cost_usd or 0.0) - last_cost)
                    last_cost = float(event.cost_usd or 0.0)
                    if is_lead:
                        # Decided before the event is recorded: an aborted
                        # turn's result text is the CLI's interrupt notice, not
                        # something the lead said, and must not be stored as a
                        # reply.
                        self._lead_aborted = self._was_aborted(turn, event.payload, produced)
                        if not self._lead_aborted and self._interrupt_target >= 0 and (
                            turn > self._interrupt_target
                        ):
                            # The interrupt was aimed at an earlier turn and this
                            # one ran normally, so it has been accounted for.
                            # Left set, it would make a genuine crash on some
                            # later turn look like an operator interrupt.
                            self._interrupt_target = -1
                        if self._lead_aborted:
                            # A half-written reply is not a message. The bubble
                            # streaming it has to be dropped, or the next turn's
                            # text is appended to the sentence that was cut.
                            self._stream_buf = ""
                            self._emit(
                                "main.interrupted",
                                {"reason": "操作员发来消息，已打断主 agent 当前回合"},
                            )
                        # An interrupt that arrives just after its turn ended is
                        # applied by the CLI to the turn that starts next. That
                        # turn is aborted before it can answer, so its prompt
                        # goes back on the queue instead of being swallowed.
                        self._lead_turn_empty = produced == 0
                    self._record_event(event, task_id, role, cost=cost)
                    final = event.payload
                    if is_lead:
                        # The turn is over the moment its result lands; clearing
                        # here (not only in ``finally``) keeps a message that
                        # arrives in the same tick from interrupting a turn that
                        # has already finished.
                        self._lead_turn_active = False
                else:
                    # Only evidence that the lead actually started on the turn
                    # counts. Session bookkeeping (``init`` etc.) arrives even
                    # for a turn that was cut at once, and treating it as
                    # "the message was read" is what would swallow the message.
                    if event.kind in ("text", "thinking", "tool_use", "text_delta", "thinking_delta"):
                        produced += 1
                    self._record_event(event, task_id, role)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._emit("session.error", {"role": role, "error": f"{type(exc).__name__}: {exc}"}, task_id)
        finally:
            if is_lead:
                self._lead_turn_active = False
            # A turn that ended on a stall leaves the generator suspended inside
            # the hung call; the persistent client cannot take another query
            # until it is closed.
            await _close_stream(stream)
        return final

    def _was_aborted(self, turn: int, final: dict[str, Any], produced: int = 0) -> bool:
        """Whether ``final`` ended ``turn`` because of our own interrupt.

        An interrupt is not a spent session. Getting this wrong closes the run
        the first time the operator speaks mid-turn, since the CLI reports both
        as ``error_during_execution``. ``terminal_reason`` is the CLI's own
        discriminator; the rest covers CLI versions that do not send it — an
        interrupt is outstanding and this turn produced nothing, so the turn it
        cut cannot have been doing anything of its own. Worse outcomes are
        asymmetric: an uncertain "aborted" leaves a run open for the operator to
        stop, while a false "dead" closes it on their first message.
        """
        reason = str(final.get("terminal_reason") or "")
        if reason.startswith("aborted"):
            return True
        subtype = str(final.get("subtype") or "")
        if not subtype.startswith("error_during_execution"):
            return False
        if self._interrupt_target == turn:
            return True
        return self._interrupt_target >= 0 and turn <= self._interrupt_target + 1 and produced == 0

    async def _summarize_after_ceiling(self, session: Any) -> None:
        """Give the lead one final turn after a ceiling.

        Without this, a run that hits its clock ends with the model mid-thought
        and the operator gets a status but no account of what was tried. The
        turn is bounded and the result is recorded as the run summary.

        Not used for a dollar ceiling: that session is already over budget, and
        asking it for one more turn is how a $5 run reached $14.
        """
        try:
            text = (
                "本次运行已经到达时间上限，不要再派新任务。"
                "用几行总结：试过什么、已经确认了什么、下一步打算怎么做。"
                "然后调用 finish_run，status 用 failed 或 needs_input。"
            )
            await self._stream(session, text, task_id=None, role="main")
        except BaseException:  # noqa: BLE001 - best effort; the run ends either way
            # BaseException on purpose: the bounded timeout cancels this turn
            # and the cancellation surfaces here as CancelledError, which an
            # `except Exception` would re-raise straight through the watchdog
            # (run 82's close died exactly there, inside a hung client).
            pass

    def _priced_cost(self, event: AgentEvent, task_id: int | None, role: str) -> float | None:
        """Session spend computed from token usage, when the model has prices.

        With a per-model price table in ``[models.pricing]`` the cost is
        computed from what the session actually consumed, instead of the CLI's
        name-based price-table lookup — which is fiction for a gateway model.

        ``usage`` is this call's tokens (see ``_USAGE_KEYS``), so the cost of an
        event is just its own usage priced; consecutive events add up. It is
        deliberately *not* a cumulative-total-delta: that resubtracted the
        previous call's tokens from this one's and lost the difference whenever
        a turn read back less than its predecessor.
        """
        if event.kind != "result":
            return None
        totals = _usage_totals(event.payload.get("usage"))
        if totals is None:
            return None
        model = self.cfg.models.main if role == "main" else self.cfg.models.model_for(role)
        rates = self.cfg.models.pricing.get(model) or {}
        if not any(rates.get(k) for k in ("input", "cache_read", "output")):
            return None
        return (
            totals["input_tokens"] * float(rates.get("input") or 0.0)
            + totals["cache_read_input_tokens"] * float(rates.get("cache_read") or 0.0)
            + totals["output_tokens"] * float(rates.get("output") or 0.0)
        ) / 1_000_000

    def _record_event(
        self, event: AgentEvent, task_id: int | None, role: str, cost: float | None = None
    ) -> None:
        priced = self._priced_cost(event, task_id, role)
        if priced is not None:
            # A price table in config.toml outranks both the CLI's number and
            # any delta precomputed from it: that number is an Anthropic
            # price-table lookup by model name, which is fiction for a gateway
            # model and has been steering the budget watchdog off real money
            # the whole time.
            cost = priced
        if cost is None:
            raw = float(event.cost_usd or 0.0)
            if task_id is None:
                cost = raw
            else:
                # ``total_cost_usd`` is cumulative for the session, and a
                # resumed session inherits the transcript it was handed, so its
                # first result already contains what that transcript cost. The
                # lead's path computes this delta at the call site; the worker
                # path used to add the raw total, which re-billed the inherited
                # spend on every resume and would have closed a run early on a
                # number that never corresponded to money actually spent.
                base = self._worker_base.get(task_id, 0.0)
                cost = max(0.0, raw - base)
                self._worker_base[task_id] = raw
        if cost:
            self.cost_usd += cost
            self.store.add_run_cost(self.id, cost)
            if task_id is not None:
                self.store.add_task_usage(task_id, cost=cost)
        if event.turns and task_id is not None:
            self.store.add_task_usage(task_id, turns=event.turns)
        if task_id is None:
            # The lead's session is the one chat has to be able to reach again
            # after a restart, and its cumulative CLI spend is what the next
            # result must be measured against (a resumed session keeps counting
            # from the transcript's total, not from zero).
            session_id = str(event.payload.get("session_id") or "")
            if session_id and session_id != self._main_session:
                self._main_session = session_id
                self.store.set_main_session(self.id, session_id, self._main_cost)
            if event.kind == "result":
                self._main_cost = float(event.cost_usd or 0.0)
                if self._main_session:
                    self.store.set_main_session(
                        self.id, self._main_session, self._main_cost
                    )
        if task_id is not None:
            live = self._live.setdefault(task_id, {"tools": 0, "last_tool": "", "last_event": 0.0})
            live["last_event"] = time.time()
            if event.kind == "tool_use":
                live["tools"] += 1
                live["last_tool"] = str(event.payload.get("tool") or "")
            elif event.kind == "tool_result":
                # Deduplicated only for the progress score: re-running the same
                # command and getting the same bytes is the archetype of a task
                # that looks busy and moves nothing. Bounded so a long task
                # cannot pin an unbounded set in memory.
                seen = self._outputs.get(task_id)
                if seen is None:
                    seen = self._outputs[task_id] = set()
                if len(seen) < 400:
                    seen.add(hash(str(event.payload.get("content") or "")[:4000]))
        self.turns += event.turns or 0
        if event.kind in ("text_delta", "thinking_delta"):
            # Live-only: the durable copy is the finished message below. Only
            # the main agent's text is streamed, so this is never a worker.
            if event.kind == "text_delta":
                self._stream_buf += str(event.payload.get("text") or "")
            self.bus.notify(self.id, event.kind, event.payload, task_id=task_id)
            return
        if event.kind == "auth_error":
            # The backend already dropped the CLI's synthetic "Not logged in ·
            # Please run /login" so it cannot land in the chat pane as the
            # lead's reply.
            if task_id is not None:
                # A worker's gateway auth failure fails that task and nothing
                # else. Observed live (bench, lcg_step, the very first real
                # run of this codebase): the verifier died on a 401
                # mid-session while the lead's own session was healthy — the
                # run-level sticky flag used to close the whole run, and a
                # challenge that was already solved scored as failed. The
                # lead re-dispatches on its own; if the token is truly dead,
                # the lead's own next turn fails auth and poisons the run
                # then, which is the right time.
                self._emit(
                    "session.error",
                    {
                        "role": role,
                        "error": auth_error_message(
                            self.cfg, str(event.payload.get("error") or "")
                        ),
                        "task_id": task_id,
                    },
                    task_id,
                )
                return
            # What replaces it has to name the cause and the file
            # to edit, and the run must stop waiting: every further turn fails
            # identically, so leaving it "running" only queues messages against
            # a session that can never answer.
            self._auth_failed = True
            self._session_error = auth_error_message(
                self.cfg, str(event.payload.get("error") or "")
            )
            self._emit(
                "session.error",
                {"role": role, "error": self._session_error, "task_id": task_id},
                task_id,
            )
            return
        self._emit(
            "agent.event",
            {"role": role, "task_id": task_id, **event.to_dict()},
            task_id,
        )
        if event.kind == "result" and task_id is None:
            # The reply was already stored when its text block arrived (below).
            # ``result`` only repeats the final block, so storing it here too
            # would duplicate the message. It is still the fallback for a turn
            # that produced text without a block event.
            result_text = str(event.payload.get("result") or "")
            # An auth failure's result text is the CLI's synthetic login prompt,
            # not something the lead said. ``_record_event`` has already turned
            # the assistant half of it into a ``session.error``; storing the
            # result's copy would put the prompt back in the chat pane.
            if (
                not self._turn_had_text
                and result_text.strip()
                and not self._lead_aborted
                and not self._auth_failed
            ):
                self.store.add_message(self.id, "assistant", result_text)
                self._emit("main.message", {"text": result_text, "streamed": False})
            self._stream_buf = ""
        elif event.kind == "text" and task_id is None:
            # Persist the lead's reply the moment it is written, not when the
            # turn ends. A turn can stay open for many minutes (it dispatches,
            # then waits on workers), and a reply that only appears at the end
            # leaves the operator staring at a silent page. Observed live: the
            # lead wrote a real finding at 15:16 and the chat pane still showed
            # nothing at 15:23.
            text = str(event.payload.get("text") or "").strip()
            if text:
                self.store.add_message(self.id, "assistant", text)
                self._emit(
                    "main.message",
                    {"text": text, "streamed": bool(self._stream_buf)},
                )
                self._turn_had_text = True
            self._stream_buf = ""

    async def chat(
        self, text: str, attachments: list[str] | None = None, display: str = ""
    ) -> None:
        """Deliver an operator message to the main agent.

        ``text`` is what the agent reads and already names any attachments;
        ``display`` is what the operator actually typed, which is what the chat
        bubble shows. Keeping them apart is what lets the page render file cards
        instead of repeating a sentence that lists the files.
        """
        self.note_operator_message(display or text, attachments)
        # A pending question is answered before the message is treated as a new turn.
        if self._question_future and not self._question_future.done():
            self._question_future.set_result(text)
            return
        # Queue first, then cut the turn: the message is waiting when the
        # interrupted stream ends, so the loop picks it up on its next pass
        # instead of after a tool call that may run for minutes.
        await self._chat_queue.put(text)
        if self._lead_turn_active:
            self._interrupt_lead()

    def _interrupt_lead(self) -> None:
        """Cut the lead's in-flight turn so it can read the operator now.

        Workers are separate tasks and are untouched — only the lead is
        stopped, which is what the operator asked for: answer me before you
        keep going.
        """
        session = self._main_client
        if session is None or not self._lead_turn_active:
            return
        if self._interrupt_target == self._lead_turn:
            return
        # Read by ``_was_aborted`` when the turn's result arrives: an abort
        # this method caused must not be mistaken for a spent session.
        self._interrupt_target = self._lead_turn
        # Not awaited: the SDK waits for the CLI's acknowledgement, and the
        # operator's POST must not hang on a process that is busy in a tool.
        self._interrupt_task = asyncio.create_task(
            self._send_interrupt(session, self._lead_turn)
        )

    async def _send_interrupt(self, session: Any, turn: int) -> None:
        current = asyncio.current_task()
        try:
            await session.interrupt()
        except Exception as exc:  # noqa: BLE001 - the message is still queued
            if self._interrupt_target == turn:
                self._interrupt_target = -1
            self._emit("session.error", {"role": "main", "error": f"打断失败：{exc}"})
        finally:
            if self._interrupt_task is current:
                self._interrupt_task = None

    def note_operator_message(self, text: str, attachments: list[str] | None = None) -> None:
        """Record an operator message without waking the main loop.

        The first message of a run is its kickoff: the run is not listening yet
        (and the kickoff prompt is not the raw text), so the operator's own words
        are stored here — otherwise the conversation would open with the agent
        answering a question the page never showed.
        """
        self.store.add_message(self.id, "user", text, attachments=attachments)
        self._emit("chat.user", {"text": text, "attachments": attachments or []})

    async def _ask_human(self, question: str) -> str:
        """Block the tool call (not the event loop) until the operator answers.

        The wait is capped below the CLI's ``MCP_TOOL_TIMEOUT`` (180s, set in
        ``backend._credential_env``). If it timed out at the CLI level the tool
        call would fail with an opaque error; instead it returns an honest "no
        answer yet" so the agent can keep working and ask again.
        """
        loop = asyncio.get_running_loop()
        self._question_future = loop.create_future()
        self._question_text = question
        self.store.set_run_status(self.id, RunStatus.NEEDS_INPUT)
        self._emit("run.question", {"question": question})
        try:
            answer = await asyncio.wait_for(self._question_future, timeout=120)
        except asyncio.TimeoutError:
            answer = "（操作员还没回复 —— 继续干活，之后可以在对话里补答）"
        finally:
            self._question_future = None
            if not self._closed:
                self.store.set_run_status(self.id, RunStatus.RUNNING)
        return answer

    # ------------------------------------------------------------------ specs
    def _case_block(self) -> str:
        """The case-library block for this run's prompts, computed once.

        Reading and ranking the library on every dispatch would put a scan of
        every past run in front of the event loop each time a worker starts.
        The challenge does not change during a run, so neither does the answer.
        """
        if self._case_block_cache is None:
            try:
                cases = caselib.relevant(self.store, self.challenge, limit=3)
            except Exception:  # noqa: BLE001 - a broken library must not stop a run
                cases = []
            for case in cases:
                try:
                    self.store.touch_case(case.id)
                except Exception:  # noqa: BLE001 - usage counting is not load-bearing
                    pass
            self._case_block_cache = caselib.render(cases)
            if cases:
                # Which cards this run was shown, so the close can charge the
                # run's outcome to them. ``used_count`` says "was present";
                # only the outcome says whether it helped.
                try:
                    self.store.set_run_cases(self.id, [c.id for c in cases])
                except Exception:  # noqa: BLE001 - attribution is not load-bearing
                    pass
                self._emit(
                    "cases.loaded",
                    {"case_ids": [c.id for c in cases], "names": [c.name for c in cases]},
                )
        return self._case_block_cache

    def _main_spec(self) -> SessionSpec:
        skills = list(ROLE_SKILL.values())
        block = challenge_block(self.challenge, self.workspace.work)
        allowed = [f"mcp__orchestrator__{name}" for name in ORCHESTRATOR_TOOLS]
        # Read-only inspection is allowed so the lead can look at an attachment
        # before deciding whom to dispatch. Running commands and editing files
        # is not: that is the workers' job, and keeping it out of the lead's
        # context is what keeps it from becoming a second, unsupervised solver.
        allowed += ["Read", "Glob", "Grep", "TodoWrite"]
        return SessionSpec(
            role="main",
            prompt="",
            cwd=self.workspace.work,
            system_prompt=prompts.main_system_prompt(
                self.id, block, skills, self._case_block(), swarm=self.swarm_enabled
            ),
            model=self.cfg.models.main,
            max_turns=self.cfg.limits.task_max_turns * 2,
            # Not the run budget: the lead is one of several sessions, and
            # inheriting the whole run ceiling lets it spend the run's entire
            # allowance by itself (observed: a $5 run billed $14).
            max_budget_usd=self.cfg.limits.main_budget_usd,
            allowed_tools=allowed,
            # "Bash" alone is not enough: the CLI names the shell tool after the
            # host shell, so on Windows the lead gets "PowerShell" and the
            # disallow list silently does nothing. Observed live: the lead ran
            # 47 PowerShell commands and did its own reversing.
            disallowed_tools=[
                "Bash", "PowerShell", "Write", "Edit", "MultiEdit", "NotebookEdit", "Task"
            ],
            mcp_servers={"orchestrator": build_sdk_mcp_server(self.tools)},
            hooks=build_hooks([self.workspace.root], self.ctx.sealed_roots),
            # The lead has WebFetch, and a 502 from the proxy would read as the
            # target being down just as it did for the workers.
            extra_env=self._session_env(),
            # Triage routing lives in `solve-challenge`; the lead needs it to
            # decide whom to dispatch. An allowlist keeps the operator's global
            # skills (design, code-review, ...) out of the session.
            skills=["solve-challenge"],
            # The lead's text is the conversation the operator is reading, so
            # it is streamed as it is written. Workers are not: their transcript
            # is inspected after the fact.
            stream_text=True,
        )

    def _committed_total(self, exclude: int | None = None) -> float:
        """Ceilings already handed to sessions that have not finished yet.

        Gateway dollars — the same unit as ``run_budget_usd`` and
        ``self.cost_usd``. A ceiling configured in CLI dollars is converted
        before it is recorded here (see ``_budget_parts``).
        """
        return sum(v for k, v in self._committed.items() if k != exclude)

    def _budget_parts(
        self, role: str, resume_spent: float = 0.0, exclude: int | None = None
    ) -> tuple[float, float]:
        """``(SDK ceiling, reservation)`` for a session, in that order.

        The SDK ceiling is in **CLI dollars** (`max_budget_usd` is judged by the
        CLI's own price table); the reservation is in **gateway dollars**, the
        unit of ``run_budget_usd`` and ``self.cost_usd``. Mixing them is what
        made three parallel flash workers "reserve" $24 of a $30 pool while
        being entitled to $1.6 each — see ``models.cli_cost_factor``.

        The cap matters because worker ceilings are not additive: three
        parallel reverse tasks can exceed a small run on their own.

        ``remaining`` subtracts the ceilings of sessions still in flight, not
        just what has been spent. Without that, every worker dispatched in the
        same tick reads the same remainder as if it were the only one, and the
        run closes having spent the sum of all of them — run 18 committed $36
        against a $30 budget and closed at $35.78.

        ``exclude`` is the task asking, so a resumed or wrapped-up session does
        not compete with the ceiling it was already granted.

        ``resume_spent`` is what the resumed transcript already cost (gateway
        dollars). The CLI carries that spend into the ceiling of the resumed
        invocation, so it has to be added back — converted — or the ceiling
        would be exhausted before the first turn.
        """
        limits = self.cfg.limits
        factor = self.cfg.models.cli_factor(self.cfg.models.model_for(role))
        base = float(limits.role_budget_usd.get(role, limits.task_budget_usd))
        remaining = (
            limits.run_budget_usd
            + self._extra_budget
            - self.cost_usd
            - self._committed_total(exclude)
        )
        if remaining <= 0:
            return 0.0, 0.0
        granted = max(0.0, min(base / factor, remaining))
        return granted * factor + max(0.0, resume_spent) * factor, granted

    def _task_budget(
        self, role: str, resume_spent: float = 0.0, exclude: int | None = None
    ) -> float:
        """The ceiling handed to the SDK, in CLI dollars."""
        return self._budget_parts(role, resume_spent, exclude)[0]

    def _reserve_ceiling(
        self, task: Any, role: str, resume_spent: float, *, started_by_board: bool
    ) -> tuple[float, float]:
        """``(SDK ceiling, reservation)`` with the board's ledger folded in.

        Two ledgers exist and they answer different questions. ``_committed``
        is the run pool's: every live session's ceiling, so the next dispatch
        sees a real remainder. ``swarm.Budget`` is the board's: claim-time
        reservations that keep the closing roles' 15% out of an exploring
        worker's reach.

        Folding them by taking the *smaller* remainder is deliberate — a
        session must be refused if either ledger says the money is gone. Only
        sessions the board opened are checked against the board's ledger: the
        lead still dispatches closing roles and triage itself, and those must
        never be unable to start because a stale hold from an earlier board
        worker is still on the books (settlement releases it, but a crash
        between claim and dispatch would otherwise freeze the pool).
        """
        ceiling, reservation = self._budget_parts(role, resume_spent, exclude=task.id)
        budget = getattr(self, "_board_budget", None)
        if budget is None or not started_by_board:
            return ceiling, reservation
        # The board ledger is in gateway dollars, like ``reservation``. A
        # wrap-up turn or a re-dispatch of the *same* task must not compete with
        # the hold it already owns — that is the ``exclude`` rule above, applied
        # to the second ledger.
        free = budget.free() + budget.holds.get(task.id, 0.0)
        if free >= reservation:
            return ceiling, reservation
        factor = self.cfg.models.cli_factor(self.cfg.models.model_for(role))
        withheld = (reservation - free) * factor
        return max(0.0, ceiling - withheld), free

    def _task_clock(self, role: str) -> int:
        """The wall clock one task of ``role`` gets, in seconds.

        Keyed by the challenge's category, with one correction: a verifier
        reproduces a single value and a writer writes one report. Neither is
        doing the challenge's work, so inheriting a reverse challenge's clock
        gave a verifier the same hour the reverse worker had — for work that is
        bounded by construction.
        """
        category = (self.challenge.category or "").lower()
        seconds = self.cfg.limits.task_timeout_s_by_category.get(
            category, self.cfg.limits.task_timeout_s
        )
        if role in CLOSING_ROLES:
            seconds = min(seconds, self.cfg.limits.closing_task_timeout_s)
        return int(seconds)

    def _task_turns(self, role: str) -> int:
        limits = self.cfg.limits
        return limits.role_max_turns.get(role, limits.task_max_turns)

    def _worker_spec(
        self, task: Any, resume_session_id: str = "", resume_spent: float = 0.0
    ) -> SessionSpec:
        role = task.role
        skill = ROLE_SKILL.get(role, "")
        block = challenge_block(self.challenge, self.workspace.work)
        # A task that is executing a board entry carries that entry's claim and
        # its falsification criterion into the prompt: "prove this" without
        # "this is how it would be wrong" is how a worker reports agreement
        # instead of evidence.
        hypothesis = (
            self.store.get_hypothesis(task.hypothesis_id)
            if getattr(task, "hypothesis_id", None)
            else None
        )
        task_context = task.context
        if hypothesis is not None:
            task_context = f"{task_context}\n\n{swarm.describe(hypothesis)}".strip()
        # Sized to the whole run, not to a sample of it: the digest used to take
        # the newest 40 of 51-74 facts, which dropped the oldest ones — and the
        # oldest are the foundational records a new worker needs in order not to
        # re-derive them.
        blackboard = self.blackboard_digest(limit=self.cfg.limits.blackboard_facts)
        # Computed once: the same ceiling is put in front of the worker and
        # handed to the SDK's enforcement. Two different numbers is how a worker
        # paces itself against a limit that is not the one that cuts it off.
        max_turns = self._task_turns(role)
        # The board's reservation ledger exists to keep the closing reserve out
        # of reach of exploring sessions. Only sessions the board opened are
        # checked against it: the lead dispatches closing roles itself and
        # manages its own resume/retry accounting (``exclude``,
        # ``resume_spent``), so charging one ceiling in two ledgers would count
        # the same money twice.
        started_by_board = bool(getattr(task, "hypothesis_id", None))
        ceiling, reservation = self._reserve_ceiling(
            task, role, resume_spent, started_by_board=started_by_board
        )
        # Claim-time hold on the board's ledger, released in ``_run_task``'s
        # ``finally`` whether or not the session ever started. Once per task:
        # ``_worker_spec`` is also called for the post-ceiling wrap-up and for
        # a resumed invocation, and holding the same ceiling again would count
        # one session twice. A refused hold needs no bookkeeping — the refund
        # in the ``finally`` is keyed on the amount that actually went in.
        if started_by_board and self._board_budget is not None:
            if task.id not in self._board_budget.holds:
                self._board_budget.reserve(task.id, reservation)
        # What that ceiling is worth in gateway dollars. The worker plans its
        # work against the number it is shown, and the two differ 4.9x on flash
        # (see ``ModelsConfig.cli_cost_factor``): a web worker told "$8.00" plans
        # $8 of work, but the CLI stops it at $1.63 of real spend.
        factor = self.cfg.models.cli_factor(self.cfg.models.model_for(role))
        if role == "triage":
            system = prompts.triage_prompt(block, self._case_block())
        elif role == "verifier":
            system = prompts.verifier_prompt(block, blackboard, self.challenge.category or "")
            if self.challenge.category in RE_CATEGORIES:
                # Same primitives as the claimant: reproducing a reversing
                # result by re-running their script is not reproduction.
                system += prompts.RE_TOOLS_BLOCK
            if (self.challenge.category or "").lower() == "pwn":
                # The verifier re-runs the exploit; the exploit only runs
                # inside WSL, so it needs the same workflow the claimant had.
                system += self.ctx.sandbox.pwn_guidance()
        elif role == "writer":
            system = prompts.writer_prompt(
                block, self.confirmed_flag_value(), blackboard, self._tasks_text()
            )
        else:
            # The verifier deliberately gets no case library: it must reproduce
            # the claim from the artifacts, and a card saying "last time this
            # shape gave X" is exactly the shortcut it exists to rule out.
            system = prompts.worker_system_prompt(
                role, skill, block, blackboard, self.sandbox_note(), self._case_block(),
                # A pasted challenge usually carries its URL in the description
                # and leaves ``remote`` empty, which used to hide the
                # remote-service discipline from exactly the workers who needed it.
                remote=bool(self._no_proxy()),
                no_proxy=self._no_proxy(),
                budget=prompts.budget_block(
                    max_turns, ceiling, resumed=bool(resume_session_id),
                    real_usd=ceiling / factor if factor else None,
                ),
                # The injection kit block follows the challenge's category, not
                # the role: a misc challenge that turns out to be a web app is
                # dispatched to a ``misc`` worker, and that worker is the one
                # who will be writing the request loop.
                category=self.challenge.category or "",
            )
            if role == "pwn":
                # ELF execution lives in WSL on this host; without the
                # workflow the worker rediscovers the /mnt/d trap run by run
                # (observed: workers improvised a ~/work copy on their own in
                # run 5 and paid turns for it).
                system += self.ctx.sandbox.pwn_guidance()
        prompt = prompts.worker_prompt_text(
            role, task.objective, task_context, resumed=bool(resume_session_id)
        )
        if role not in ("triage", "verifier", "writer"):
            # The verifier stays deliberately shortcut-free, and triage sees
            # the case block instead; a technique index belongs to the workers
            # who might apply the method.
            note_index = self._learned_notes_block(skill)
            if note_index:
                system += note_index
        allowed = [
            # The CLI names the shell tool after the host shell: "Bash" does not
            # exist on Windows, and asking for it drew "No such tool available".
            "Bash", "PowerShell", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite",
            "WebFetch", "WebSearch",
            "mcp__worker__record_fact",
            "mcp__worker__record_unknown",
            "mcp__worker__submit_flag",
            "mcp__worker__report_progress",
            "mcp__worker__request_help",
            "mcp__worker__task_complete",
            "mcp__worker__read_skill_doc",
            "mcp__worker__post_hypothesis",
        ]
        if patrol.is_patrol(task.objective):
            # A patrol reads the board and answers; it does not run experiments.
            # Dropping shell, network and the RE server is what makes its cost
            # one round trip and its blast radius nothing.
            allowed = list(patrol.PATROL_TOOLS)
            max_turns = min(max_turns, patrol.PATROL_MAX_TURNS)
            ceiling = min(ceiling, patrol.PATROL_CEILING_CLI_USD)
        servers: dict[str, Any] = {
            "worker": worker_tools.build_sdk_mcp_server(
                worker_tools.WorkerTools(self, task.id)
            )
        }
        # Binary reconnaissance is handed to the roles that actually need it.
        # Giving it to every worker would put ten more tools in front of a web
        # worker whose whole job is HTTP.
        if not patrol.is_patrol(task.objective) and (
            role in RE_ROLES
            or (role == "verifier" and self.challenge.category in RE_CATEGORIES)
        ):
            servers["re"] = re_tools.build_sdk_mcp_server(re_tools.ReTools(self, task.id))
            allowed += [f"mcp__re__{name}" for name in re_tools.TOOL_NAMES]
        return SessionSpec(
            role=role,
            prompt=prompt,
            cwd=self.workspace.work,
            system_prompt=system,
            model=self.cfg.models.model_for(role),
            max_turns=max_turns,
            max_budget_usd=ceiling,
            allowed_tools=allowed,
            mcp_servers=servers,
            hooks=build_hooks(
                [self.workspace.root, self.workspace.attachments], self.ctx.sealed_roots
            ),
            resume_session_id=resume_session_id,
            extra_env=self._session_env(),
            # One skill per role: the technique index for the direction. The CLI
            # discovers every skill in the workspace, but the allowlist is what
            # keeps the operator's global skills out of a competition session.
            skills=[skill] if skill else None,
        )

    def sandbox_note(self) -> str:
        return self.ctx.sandbox.guidance

    def _no_proxy(self) -> str:
        """Hosts this run must reach without the operator's proxy.

        Empty when the challenge names no target, in which case the workers
        inherit the operator's proxy untouched.
        """
        return no_proxy_value(self.challenge)

    def _session_env(self) -> dict[str, str]:
        """Env applied to every session of this run.

        The SDK merges ``os.environ`` with this, so the operator's
        ``HTTP_PROXY`` reaches every worker whether or not it is repeated here.
        ``NO_PROXY`` is the only way to keep it off the target: without it a
        challenge port that is not 80/443 comes back as the proxy's own ``502``,
        and workers recorded that fake status as target behaviour.
        """
        env = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        # Never let the CLI swap its own binary mid-competition. Observed live
        # on 2026-10-31 17:19: for a couple of minutes
        # ``...\@anthropic-ai\claude-code\bin\claude.exe`` was not openable —
        # the directory held two ``claude.exe.old.<timestamp>`` files, the
        # signature of an auto-update replacing a 300 MB executable — and every
        # worker dispatched in that window died in the same second. A run does
        # not need a new CLI version mid-challenge; it needs the one it started
        # with.
        env["DISABLE_AUTOUPDATER"] = "1"
        # Let the lead's wait_tasks block for minutes inside one MCP call.
        # The CLI kills an MCP tool call that exceeds MCP_TOOL_TIMEOUT
        # (milliseconds); at the stock ceiling the lead could wait at most
        # ~2 minutes per call and re-polled up to 65 times per run, each poll
        # a full model turn. 600 s matches the CLI's own Bash tool ceiling.
        env["MCP_TOOL_TIMEOUT"] = "600000"
        # Put this project's venv first on PATH, so a worker's `python` is the
        # interpreter the dependencies were installed into. Without it a worker
        # ran whatever `python` the operator's shell happened to resolve — on
        # this host a system interpreter with none of the CTF toolkit — while
        # `ctfagent doctor` reported the venv, and the two never met.
        venv_bin = Path(sys.executable).parent
        if venv_bin.is_dir():
            existing_path = os.environ.get("PATH", "")
            env["PATH"] = f"{venv_bin}{os.pathsep}{existing_path}" if existing_path else str(venv_bin)
        hosts = self._no_proxy()
        if hosts:
            env["NO_PROXY"] = hosts
            env["no_proxy"] = hosts
            # WSL does not inherit Windows env vars unless they are named here.
            # Keep anything the operator already listed.
            existing = os.environ.get("WSLENV", "").strip(":")
            extra = "NO_PROXY:no_proxy"
            env["WSLENV"] = f"{existing}:{extra}" if existing else extra
        return env

    def _kickoff_text(self) -> str:
        return prompts.main_kickoff(
            challenge_block(self.challenge, self.workspace.work),
            self.workspace.work,
            self.workspace.attachments,
        )

    # ------------------------------------------------------------------ tasks
    def live_direction_tasks(self, role: str) -> list[Any]:
        """Tasks of ``role`` that have not ended yet."""
        return [
            t
            for t in self.store.list_tasks(self.id)
            if t.role == role and not t.terminal
        ]

    def resumable_tasks(self, role: str) -> list[Any]:
        """Ended tasks of ``role`` whose transcript can still be continued.

        A fresh worker in a direction that already has one costs the whole
        context load again. Run 15 dispatched seven reverse workers, each
        re-reading the same 1312-layer archive from the start, because the only
        guard looked at tasks that had not ended yet.
        """
        return [
            t
            for t in self.store.list_tasks(self.id)
            if t.role == role
            and t.terminal
            and t.status == TaskStatus.EXHAUSTED
            and t.session_id
        ]

    def duplicate_direction_warnings(
        self, role: str, resume_task_id: int | None = None
    ) -> list[str]:
        """Say so when a dispatch repeats a direction that is already covered.

        Two cases, both observed live in run 5: a second worker in a direction
        that still has one running (two reverse workers, $10.43, both cut at the
        turn ceiling), and a fresh worker where a transcript already exists
        (the same two tasks, the second re-doing the first one's work).
        """
        out: list[str] = []
        if resume_task_id is not None:
            return out
        live = self.live_direction_tasks(role)
        if live:
            names = "、".join(f"#{t.id}" for t in live)
            out.append(
                f"{role} 方向已有活跃工单 {names}。同一个方向并行两个工单通常是把同一段"
                f"分析付两遍钱；如果它卡住了，用 resume_task_id 接上它，或等它结束后再派。"
            )
        spent = self.resumable_tasks(role)
        if spent:
            names = "、".join(f"#{t.id}" for t in spent[-2:])
            out.append(
                f"{role} 方向有撞上限但可续跑的工单 {names}；带 resume_task_id 接上它的"
                f"会话，比新开一个便宜。"
            )
        return out

    async def dispatch(
        self,
        role: str,
        objective: str,
        context: str = "",
        resume_task_id: int | None = None,
        hypothesis_id: int | None = None,
    ) -> Any:
        task = self.store.create_task(self.id, role, objective, context, hypothesis_id)
        if resume_task_id is not None:
            self._bind_resume(task, resume_task_id)
        self._done[task.id] = asyncio.Event()
        self._emit(
            "task.dispatched",
            {"task_id": task.id, "role": role, "objective": objective, "context": context,
             "resume_task_id": resume_task_id},
            task_id=task.id,
        )
        self._tasks[task.id] = asyncio.create_task(self._run_task(task.id))
        return task

    def _note_gateway_failure(
        self, task: Any, payload: dict[str, Any], last_text: str
    ) -> str:
        """Record an endpoint-level failure and return its text, or ``""``.

        The result payload says ``subtype: success`` with ``is_error: true`` and
        the gateway's message as the *text*, so nothing in the status told the
        lead this was infrastructure. It read as "the pwn attempt failed" and
        dispatched pwn again — four times in run 84, three minutes each, all to
        a model the endpoint does not serve.
        """
        text = " ".join(
            str(payload.get("result") or last_text or "").split()
        )
        message = gateway_error(text)
        if not message:
            return ""
        role = str(getattr(task, "role", "") or "")
        model = self.cfg.models.model_for(role) if role else self.cfg.models.main
        self._gateway_failures.append((time.time(), model, message))
        del self._gateway_failures[:-20]
        if model not in self._gateway_warned:
            self._gateway_warned.add(model)
            self._emit(
                "run.warning",
                {
                    "kind": "gateway_error",
                    "model": model,
                    "message": (
                        f"端点拒绝了这个模型（{model}）：{message[:200]} "
                        "这不是题目难度问题，也不是这个方向走不通。到 /settings "
                        "换供应商或把模型改成端点提供的名字；修好之前继续派工只会"
                        "重复同一个必然失败。"
                    ),
                },
            )
        return message

    def _note_infra_failure(self, message: str) -> str:
        """Record a machine-level failure and return an actionable line, or "".

        A task whose session never started must not read like a challenge that
        was tried and failed: on 2026-10-31 eight tasks died in the same second
        with an empty error column, and the lead — seeing only "failed" — kept
        dispatching instead of asking the operator.
        """
        text = " ".join(str(message or "").split())
        hint = spawn_error_message(text) or cli_missing_message(text)
        if not hint:
            return ""
        self._infra_failures.append((time.time(), hint))
        del self._infra_failures[:-20]
        if hint not in self._infra_warned:
            self._infra_warned.add(hint)
            self._emit(
                "run.warning",
                {"kind": "infra_error", "message": hint},
            )
        return hint

    def infra_outage(self, window_s: float = 900.0) -> dict[str, Any]:
        """Recent machine-level failures, when there are enough to stop dispatching."""
        now = time.time()
        recent = [f for f in self._infra_failures if now - f[0] <= window_s]
        if len(recent) < 2:
            return {}
        return {"count": len(recent), "message": recent[-1][1], "window_s": int(window_s)}

    def gateway_outage(self, window_s: float = 900.0) -> dict[str, Any]:
        """Recent endpoint rejections, when there are enough to stop dispatching."""
        now = time.time()
        recent = [f for f in self._gateway_failures if now - f[0] <= window_s]
        if len(recent) < 2:
            return {}
        _, model, message = recent[-1]
        return {
            "count": len(recent),
            "model": model,
            "message": message,
            "window_s": int(window_s),
        }

    def stop_task(self, task_id: int, reason: str = "") -> dict[str, Any]:
        """Cancel one task and free its worker slot.

        The lead could open directions but not close them: a direction whose
        premise a later finding had already refuted kept its slot and its wall
        clock until the clock reaped it — on a 45-minute clock that is most of
        a four-slot run spent on a route the blackboard marks as dead. Stopping
        is what turns a refutation into a freed slot.

        The task is closed as STOPPED by ``_run_task``'s own cancellation
        handler, so this returns before that has happened; the next
        ``wait_tasks`` reports it terminal.
        """
        task = self.store.get_task(int(task_id))
        if task is None or task.run_id != self.id:
            return {"error": f"工单 #{task_id} 不属于本次运行"}
        if task.terminal:
            return {
                "error": f"工单 #{task_id} 已经结束（{task.status}），没有可停的东西",
                "status": str(task.status),
            }
        handle = self._tasks.get(task.id)
        if handle is None or handle.done():
            return {"error": f"工单 #{task_id} 没有在运行"}
        handle.cancel()
        if reason.strip():
            # The transcript is about to disappear from the lead's view; the
            # reason is what keeps the decision reviewable.
            self.record_fact(
                None,
                FactKind.OBSERVATION,
                f"主 agent 停止了工单 #{task.id}（{task.role}）：{reason.strip()[:300]}",
                {"system": True, "stopped_task_id": task.id},
            )
        self._emit(
            "task.stopping",
            {"task_id": task.id, "role": task.role, "reason": reason.strip()[:300]},
            task_id=task.id,
        )
        return {
            "task_id": task.id,
            "role": task.role,
            "status": "stopping",
            "note": "已发出停止信号；下一次 wait_tasks 会看到它的最终状态，槽位同时释放。",
        }

    def _bind_resume(self, task: Any, prior_id: int) -> None:
        """Point a new task at the transcript of a task that ran out of room.

        Only an exhausted task is resumable: it stopped with work in progress.
        A task that finished or failed has nothing left to continue, and
        resuming it would replay a transcript to reach the same conclusion.
        """
        prior = self.store.get_task(int(prior_id))
        reason = ""
        if prior is None or prior.run_id != self.id:
            reason = f"工单 #{prior_id} 不属于本次运行"
        elif prior.role != task.role:
            reason = f"工单 #{prior_id} 的角色是 {prior.role}，与本工单 {task.role} 不同"
        elif prior.status != TaskStatus.EXHAUSTED:
            reason = f"工单 #{prior_id} 的状态是 {prior.status}，只有撞上限的工单可以续跑"
        elif not prior.session_id:
            reason = f"工单 #{prior_id} 没有留下会话 id，无法续跑"
        if reason:
            # Not fatal: the new task still runs, just from a clean transcript.
            self.record_fact(
                task.id, FactKind.OBSERVATION, f"续跑请求被忽略：{reason}。本工单从头开始。",
                {"resume_task_id": prior_id},
            )
            return
        self._resume[task.id] = (prior.session_id, self.store.get_task_cost(prior.id))

    async def _run_task(self, task_id: int) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        status: str = TaskStatus.FAILED
        error = ""
        last_text = ""
        final_payload: dict[str, Any] = {}
        try:
            # The worker slots bound how many *directions* may run at once, so
            # the lead cannot fan out past what it can read. A verifier is not a
            # direction: it confirms a value the run has already found, and it
            # used to queue behind four busy workers — the operator's confirm
            # then expired after ``confirm_verifier_wait_s`` (90 s) without a
            # verdict, because the session had not even started. Closing roles
            # still take a pool slot (``max_sessions``), which is the real
            # resource bound.
            slots = (
                contextlib.nullcontext()
                if task.role in CLOSING_ROLES
                else self._worker_slots
            )
            async with slots:
                async with self.ctx.pool:
                    self.store.mark_task_started(task_id)
                    self._emit("task.started", {"task_id": task_id, "role": task.role}, task_id=task_id)
                    resume_session_id, resume_spent = self._resume.pop(task_id, ("", 0.0))
                    spec = self._worker_spec(task, resume_session_id, resume_spent)
                    if spec.max_budget_usd <= 0:
                        # The run has no money left. Spawning anyway is not
                        # neutral: the CLI refuses a zero ceiling and exits 1,
                        # which reads as a crash instead of "out of budget".
                        status = TaskStatus.EXHAUSTED
                        error = "运行预算已用尽，未启动会话"
                        final_payload = {"subtype": "error_max_budget_usd", "is_error": True}
                        self._record_exhausted_handoff(task, final_payload, "")
                        self._finish_task(task_id, task.role, status, error, final_payload, "")
                        return
                    # Reserve the ceiling before the session starts, so a worker
                    # dispatched a moment later sees this one's claim instead of
                    # the untouched remainder. The reservation is in gateway
                    # dollars while the ceiling is in the CLI's, so the inherited
                    # spend (already counted in ``cost_usd``) comes back out
                    # before dividing.
                    factor = self.cfg.models.cli_factor(
                        self.cfg.models.model_for(task.role)
                    )
                    self._committed[task_id] = max(
                        0.0, (spec.max_budget_usd - float(resume_spent) * factor) / factor
                    )
                    # A resumed session reports a cumulative total that already
                    # includes the transcript it inherited; seed the baseline so
                    # only the new spend is charged.
                    self._worker_base[task_id] = max(0.0, resume_spent)
                    try:
                        # Tool calls in flight. A running tool emits no events,
                        # and that silence is not a hang: a worker running gdb
                        # under WSL or replaying a capture can be quiet for
                        # minutes. Observed live: a verifier was killed at 300 s
                        # while its `run_hook.sh` (timeout 420 s) was still
                        # executing, and the flag it was confirming stayed
                        # unverified until a second verifier was paid for.
                        inflight: set[str] = set()
                        # Background work the CLI started on the worker's
                        # behalf. A command that outlives the CLI's 600 s tool
                        # ceiling is moved to the background; the session then
                        # waits on it silently, and that silence is not a hung
                        # model. Observed live: four workers were killed at
                        # exactly 305 s while their own cracks were still
                        # printing progress — resuming each one re-paid for the
                        # analysis it had already done.
                        background: set[str] = set()
                        session_id = ""
                        #: How many times this task's session has been resumed
                        #: after a model stall, and whether the next pass has to
                        #: open a new one. Resuming reuses the transcript, so the
                        #: worker keeps everything it had established.
                        stalls = 0
                        restart = True
                        # A per-task wall clock. Turns and dollars are ceilings,
                        # not clocks: a worker that spends an hour on one
                        # tool-heavy detour has used up neither. Run 15's task
                        # #70 ran 3540 s against a 1800 s limit that was
                        # declared in config and enforced nowhere — it was only
                        # used as the wait timeout for the writer.
                        _timeout_s = self._task_clock(task.role)
                        deadline = time.time() + _timeout_s
                        # Registered before the first event so ``wait_tasks``
                        # can report how much of the task's clock is left even
                        # while the model is still on its opening turn.
                        self._live.setdefault(
                            task_id,
                            {"tools": 0, "last_tool": "", "last_event": time.time()},
                        )["deadline"] = deadline
                        # Inter-event gaps this task has actually shown, and when
                        # the last event landed. A model turn slower than the
                        # stall clock is ordinary work, not a hang: on run 84,
                        # task #164 produced 279 s and 299 s turns with nothing
                        # wrong, right after a 300 s stall had reaped and
                        # re-paid for an earlier one. Turns also lengthen as the
                        # transcript grows, so the clock has to follow them.
                        turn_gaps: list[float] = []
                        last_event_at: float | None = None
                        pass_started = time.time()
                        #: Spawn attempts already spent on this task. A missing
                        #: CLI is usually a short file lock, so the first two
                        #: are retried with backoff instead of failing the task.
                        spawn_retries = 0
                        while True:
                            if restart:
                                stream = self.ctx.backend.run(spec)
                                iterator = stream.__aiter__()
                                inflight.clear()
                                background.clear()
                                restart = False
                                # The silence that just fired is the one thing
                                # that must not count as this task's normal
                                # latency.
                                last_event_at = None
                                pass_started = time.time()
                            waiting_on_tool = bool(inflight)
                            waiting_on_background = bool(background)
                            if waiting_on_tool or waiting_on_background:
                                budget = self.cfg.limits.tool_wait_timeout_s
                                awaiting_model = False
                            else:
                                budget = self._turn_budget(
                                    self.cfg.limits.stall_timeout_s, turn_gaps
                                )
                                awaiting_model = True
                            left = deadline - time.time()
                            if left <= 0:
                                error = (
                                    f"任务超过 {_timeout_s} 秒上限，已中断"
                                )
                                final_payload.setdefault("subtype", "task_timeout")
                                status = TaskStatus.EXHAUSTED
                                break
                            try:
                                event = await asyncio.wait_for(
                                    iterator.__anext__(), timeout=min(budget, left)
                                )
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError:
                                # Whichever clock ran out, the task is over.
                                # Only the stall clock is worth retrying: a task
                                # that has spent its whole wall clock has no room
                                # left for another attempt.
                                if time.time() >= deadline:
                                    error = (
                                        f"任务超过 {_timeout_s} 秒上限，已中断"
                                    )
                                    final_payload.setdefault("subtype", "task_timeout")
                                    status = TaskStatus.EXHAUSTED
                                    break
                                # A stall while no tool is running means the
                                # model's own call went quiet. That is a gateway
                                # hiccup, not a bad transcript: the session has
                                # already done the work, and closing it throws
                                # that away. Resume it and carry on.
                                model_stall = not (waiting_on_tool or waiting_on_background)
                                if (
                                    model_stall
                                    and stalls < self.cfg.limits.stall_retries
                                    and session_id
                                    and not self._stop.is_set()
                                ):
                                    stalls += 1
                                    await _close_stream(stream)
                                    final_payload = {}
                                    spec = replace(
                                        spec,
                                        resume_session_id=session_id,
                                        prompt=prompts.stall_resume_text(),
                                    )
                                    silent = int(
                                        time.time() - (last_event_at or pass_started)
                                    )
                                    # The lead wakes on ``wait_tasks`` and has to
                                    # be able to tell a resumed task that is
                                    # still making turns from one that has now
                                    # stalled twice.
                                    self._live.setdefault(
                                        task_id,
                                        {"tools": 0, "last_tool": "", "last_event": time.time()},
                                    )["stalls"] = stalls
                                    self._emit(
                                        "task.stall_retry",
                                        {
                                            "task_id": task_id,
                                            "attempt": stalls,
                                            "session_id": session_id,
                                            "silent_s": silent,
                                        },
                                        task_id=task_id,
                                    )
                                    restart = True
                                    continue
                                if waiting_on_tool:
                                    what = "工具调用"
                                elif waiting_on_background:
                                    what = f"后台任务（{'、'.join(sorted(background))}）"
                                else:
                                    what = "模型"
                                error = (
                                    f"卡住了：{what} {int(budget)} 秒没有任何事件，已中断"
                                )
                                final_payload.setdefault("subtype", "stall_timeout")
                                status = TaskStatus.EXHAUSTED
                                break
                            now = time.time()
                            if awaiting_model and last_event_at is not None:
                                # Two events with no tool in flight between them
                                # is one model turn. Only the recent past counts:
                                # an early slow stretch must not license a much
                                # longer stall a quarter of an hour later.
                                turn_gaps.append(now - last_event_at)
                                del turn_gaps[:-16]
                            last_event_at = now
                            if event.kind == "tool_use":
                                inflight.add(str(event.payload.get("id") or ""))
                            elif event.kind == "tool_result":
                                inflight.discard(str(event.payload.get("id") or ""))
                            elif event.kind == "init":
                                self._track_background(event.payload, background)
                                # Published so the lead can tell "quiet because
                                # it is waiting on a background job" from
                                # "quiet because it is stuck": that distinction
                                # is what the refusal message tells it to check.
                                self._live.setdefault(
                                    task_id,
                                    {"tools": 0, "last_tool": "", "last_event": time.time()},
                                )["background"] = len(background)
                            if event.kind == "init" and event.payload.get("session_id"):
                                # A session killed mid-work never sends a
                                # result, and the result is where the session id
                                # normally comes from. Without it the task is
                                # EXHAUSTED but not resumable, so the transcript
                                # that already did the work is unreachable.
                                session_id = str(event.payload["session_id"])
                            self._record_event(event, task_id, task.role)
                            # Two waste guards, both about *repetition* rather
                            # than silence. An earlier version counted calls
                            # since the last record_fact; the two most expensive
                            # sessions turned out to be 188/129 *distinct*
                            # operations (16-18% repetition) — real analysis of
                            # an obfuscated binary, not spinning — and that
                            # guard would have cut them short.
                            if event.kind == "tool_use" and task.role not in CLOSING_ROLES:
                                payload = event.payload or {}
                                sig = (
                                    str(payload.get("tool") or ""),
                                    json.dumps(payload.get("input") or {},
                                               ensure_ascii=False, sort_keys=True)[:400],
                                )
                                if sig == self._last_call.get(task_id):
                                    self._repeats[task_id] = self._repeats.get(task_id, 0) + 1
                                else:
                                    self._last_call[task_id] = sig
                                    self._repeats[task_id] = 1
                                repeat_limit = self.cfg.limits.repeat_call_limit
                                stuck = (
                                    repeat_limit > 0
                                    and self._repeats[task_id] >= repeat_limit
                                )
                                idle_limit = self.cfg.limits.no_progress_tools
                                idle = False
                                if idle_limit > 0 and task.role != "triage":
                                    made = int((self._live.get(task_id) or {}).get("tools") or 0)
                                    idle = made - self._tools_at_fact.get(task_id, 0) >= idle_limit
                                if stuck or idle:
                                    error = (
                                        f"同一条工具调用连续重复了 {self._repeats[task_id]} 次，已停止"
                                        if stuck
                                        else "连续多次工具调用没有产生任何新结论，已停止"
                                    )
                                    final_payload.setdefault(
                                        "subtype", "repeat_loop" if stuck else "no_progress"
                                    )
                                    status = TaskStatus.EXHAUSTED
                                    break
                            if event.kind == "error":
                                # The backend yields this when the session's
                                # stream threw. Nothing follows it, so the loop
                                # must close the task here — otherwise it fell
                                # through to StopAsyncIteration and the task was
                                # recorded FAILED with an empty error column.
                                raw = str(event.payload.get("error") or "")
                                hint = self._note_infra_failure(raw)
                                # A CLI that is *sometimes* unfindable is a file
                                # lock (the 335 MB binary being scanned or
                                # replaced), not a broken install: a retry two
                                # seconds later spawns fine. Measured 2026-10-31
                                # — two pwn sessions died at 05:54 and 05:57
                                # while a probe worker at 05:59 came up normally.
                                # Three attempts turn that into a delay instead
                                # of a dead task.
                                if hint and spawn_retries < 3 and not self._stop.is_set():
                                    spawn_retries += 1
                                    await _close_stream(stream)
                                    await asyncio.sleep(2.0 * spawn_retries)
                                    restart = True
                                    continue
                                error = hint or raw or "会话异常结束"
                                status = TaskStatus.FAILED
                                final_payload = {}
                                last_text = ""
                                break
                            if event.kind == "auth_error":
                                # Same failure the lead would hit: no credential
                                # reaches the child at all. The CLI's own text
                                # ("Not logged in · Please run /login") must not
                                # become the task's summary — it names a fix that
                                # does not exist here. Close the task as a
                                # failure with the actionable line instead.
                                status = TaskStatus.FAILED
                                error = auth_error_message(
                                    self.cfg, str(event.payload.get("error") or "")
                                )
                                final_payload = {}
                                last_text = ""
                                break
                            if event.kind == "text":
                                last_text = str(event.payload.get("text") or last_text)
                            elif event.kind == "result":
                                final_payload = event.payload
                                session_id = str(final_payload.get("session_id") or session_id)
                                status = self._status_from_result(event.payload)
                                outage = self._note_gateway_failure(
                                    task, final_payload, str(last_text or "")
                                )
                                if outage:
                                    # A 503/model_not_found result is the endpoint
                                    # talking, not the task. Recorded with its
                                    # text so the lead and the operator can see
                                    # what it was, and counted so the next
                                    # dispatch is refused instead of paid for.
                                    status = TaskStatus.FAILED
                                    error = outage
                                    final_payload["subtype"] = "gateway_error"
                                # A result is the SDK's terminal message: what
                                # follows is teardown, not work. Observed live
                                # (run 88's verifier): the CLI emitted
                                # `subtype=success` and then did not exit — the
                                # async-for below waited 18 minutes for a stream
                                # that had nothing left to say, freezing a
                                # finished task on `running`, and inside the
                                # bench runner it ate the whole wall clock.
                                # Close the stream (bounded) and finish now;
                                # the EXHAUSTED wrap-up below still runs and
                                # resumes its own stream when it applies.
                                await _close_stream(stream)
                                break
                            if self._stop.is_set() and task_id not in self._post_stop_ok:
                                status = TaskStatus.STOPPED
                                break
                        if session_id:
                            # Kept out of the result payload: that payload is
                            # what the CLI reported, and this is bookkeeping.
                            final_payload["session_id"] = session_id
                        if status == TaskStatus.EXHAUSTED and not self._stop.is_set():
                            subtype = str(final_payload.get("subtype") or "")
                            if subtype.startswith("error_max_turns"):
                                # The transcript is intact and the run still has
                                # money; only the turn counter ran out. A few more
                                # turns are enough to write down the conclusion,
                                # and they are cheaper than the next worker
                                # rediscovering the same ground from scratch.
                                tail = await self._wrap_up_after_ceiling(task, session_id)
                                if tail:
                                    last_text = tail
                            # The task ran out of money or turns mid-work. No
                            # extra model turn: the session is already over
                            # budget, and resuming it would reload its whole
                            # transcript to write a summary the orchestrator can
                            # assemble from what the task already recorded.
                            self._record_exhausted_handoff(task, final_payload, last_text)
                    except asyncio.CancelledError:
                        # The run is closing (operator stop, clock, launcher
                        # crash). The session ends mid-turn, but what it already
                        # said is still the most valuable thing it produced.
                        # Observed live: a verifier had written "我已用独立的
                        # Python 实现复现出明文" and was still checking when the
                        # run clock cancelled it; it was recorded as FAILED with
                        # an empty result, its summary was overwritten with {},
                        # and the run closed reporting "验证任务没有给出结论".
                        status = TaskStatus.STOPPED
                        raise
                    finally:
                        self._finish_task(
                            task_id, task.role, status, error, final_payload, last_text
                        )
        except asyncio.CancelledError:
            current = self.store.get_task(task_id)
            if current is not None and current.status in (
                TaskStatus.QUEUED, TaskStatus.RUNNING
            ):
                # Cancelled before the session loop was entered (waiting for a
                # pool slot or a worker slot): nothing was recorded, so this is
                # the only place that can close the task.
                self.store.finish_task(task_id, TaskStatus.STOPPED, {}, "运行已结束")
                self._emit(
                    "task.finished",
                    {"task_id": task_id, "role": task.role, "status": TaskStatus.STOPPED},
                    task_id=task_id,
                )
        except Exception as exc:  # noqa: BLE001
            self.store.finish_task(task_id, TaskStatus.FAILED, {}, f"{type(exc).__name__}: {exc}")
            self._emit(
                "task.finished",
                {"task_id": task_id, "role": task.role, "status": TaskStatus.FAILED,
                 "error": str(exc)},
                task_id=task_id,
            )
        finally:
            # Release the reservation: whatever this task did not spend has to
            # become available to the next dispatch.
            self._committed.pop(task_id, None)
            self._worker_base.pop(task_id, None)
            # The board's ledger books real spend and releases the unspent
            # hold. Reads the task row, not ``_worker_base``: the cost was
            # written there as it was priced, and a task that never started
            # has zero.
            budget = self._board_budget
            if budget is not None and task_id in budget.holds:
                spent = self.store.get_task_cost(task_id)
                budget.settle(task_id, spent)
            event = self._done.get(task_id)
            if event:
                event.set()

    async def _wrap_up_after_ceiling(self, task: Any, session_id: str) -> str:
        """Give a worker that hit its turn ceiling a few turns to say what it found.

        Observed live: two reverse workers in one run stopped at exactly 151
        turns. Each had already reached a real conclusion — one had located the
        custom VM, the other had reconstructed the key schedule — but the
        session ended mid-sentence, so the run's card and the next worker both
        lost it. Turns, unlike budget, are cheap to extend: the transcript is
        already warm and only the conclusion is missing.

        Returns the last text the wrap-up produced, or ``""`` when it could not
        run. It never raises: this is a courtesy turn, and the hand-off below is
        the path that must survive.
        """
        limits = self.cfg.limits
        if not session_id or task.id in self._wrapped or self._stop.is_set():
            return ""
        if not limits.task_wrapup_turns:
            return ""
        remaining = limits.run_budget_usd - self.cost_usd
        if remaining <= 0.05:
            return ""
        self._wrapped.add(task.id)
        spent = self.store.get_task_cost(task.id)
        # A resumed invocation is charged from the transcript's running total,
        # so the wrap-up ceiling has to include what the session already spent.
        # ``remaining`` and ``spent`` are gateway dollars while the configured
        # grant is in the CLI's: compare in one unit, hand back in the other.
        factor = self.cfg.models.cli_factor(self.cfg.models.model_for(task.role))
        grant_real = min(limits.task_wrapup_budget_usd / factor, remaining)
        ceiling = grant_real * factor + spent * factor
        try:
            base_spec = self._worker_spec(task, session_id, spent)
            spec = replace(
                base_spec,
                max_turns=limits.task_wrapup_turns,
                max_budget_usd=ceiling,
                # The system prompt states the task's own ceiling. This turn is
                # much shorter than that, and a worker pacing itself against the
                # wrong number would spend its last turns exploring. Both units,
                # because ``budget_block`` above now shows both and a correction
                # in one of them alone would read as a contradiction.
                system_prompt=base_spec.system_prompt + (
                    f"\n# 更正\n这是一次收尾会话，只有 {limits.task_wrapup_turns} 轮、"
                    f"真实花费约 ${grant_real:.2f}（CLI 口径 ${grant_real * factor:.2f}），"
                    f"不是上文写的额度。用它把结论落下来。\n"
                ),
                prompt=(
                    "你撞上了本轮会话的轮数上限。不要再做新的调查，也不要启动长命令。"
                    "用这几轮把已经确定的东西写下来：调用 task_complete，"
                    "summary 写清楚做到哪一步、确认了什么、还差什么、下一步该怎么做；"
                    "还没记录到黑板上的关键结论先用 record_fact 记下来。"
                ),
            )
            tail = ""
            # Bounded on purpose. A courtesy turn that hangs is worse than no
            # courtesy turn: it holds a session slot, keeps the task open, and
            # the orchestrator is already past the point where this worker
            # mattered. The plain ``async for`` here had no clock at all.
            stream = self.ctx.backend.run(spec)
            iterator = stream.__aiter__()
            budget = self.cfg.limits.stall_timeout_s
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(iterator.__anext__(), timeout=budget)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        self._emit(
                            "task.wrapup_timeout",
                            {"task_id": task.id, "silent_s": budget},
                            task_id=task.id,
                        )
                        break
                    self._record_event(event, task.id, task.role)
                    if event.kind == "text":
                        tail = str(event.payload.get("text") or tail)
                    if self._stop.is_set():
                        break
            finally:
                await _close_stream(stream)
            self._emit(
                "task.wrapup",
                {"task_id": task.id, "role": task.role, "chars": len(tail)},
                task_id=task.id,
            )
            return tail.strip()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the hand-off is the fallback
            return ""

    def _record_exhausted_handoff(
        self, task: Any, final_payload: dict[str, Any], last_text: str
    ) -> None:
        """Turn a task that ran out of budget into a hand-off, not a dead end.

        What the worker wrote with ``record_fact`` is already on the blackboard,
        so the next worker inherits it. What is missing is *why it stopped* and
        *where it got to*: without that the lead re-dispatches the same
        direction and pays for the same dead end again.

        ``_finish_task`` records the session id, which is what
        ``dispatch(resume_task_id=...)`` needs to continue this transcript.
        """
        subtype = str(final_payload.get("subtype") or "")
        spent = self.store.get_task_cost(task.id)
        # Name the actual ceiling. The lead reads this fact and decides what to
        # do next; telling it "turns" when the wall clock ran out sends it
        # looking for more turns instead of a narrower approach.
        if subtype == "stall_timeout":
            reason = "无事件超时"
        elif subtype == "task_timeout":
            reason = "任务时长"
        elif subtype == "no_progress":
            reason = "空转"
        elif subtype == "repeat_loop":
            reason = "重复调用"
        else:
            reason = "预算" if "budget" in subtype else "轮数"
        tail = (last_text or str(final_payload.get("result") or "")).strip()
        lines = [
            f"工单 #{task.id}（{task.role}）撞上{reason}上限，花费 ${spent:.2f} 后停止。",
            f"停止原因：{subtype or 'unknown'}。",
        ]
        if tail:
            lines.append(f"停止前最后一段：{tail[-800:]}")
        lines.append(
            f"续做时不要重复它的路线；先读它写在黑板上的结论，从它停下的地方继续。"
            f"派后续工单时带上 resume_task_id={task.id} 可以直接接上它的会话。"
        )
        self.record_fact(
            task.id,
            FactKind.OBSERVATION,
            "\n".join(lines),
            {"exhausted": subtype, "cost_usd": round(spent, 4), "system": True},
            "observed",
        )

    def _finish_task(
        self,
        task_id: int,
        role: str,
        status: str,
        error: str,
        final_payload: dict[str, Any],
        last_text: str,
    ) -> None:
        """Close a task exactly once, preferring a result the worker wrote itself."""
        if self.store.get_task(task_id) is None:
            return
        if status == TaskStatus.RUNNING:
            status = TaskStatus.DONE
        explicit = self._results.pop(task_id, None)
        if explicit:
            # A ceiling outranks the worker's own verdict. Observed live: a
            # worker called task_complete(done) and *then* hit its budget, so
            # the explicit "done" overwrote EXHAUSTED — the task looked
            # finished, got no hand-off, and was not resumable, while its
            # findings stopped mid-investigation.
            if status != TaskStatus.EXHAUSTED:
                status = explicit.get("status") or status
            summary = str(explicit.get("summary") or last_text)
        else:
            summary = str(final_payload.get("result") or last_text)
        if status == TaskStatus.FAILED and not error:
            # Six tasks in runs 83/84 were closed FAILED with an empty error
            # column while their whole result was the gateway's 503 text, so
            # ``list_tasks`` showed a bare "失败" with nothing to act on.
            error = (last_text or str(final_payload.get("result") or "")).strip()[:300]
        live = self._live.get(task_id) or {}
        result = {
            "summary": summary[:8000],
            "subtype": final_payload.get("subtype", ""),
            "findings": [f.content for f in self.store.list_facts(self.id, task_id=task_id)],
            "artifacts": self._list_artifacts(),
            "cost_usd": self.store.get_task_cost(task_id),
            # A task closed without a result event books no turns and no cost —
            # which is exactly the case where it may have done the most work.
            # Run 15's four stalled reverse tasks made 16-66 tool calls each and
            # were recorded as ``turns=0, $0``, so the dashboard showed four
            # sessions that had done nothing. The tool count says otherwise.
            "tools_called": int(live.get("tools") or 0),
            "next_steps": (explicit or {}).get("next_steps", []),
            "verdict": (explicit or {}).get("verdict", ""),
        }
        session_id = str(final_payload.get("session_id") or "")
        self.store.finish_task(task_id, status, result, error, session_id=session_id)
        # The task is over, so nothing is "running" in it any more. Left in
        # place, a stale entry would keep ``live`` (and its background count)
        # on every later ``wait_tasks`` for a task that has already ended.
        self._live.pop(task_id, None)
        self._lead_seen.pop(task_id, None)
        self._emit(
            "task.finished",
            {"task_id": task_id, "role": role, "status": status, "error": error,
             "summary": result["summary"][:1200]},
            task_id=task_id,
        )
        if task_id in self._patrol_tasks:
            # A patrol's text is a judgement about the run, not findings about
            # the challenge: it must not set the category or seed flag scanning.
            self._patrol_tasks.discard(task_id)
            action, target, reason = patrol.parse_verdict(result["summary"])
            refused = ""
            if action == "CLOSE" and target is not None:
                # The one place a patrol may change the board. It is still not a
                # refutation: a refutation needs a worker's observation, and an
                # abandoned entry carries none. The guard lives in
                # ``patrol.apply_close`` so it is testable without a session.
                refused = patrol.apply_close(self.store, self.id, target)
                if not refused:
                    self._emit(
                        "hypothesis.abandoned",
                        {"hypothesis_id": target, "by": "patrol", "reason": reason},
                        task_id=task_id,
                    )
            self._emit(
                "patrol.verdict",
                {"action": action, "target_task_id": target, "reason": reason,
                 "status": str(status), "refused": refused},
                task_id=task_id,
            )
            return
        self._score_progress(task_id, role, status, result)
        # The board entry this task was holding is closed by what the task
        # *did*, not by the fact that it stopped. Runs in every mode: a task the
        # lead dispatched with ``hypothesis_id`` has the same lease to settle.
        self._settle_hypothesis(task_id, status)
        if role == "verifier":
            # A verifier's report deliberately quotes negative samples
            # (``flag{wrong}`` and near-misses) to show what it ruled out.
            # Scanning it for flags turns those into candidates, and the
            # near-miss then outranks the real value in any "latest
            # candidate" lookup. Its flag claims arrive via submit_flag only.
            self._apply_verdict(task_id, result["summary"], result.get("verdict", ""))
        elif role == "triage":
            self._apply_triage_category(task_id, result["findings"], result["summary"])
            if status != TaskStatus.STOPPED:
                self._scan_for_flags(task_id, result["summary"])
        elif status != TaskStatus.STOPPED:
            # A cancelled task's text is a fragment, not a report: it may be
            # quoting bait it was about to rule out.
            self._scan_for_flags(task_id, result["summary"])

    def _score_progress(self, task_id: int, role: str, status: str, result: dict[str, Any]) -> None:
        """Score what one finished task contributed, then look for a stalled run.

        Deterministic and free. The reason this exists at the run level: a run
        can lose its way without any single task failing — workers keep
        reporting conclusions, the blackboard stops moving, and every per-task
        guard is happy because each task ended "done".

        ``observe`` records the score and the trigger but never dispatches, so
        the correlation between "three quiet tasks" and "the run fails" can be
        measured before anything acts on it.
        """
        mode = str(getattr(self.cfg.limits, "patrol", "observe") or "observe").strip().lower()
        if mode not in ("observe", "act"):
            return
        self._sweep_hypotheses()
        row = self.store.get_task(task_id)
        trace = patrol.TaskTrace(
            task_id=task_id,
            role=role,
            status=str(status),
            objective=str(getattr(row, "objective", "") or "")[:120],
            facts_added=len(result.get("findings") or []),
            new_outputs=len(self._outputs.pop(task_id, ())),
            # One tool call is roughly one model round trip, which is the unit
            # the stall report is phrased in.
            turns=int(result.get("tools_called") or 0),
        )
        self._traces.append(trace)
        self._emit(
            "patrol.score",
            {"task_id": task_id, "role": role, "score": trace.score,
             "facts": trace.facts_added, "outputs": trace.new_outputs,
             "turns": trace.turns, "mode": mode},
            task_id=task_id,
        )
        if not self.accepting_work:
            return
        trigger = patrol.evaluate(
            traces=self._traces,
            has_candidate_flag=bool(self.store.list_flags(self.id)),
            state=self._patrol,
            unworked=self._unworked_hypotheses(),
        )
        if trigger is None:
            return
        self._emit(
            "patrol.trigger",
            {"reason": trigger.reason, "detail": trigger.detail,
             "task_ids": list(trigger.task_ids), "severity": trigger.severity,
             "mode": mode},
        )
        if mode != "act":
            return
        self._patrol.dispatched += 1
        self._patrol.last_reason = trigger.reason
        # Two id namespaces, two sets. ``trigger.task_ids`` holds board ids for
        # ``unworked_hypothesis`` and task ids for every other reason; putting
        # them in one set let a hypothesis id mark an unrelated task as already
        # looked at, and vice versa.
        if trigger.reason == patrol.UNWORKED:
            self._patrol.covered_hypotheses.update(trigger.task_ids)
        else:
            self._patrol.covered.update(trigger.task_ids)
        try:
            asyncio.get_running_loop().create_task(self._spawn_patrol(trigger))
        except RuntimeError:
            # No loop (a synchronous caller in a test): undo the reservation so
            # the budget is not spent on a patrol that never ran.
            self._patrol.dispatched -= 1

    def _unworked_hypotheses(self) -> tuple[tuple[int, str, float, str], ...]:
        """Board entries still ``open``, with how long they have waited.

        The design doc has carried this as the last unimplemented patrol target
        since B phase landed: the patrol only ever looked at *task* traces, so
        the one state this mode creates — a direction nothing will start — was
        invisible to it. Empty in off mode (there is no board), and empty when
        the store cannot age a row: an unmeasured age must not become "zero,
        keep waiting" or "infinite, patrol now".
        """
        if not self.swarm_enabled:
            return ()
        ages = self.store.hypothesis_ages(self.id)
        return tuple(
            (
                h.id,
                h.family,
                ages[h.id],
                h.statement,
            )
            for h in self.store.list_hypotheses(self.id, status=swarm.OPEN)
            if h.id in ages
        )

    async def _spawn_patrol(self, trigger: patrol.Trigger) -> None:
        digest = self.blackboard_digest(limit=self.cfg.limits.blackboard_facts)
        objective = patrol.mark(patrol.build_prompt(trigger, digest, self._traces))
        try:
            task = await self.dispatch(
                "triage",
                objective=objective,
                context="巡检回合：只给结论，不派工。",
            )
        except Exception as exc:  # noqa: BLE001
            self._emit("patrol.error", {"error": f"{type(exc).__name__}: {exc}"})
            return
        self._patrol_tasks.add(task.id)
        self._emit(
            "patrol.dispatched",
            {"task_id": task.id, "reason": trigger.reason, "trigger": trigger.detail},
            task_id=task.id,
        )

    def _apply_triage_category(
        self, task_id: int, findings: list[str], summary: str
    ) -> None:
        """Persist the category triage concluded, on the row and on the run.

        Triage announces its verdict as a ``category=<cat>`` finding, but that
        conclusion used to live only on this run's blackboard:
        ``challenges.category`` stayed ``unknown``, so ``caselib``'s strongest
        retrieval signal (same category, +3 and an admission gate) never
        fired, and every card distilled from the run was filed as ``unknown``
        too — 12 of the 14 cards in the live library. The in-memory
        ``challenge`` matters as much as the row: ``caselib.distill`` reads it
        when the run closes, so only fixing the database left the cards wrong.

        The same moment also normalizes a pasted-in blob title (「web ·
        host:port · 短语」): the run header, the task rail and the browser tab
        were all rendering the raw first line of the challenge prose. The
        rename runs whenever the direction is known — including one the
        platform already set — because a URL title is unreadable regardless
        of who classified it.
        """
        category = _triage_category(findings, summary)
        if category and (self.challenge.category or "").lower() in ("", "unknown"):
            if self.store.set_challenge_category(self.challenge.id, category):
                previous = self.challenge.category
                self.challenge.category = category
                self._emit(
                    "challenge.categorized",
                    {"category": category, "previous": previous, "task_id": task_id},
                    task_id=task_id,
                )
        # A triage task that was stopped or stalled before concluding has
        # nothing to say; without a direction there is nothing to name either.
        self._maybe_auto_rename(task_id)

    def _maybe_auto_rename(self, task_id: int) -> None:
        """Swap a pasted blob title for 「方向 · 主机 · 短语」, once.

        Guarded three ways: the name must look like a blob (URL or over 40
        chars — ``rename_challenge`` re-checks in SQL), a direction must be
        known (a triage that concluded nothing names nothing), and the
        composed title must actually differ. All three fail closed to
        "leave the challenge alone".
        """
        name = self.challenge.name or ""
        if "://" not in name and len(name) <= _BLOB_NAME_CHARS:
            return
        # A title already in the normalized shape is final — without this a
        # 46-char normalized title (long host, long phrase) would read as a
        # blob again and be re-composed on every later triage.
        if any(name.startswith(d + " · ") for d in _DIRECTIONS):
            return
        category = (self.challenge.category or "").lower()
        if not category or category == "unknown":
            return
        new_name = _auto_title(
            category, name, self.challenge.description or "", self.challenge.remote or ""
        )
        if not new_name or new_name == name:
            return
        if not self.store.rename_challenge(self.challenge.id, new_name):
            return
        self.challenge.name = new_name
        self._emit(
            "challenge.renamed",
            {"from": name, "to": new_name, "task_id": task_id},
            task_id=task_id,
        )

    def complete_task(
        self,
        task_id: int,
        status: str = "done",
        summary: str = "",
        findings: list[str] | None = None,
        artifacts: list[str] | None = None,
        next_steps: list[str] | None = None,
        verdict: str = "",
    ) -> None:
        """Called by the worker's ``task_complete`` tool.

        The task is not closed here: the session is still running and closing it
        under the SDK would lose the remaining events. The result is recorded and
        applied when the session ends; ``stop_after`` makes the loop stop early.
        """
        status = status if status in (TaskStatus.DONE, TaskStatus.BLOCKED) else TaskStatus.DONE
        verdict = str(verdict or "").strip().lower()
        self._results[task_id] = {
            "status": status,
            "summary": summary,
            "findings": findings or [],
            "artifacts": artifacts or [],
            "next_steps": next_steps or [],
            # Only the verifier has one, and only a recognised value is kept:
            # a free-text guess here would be applied to the flag as a verdict.
            "verdict": verdict if verdict in VERDICTS else "",
        }
        self._emit(
            "task.result",
            {"task_id": task_id, "status": status, "summary": summary[:1200],
             "findings": findings or [], "next_steps": next_steps or []},
            task_id=task_id,
        )
        # Findings usually repeat facts the worker already wrote with
        # ``record_fact`` (observed live: 36 of 36 across three tasks). Copying
        # them onto the blackboard doubles every entry and, worse, drops the
        # ``basis`` the worker set — the copy is an ungrounded INSIGHT, so the
        # next worker sees a verified-looking claim where the original said
        # "inferred". ``record_fact`` drops restatements; only findings that say
        # something new are written.
        for item in findings or []:
            text = item.strip()
            if text:
                self.record_fact(
                    task_id, FactKind.INSIGHT, text, {"reported_by": "task_complete"}
                )

    def worker_help(self, task_id: int, question: str) -> dict[str, Any]:
        """A worker asking the lead agent for something.

        Deliberately non-blocking: a worker that parks waiting for an answer
        burns a session slot and its own context. The question lands on the
        blackboard, the worker writes down what it has, and the lead agent
        answers by dispatching the next task.
        """
        fact_id = self.record_fact(
            task_id, FactKind.OBSERVATION, f"工单 #{task_id} 请求协助：{question}",
            {"kind": "help_request"},
        )
        self._emit("task.help_requested", {"task_id": task_id, "question": question}, task_id=task_id)
        return {
            "notified": True,
            "fact_id": fact_id,
            "note": (
                "主 agent 会在黑板上看到这条。不要干等：先把能做的做完，"
                "调用 task_complete 写上 next_steps，主 agent 会派后续任务。"
            ),
        }

    def _status_from_result(self, payload: dict[str, Any]) -> str:
        subtype = str(payload.get("subtype") or "")
        # Ceilings first: the CLI marks every one of them is_error, so testing
        # is_error first classified budget-exhausted workers as crashes — and a
        # crash gets no hand-off and is not resumable.
        if subtype.startswith("error_max_turns") or subtype.startswith("error_max_budget"):
            return TaskStatus.EXHAUSTED
        if payload.get("is_error"):
            return TaskStatus.FAILED
        if subtype.startswith("error"):
            return TaskStatus.BLOCKED
        return TaskStatus.DONE

    async def wait(self, task_ids: Iterable[int], timeout_s: int = 600) -> list[dict[str, Any]]:
        ids = [int(i) for i in task_ids]
        events = [self._done[i] for i in ids if i in self._done]
        if events:
            # Wake on the first completion, not after all of them. The lead's
            # next decision depends on whichever task changed; a wait that
            # slept to its timeout while one task had already finished (the
            # old gather-all semantics) locked the reaction behind the polling
            # boundary — measured 43–65 wait_tasks calls per run, each one a
            # full model turn on a transcript that keeps growing.
            pending = [asyncio.ensure_future(event.wait()) for event in events]
            try:
                await asyncio.wait_for(
                    asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                pass
            finally:
                for fut in pending:
                    fut.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        out = []
        for task_id in ids:
            task = self.store.get_task(task_id)
            if task is None:
                continue
            out.append(
                {
                    "task_id": task.id,
                    "role": task.role,
                    "status": task.status,
                    "terminal": task.terminal,
                    "result": self._wait_result(task),
                    "error": task.error,
                    "live": self._pulse(task_id),
                }
            )
        return out

    def _wait_result(self, task: Any) -> dict[str, Any]:
        """The task's result, minus what the blackboard already holds.

        ``findings`` are the same strings ``complete_task`` wrote to the
        blackboard, so returning them here paid for them twice: observed live,
        8237 of task #23's 11115 result bytes were findings already on the
        board. ``artifacts`` was worse in proportion — 200 names per task, most
        of them inputs. The full record stays reachable through
        ``get_task_result``.
        """
        result = dict(task.result or {})
        findings = result.pop("findings", None)
        if findings:
            result["findings_note"] = (
                f"{len(findings)} 条 findings 已写入黑板（record_fact），"
                f"用 read_blackboard 或 get_task_result(#{task.id}) 查看"
            )
        artifacts = result.get("artifacts")
        if isinstance(artifacts, list) and len(artifacts) > 30:
            result["artifacts"] = artifacts[:30]
            result["artifacts_note"] = f"另有 {len(artifacts) - 30} 个文件未列出"
        return result

    def _track_background(self, payload: dict[str, Any], live: set[str]) -> None:
        """Keep ``live`` in step with the CLI's background task set.

        A worker that runs a long crack is not idle: the CLI moves the command
        to the background past its 600 s ceiling and the session then waits on
        it without emitting anything. The orchestrator has to know those tasks
        exist, or that wait looks exactly like a hung model — observed live in
        run 15, where four workers were cut off at 305 s of silence while their
        own background jobs were still printing progress.

        Three signals feed the set:

        * ``task_started`` — a task was registered. ``is_backgrounded=False``
          means the spawning tool call is blocking on it, which ``inflight``
          already covers; a task that says nothing is treated as background,
          because the cost of being wrong that way is one long clock, while
          the cost of the other way is a killed worker.
        * ``task_updated`` — a status change; terminal ones leave the set, and
          ``is_backgrounded=True`` adds a task that was moved off the
          foreground mid-run.
        * ``background_tasks_changed`` — a *level* signal with REPLACE
          semantics: the payload is every live task after the change, so the
          set is swapped for it rather than merged.
        """
        subtype = str(payload.get("subtype") or "")
        if subtype == "background_tasks_changed":
            tasks = payload.get("tasks")
            if isinstance(tasks, list):
                live.clear()
                live.update(
                    str(t.get("task_id")) for t in tasks
                    if isinstance(t, dict) and t.get("task_id")
                )
            return
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            return
        backgrounded = payload.get("is_backgrounded")
        if subtype == "task_started":
            if backgrounded is not False:
                live.add(task_id)
        elif subtype in ("task_notification", "task_updated"):
            if str(payload.get("status") or "") in _BACKGROUND_DONE:
                live.discard(task_id)
            elif backgrounded is True:
                live.add(task_id)

    def _turn_budget(self, base: float, gaps: list[float]) -> float:
        """How long a silent model turn may run before it counts as a stall.

        ``stall_timeout_s`` is a constant; turn latency is not. It differs by
        model by 3-4x and grows with the transcript, and its slowest ordinary
        turns land right on the constant — measured on run 84, task #164 ran
        279 s and 299 s turns with nothing wrong, immediately after a 300 s
        stall had reaped one and made it pay for the resumed transcript. A turn
        already observed taking ``p90`` is this task's normal, so allow two of
        them, capped by ``tool_wait_timeout_s`` so a dead gateway is still
        caught inside a quarter of an hour. It only ever loosens: a fast model
        keeps exactly the configured clock.
        """
        if len(gaps) < 3:
            return float(base)
        ordered = sorted(gaps)
        p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
        return max(float(base), min(p90 * 2.0, float(self.cfg.limits.tool_wait_timeout_s)))

    def _pulse(self, task_id: int) -> dict[str, Any]:
        """``live_progress`` plus what changed since the lead last looked.

        A bare ``tools: 42`` answers nothing on the second read: the same 42
        and a frozen worker look identical. ``new_tools`` against the previous
        ``wait_tasks`` is the delta the lead actually decides on, and it is
        kept out of ``live_progress`` so the page's activity strip cannot
        consume the lead's snapshot and zero its next delta.
        """
        now = time.time()
        out = self.live_progress(task_id)
        if not out:
            return out
        prev = self._lead_seen.get(task_id)
        if prev is not None:
            out["new_tools"] = int(out["tools"]) - int(prev["tools"])
            out["checked_ago_s"] = int(now - float(prev["at"]))
        self._lead_seen[task_id] = {"tools": int(out["tools"]), "at": now}
        return out

    def live_progress(self, task_id: int) -> dict[str, Any]:
        """What a running task has actually done, since its cost books at the end.

        ``tasks.cost_usd`` only moves when the session emits its result event,
        so a running task reads $0.00 for its entire life. The lead saw that and
        could not tell a live worker from a dead one.
        """
        live = self._live.get(task_id) or {}
        if not live:
            return {}
        out = {
            "tools": live.get("tools", 0),
            "last_tool": live.get("last_tool", ""),
            "silent_s": int(time.time() - float(live.get("last_event") or time.time())),
        }
        if live.get("background"):
            # Silence from a task that has a background job running is work in
            # progress. Without this the lead sees the same ``silent_s`` for a
            # 40-minute extraction and a dead session, and hedges with a second
            # worker — the one response that is always wasted.
            out["background"] = live["background"]
        if live.get("stalls"):
            # A resumed task carries the count with it: the second silence on
            # the same task means the gateway, not the transcript.
            out["stalls"] = int(live["stalls"])
        deadline = float(live.get("deadline") or 0.0)
        if deadline:
            # Without this the lead cannot tell "quiet for 4 minutes out of 30"
            # from "quiet for 4 minutes out of 31", which is what decides
            # whether to keep waiting or to consolidate.
            out["clock_left_s"] = max(0, int(deadline - time.time()))
        return out

    # ------------------------------------------------------------- blackboard
    def _hypotheses_text(self, *, for_main: bool = False, limit: int = 24) -> str:
        """The board's open directions, rendered for whoever is reading.

        Two readers, two questions. A worker needs to know which direction is
        still free before it posts a duplicate; the lead needs to know what is
        live, what came back refuted, and what nothing has picked up. Both are
        shown the same facts — only the framing differs — because the board is
        the only thing they share.
        """
        all_rows = self.store.list_hypotheses(self.id)
        rows = [h for h in all_rows if h.viable()][-limit:]
        # Closed directions stay visible on purpose. Filtering them out (the old
        # behaviour) made the board look silent exactly where it had a verdict,
        # so the lead re-posted the same idea, the post silently merged into the
        # refuted row, and the direction was dispatched again — a refutation that
        # cost a worker and changed nothing. A closed row is information, not
        # noise: it is the list of things not to spend the next worker on.
        closed = [h for h in all_rows if not h.viable()][-CLOSED_DIRECTIONS_LIMIT:]
        if not rows and not closed and not for_main:
            return ""
        state = swarm.convergence(
            self.store.list_hypotheses(self.id),
            has_candidate_flag=bool(self.store.list_flags(self.id)),
            # Any live worker makes the board read *working*: work in a
            # direction nobody wrote down still occupies the run, and telling a
            # worker "nobody is on this" while the lead's own task holds the
            # pool is how a second front gets opened.
            running=self._total_workers_running() > 0,
        )
        state_label = {
            "working": "有 worker 在做",
            "postable": "有人可以做，等认领",
            "needs_input": "等操作员确认",
            "idle": "板上没有活",
        }.get(state, state)
        lines = [f"## 假设板（当前：{state_label}）"]
        if not rows and not closed:
            lines.append("- （空。空板是一个结果，不是再挂一批的理由）")
            return "\n".join(lines)
        label = {
            swarm.OPEN: "待认领",
            swarm.CLAIMED: "在做",
            swarm.SUPPORTED: "已证实",
            swarm.REFUTED: "已证伪",
            swarm.ABANDONED: "已放弃",
        }
        for h in rows:
            who = f" ← 工单#{h.claimed_by}" if h.claimed_by else ""
            tally = ""
            if h.supports or h.refutes:
                tally = f"（证实 {h.supports} / 证伪 {h.refutes}）"
            lines.append(
                f"- #{h.id} [{label.get(h.status, h.status)}] {h.family}{tally}{who} "
                f"{h.statement}"
            )
            if for_main:
                lines.append(f"  证伪判据：{h.criterion}")
        if not rows:
            lines.append("- （没有开放方向。开放方向为空不是再挂一批的理由）")
        if closed:
            lines.append("已关闭方向（不要重派、不要换个说法再挂；要重走必须带新证据）：")
            for h in closed:
                lines.append(
                    f"- #{h.id} [{label.get(h.status, h.status)}] {h.family} "
                    f"（证实 {h.supports} / 证伪 {h.refutes}）{h.statement}"
                )
        return "\n".join(lines)

    def post_hypothesis(
        self,
        *,
        family: str,
        statement: str,
        criterion: str,
        evidence: str,
        origin: str = "lead",
        basis: str = "inferred",
        priority: int = 0,
    ) -> dict[str, Any]:
        """Put a falsifiable direction on the board.

        Swarm rollout, phase A: the lead still dispatches, but what the run
        *believed* now has a place to live that is not the objective text. Two
        workers chasing the same idea are only detectable if the idea was
        written down before either started.
        """
        h = swarm.Hypothesis(
            family=str(family or "").strip(),
            statement=str(statement or "").strip(),
            criterion=str(criterion or "").strip(),
            evidence=str(evidence or "").strip(),
            origin=origin,
            basis=basis if basis in BASIS_VALUES else "inferred",
            priority=int(priority or 0),
        )
        try:
            row, created = self.store.add_hypothesis(self.id, h)
        except ValueError as exc:
            # Validation is the point: a direction nobody can falsify is not
            # work. Reported as a tool error, not as a dispatch failure.
            return {"error": str(exc)}
        self._emit(
            "hypothesis.posted" if created else "hypothesis.merged",
            {"hypothesis_id": row.id, "family": row.family, "statement": row.statement,
             "criterion": row.criterion, "origin": origin, "created": created},
        )
        out = {"hypothesis_id": row.id, "family": row.family, "created": created}
        if not row.viable():
            # Merging into a settled row does not reopen it (``add_hypothesis``
            # only merges evidence and tallies). Say so in the tool result:
            # without this the lead reads "merged" as "accepted", dispatches the
            # direction again, and the refutation buys nothing.
            out["status"] = row.status
            out["note"] = (
                f"这条方向在板上已关闭（{row.status}，证实 {row.supports} / 证伪 {row.refutes}）。"
                "合并没有重开它，也不能再派工。若确有新证据要重走，请写清新证据、"
                "换一条可区分的新表述重新 post。"
            )
            self._emit(
                "hypothesis.repost_closed",
                {"hypothesis_id": row.id, "status": row.status, "origin": origin},
            )
        return out

    def bind_hypothesis(self, task_id: int, hypothesis_id: int) -> dict[str, Any]:
        """Point a task at a board entry and lease it to that task."""
        row = self.store.get_hypothesis(hypothesis_id)
        if row is None:
            return {"error": f"假设 #{hypothesis_id} 不存在"}
        # A settled direction is closed, not free work. This is the gate that
        # turns "the lead re-posts a refuted idea and dispatches it anyway" into
        # a hard rejection: the only way back onto a closed direction is a new
        # hypothesis carrying new evidence.
        if not row.viable():
            return {
                "error": (
                    f"假设 #{hypothesis_id} 已关闭（{row.status}，证实 {row.supports} / "
                    f"证伪 {row.refutes}），不能派工。要重走必须带新证据 post 一条新假设。"
                )
            }
        # The coordinator leases with a negative placeholder id before the task
        # row exists (it has to claim before it can dispatch). Any negative
        # owner is "claimed by a dispatch in progress", not a rival task.
        held_by_other = (
            row.claimed_by is not None
            and row.claimed_by != task_id
            and row.claimed_by > 0
        )
        if row.status == swarm.CLAIMED and held_by_other:
            return {"error": f"假设 #{hypothesis_id} 已被工单 #{row.claimed_by} 认领"}
        if not self.store.lease_hypothesis(hypothesis_id, task_id, time.time()):
            return {"error": f"假设 #{hypothesis_id} 已被别人抢先认领"}
        self._emit(
            "hypothesis.claimed",
            {"hypothesis_id": hypothesis_id, "task_id": task_id,
             "family": row.family, "statement": row.statement},
            task_id=task_id,
        )
        return {"hypothesis_id": hypothesis_id, "family": row.family}

    def _sweep_hypotheses(self) -> None:
        """Return lapsed leases to the board. Cheap; called on task boundaries."""
        for hypothesis_id in self.store.expire_hypotheses(time.time()):
            self._emit("hypothesis.expired", {"hypothesis_id": hypothesis_id})

    # ------------------------------------------------- board-driven dispatch
    #
    # 蜂群 B 阶段（「蜂巢」）：the lead writes what it believes, workers are
    # started by the board rather than by the lead's next turn. The point is not
    # more agents — it is that a direction nobody has to *decide* to start
    # cannot wait behind the lead's slowest turn (35% of run cost, measured).
    #
    # Everything here is deterministic and free: the coordinator reads rows and
    # starts tasks. What it must never do is invent work — an empty board is a
    # result, and the convergence rules say so.

    @property
    def swarm_enabled(self) -> bool:
        return str(getattr(self.cfg.limits, "swarm", "off") or "off").strip().lower() == "board"

    def _start_board_loop(self) -> None:
        """Arm the coordinator. No-op unless the swarm mode is on."""
        if not self.swarm_enabled:
            return
        if self._board_task is not None and not self._board_task.done():
            return
        self._board_task = asyncio.create_task(self._board_loop())

    def _stop_board_loop(self) -> None:
        task, self._board_task = self._board_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _board_loop(self) -> None:
        """Watch the board and start a worker for every open entry we can afford.

        Polls rather than waits on a signal: the producers are worker tool
        calls and the lead's turn, and every wake-up path would have to be wired
        into both. A two-second tick costs one SQL query and cannot deadlock on
        a missed notification.
        """
        try:
            while not self._stop.is_set():
                await asyncio.sleep(_BOARD_TICK_S)
                try:
                    await self._board_tick()
                except Exception as exc:  # noqa: BLE001 - the loop must outlive bugs
                    self._emit("board.error", {"error": f"{type(exc).__name__}: {exc}"})
        except asyncio.CancelledError:
            raise

    async def _board_tick(self) -> list[int]:
        """One coordination pass. Returns the task ids it started.

        Split from the loop so the rules are testable and reviewable without
        timers. Order matters: release first (a dead worker's direction goes
        back on the board before we look at it again), then converge, then
        start — a tick that started first would pay for a claim it is about to
        release.
        """
        if self._stop.is_set() or self._finish_status:
            return []
        self._release_dead_claims()
        self._sweep_hypotheses()
        # The soft ceiling binds the coordinator exactly as it binds the lead's
        # ``dispatch_task``: past 85% of the run budget only closing roles may
        # start. The board starts nothing but exploration roles, so without
        # this gate a decentralised run would spend the 15% that is reserved
        # for the verifier — the one session that turns a claimed flag into a
        # confirmed one. Entries stay open, so the lead still sees them.
        if not self.accepting_exploration:
            return []
        header = self.store.list_hypotheses(self.id)
        # ``idle`` is a result, not an instruction. Say so once, and only when
        # the lead alone can still act: a board with no open entry and nothing
        # running is the one state where the run waits on a hypothesis the
        # operator (through the lead) has not posted. Without this event the UI
        # shows an empty board and a quiet run with no explanation; without the
        # latch it would repeat every two seconds.
        if not any(h.viable() for h in header) and not any(
            not t.terminal for t in self.store.list_tasks(self.id)
        ):
            if not self._board_idle_announced:
                self._board_idle_announced = True
                self._emit(
                    "board.idle",
                    {"message": "板上没有可认领的假设，也没有在跑的工单"},
                )
            return []
        self._board_idle_announced = False
        # The gate is "is there anything to start", not the board's overall
        # state string. ``convergence`` answers "what does the board mean" for
        # the lead's prompt, where a live worker makes the whole board
        # *working*; the coordinator has a capacity budget instead, and a
        # lead-dispatched task is one slot, not a reason to stop starting the
        # open entries beside it. Only two things stop a tick: no open entry
        # (nothing to start — an empty board is a result, not an instruction),
        # and no free slot (the loop below).
        if not any(h.status == swarm.OPEN for h in header):
            return []
        started: list[int] = []
        limit = max(1, int(getattr(self.cfg.limits, "swarm_max_workers", 3)))
        # A refused dispatch is final for this tick: budget, the soft ceiling
        # and an infra outage do not heal inside one pass, and retrying would
        # burn every open entry into "claimed, no task" and back.
        while self._board_has_slot(limit):
            row = self._claim_next()
            if row is None:
                break
            task = await self._dispatch_claim(row)
            if task is None:
                # Put the entry straight back — a claim with no task behind it
                # reads as "working" forever, and the board would stop starting
                # anyone.
                self.store.set_hypothesis_status(row.id, swarm.OPEN, clear_claim=True)
                self._board_backoff[row.family] = (
                    time.monotonic() + _BOARD_REFUSAL_BACKOFF_S
                )
                break
            started.append(task.id)
        if started:
            self._wake_lead(started)
        return started

    def _wake_lead(self, task_ids: list[int]) -> None:
        """Tell an idle lead that the board started work on its behalf.

        In this mode the lead writes hypotheses and then has nothing to wait on:
        it never called ``dispatch_task``, so it holds no task ids, and a lead
        that goes quiet waiting for a turn nobody will send stalls the run while
        its own workers are alive. The message is queued, never delivered as an
        interrupt — a lead mid-turn keeps its turn and reads this next.

        At most one is outstanding. The board can start a worker on every tick
        and a lead turn takes minutes; piling one message per dispatch behind a
        slow turn replays the same instruction several times when it finally
        reads. A second batch is folded into the first instead: by the time the
        lead looks, the task ids it needs are in the digest anyway.
        """
        if self._lead_turn_active or self._main_client is None or self._closed:
            return
        if self._board_wake_pending:
            self._board_wake_ids = list(dict.fromkeys([*self._board_wake_ids, *task_ids]))
            return
        self._board_wake_pending = True
        self._board_wake_ids = list(task_ids)
        names = "、".join(f"#{tid}" for tid in task_ids)
        text = (
            f"板子已经派出 {names}（来自你挂在板上的假设）。用 wait_tasks([{', '.join(str(t) for t in task_ids)}]) "
            "等它们，不要为同一个方向再派一个 —— 同族同时只有一个在跑。"
        )
        self._board_wake_text = text
        try:
            self._chat_queue.put_nowait(text)
        except asyncio.QueueFull:  # pragma: no cover - unbounded queue
            self._board_wake_pending = False
            self._board_wake_text = None

    def _board_running(self) -> int:
        """Worker directions the *board* currently has alive in this run.

        Counted from task rows (not from a local counter) so a task that died
        without telling us is not counted as occupying a slot. Only tasks bound
        to a hypothesis count: a lead-dispatched triage worker does not consume
        the board's own bound, it consumes the run's pool, and that is enforced
        separately (``_board_has_slot``).
        """
        return sum(
            1
            for t in self.store.list_tasks(self.id)
            if not t.terminal
            and t.role not in CLOSING_ROLES
            and getattr(t, "hypothesis_id", None)
        )

    def _total_workers_running(self) -> int:
        """Every live worker in the run, board-started or lead-dispatched."""
        return sum(
            1
            for t in self.store.list_tasks(self.id)
            if not t.terminal and t.role not in CLOSING_ROLES
        )

    def _board_has_slot(self, limit: int) -> bool:
        """Whether the coordinator may start one more worker.

        Two bounds, both deliberate. ``swarm_max_workers`` caps what this
        mechanism opens on its own; ``workers_per_run`` caps the run, and a
        lead that has already filled it must not have the board pile a queue of
        claimed-but-waiting sessions behind the semaphore — those hold their
        hypotheses while doing nothing.
        """
        if self._board_running() >= limit:
            return False
        return self._total_workers_running() < max(1, int(self.cfg.limits.workers_per_run))

    def _claim_next(self) -> Any:
        """Lease the best open hypothesis for a *new* worker of its own family.

        A board entry names a family, and the family names the role: the worker
        that starts is the specialist for that direction, not a generalist. The
        role's own budget is what the dispatch will hand it, so an entry no role
        can afford is simply not started this tick.

        Occupancy is read from the task table, not from a counter: a family is
        busy only while a worker of that family is *alive*. A lifetime counter
        would let two started-then-crashed workers retire a direction for the
        rest of the run, while the board shows four open entries nobody may
        touch. The counter below survives for one purpose only — capping how
        many times a refuted direction may be retried.
        """
        rows = self.store.list_hypotheses(self.id, status=swarm.OPEN)
        if not rows:
            return None
        live = self._live_board_families()
        now = time.monotonic()
        for row in rows:
            # One live direction at a time per family: two workers on one family
            # is the duplicate dispatch the lead was already warned about, now
            # enforced instead of suggested.
            if row.family in live:
                continue
            # A family whose last start was refused waits out the backoff. Other
            # families keep their chance: the refusal may be about this
            # direction's budget, and holding the whole board would turn one
            # expensive entry into a silent stop for everyone.
            if self._board_backoff.get(row.family, 0.0) > now:
                continue
            # A direction that has been refuted may be retried — with new
            # evidence, which ``swarm.describe`` puts in front of the worker —
            # but a family that keeps coming back refuted has spent its budget.
            if row.refutes and self._board_starts.get(row.family, 0) >= 2:
                continue
            # Lease *this* row, not "the best open one": ``claim_hypothesis``
            # picks its own winner, so asking it for the entry we just filtered
            # returns a different hypothesis and silently starts the second
            # worker of a family we just decided to hold back (observed while
            # writing the coordinator's own test).
            if self.store.lease_hypothesis(row.id, -row.id, time.time()):
                return self.store.get_hypothesis(row.id)
        return None

    def _live_board_families(self) -> set[str]:
        """Families that already have a live worker.

        Two sources, and both count: a worker started by the board (its task row
        names the hypothesis, whose family is the direction) and one the lead
        dispatched centrally (its role *is* the direction). A live lead worker
        is the same duplicate dispatch as a second board worker — the guard has
        to see both or the "one live direction per family" rule holds only for
        the shape that happened to be tested.
        """
        family_of = {h.id: h.family for h in self.store.list_hypotheses(self.id)}
        out: set[str] = set()
        for task in self.store.list_tasks(self.id):
            if task.terminal:
                continue
            if task.role in swarm.DIRECTION_ROLES:
                out.add(task.role)
            bound = getattr(task, "hypothesis_id", None)
            if bound:
                family = family_of.get(int(bound))
                if family:
                    out.add(family)
        return out

    async def _dispatch_claim(self, row: Any) -> Any:
        """Start the specialist for ``row``'s family and re-lease the entry to it."""
        role = row.family if row.family in swarm.DIRECTION_ROLES else "triage"
        # The coordinator only starts exploring roles, and those are exactly the
        # sessions the board ledger holds back. Zero free means every usable
        # dollar is already promised (live holds) or spent; starting anyway
        # would hand the SDK a zero ceiling, which dies in a second as
        # "budget exhausted" and makes the tick retry the same entry forever.
        budget = self._board_budget
        if budget is not None and budget.free() <= 0.0:
            self._emit(
                "board.refused",
                {"hypothesis_id": row.id, "role": role,
                 "error": "板子的可用额度已用完（收尾角色的 15% 不在这里花）"},
            )
            return None
        objective = f"{row.family} 方向：证伪或证实假设 #{row.id}"
        context = swarm.describe(row)
        try:
            task = await self.dispatch(role, objective=objective, context=context)
        except Exception as exc:  # noqa: BLE001 - a refused dispatch is data
            self._emit(
                "board.refused",
                {"hypothesis_id": row.id, "role": role,
                 "error": f"{type(exc).__name__}: {exc}"},
            )
            return None
        # Bind immediately: the row now carries ``hypothesis_id``, which is what
        # deadline/timeout cleanup reads to put a dead worker's direction back.
        # Leaving it None until a later step is how the worker test caught this —
        # a task that died between dispatch and bind held a claim nobody could
        # trace.
        try:
            self.store.set_task_hypothesis(task.id, row.id)
            task = self.store.get_task(task.id) or task
        except Exception:  # noqa: BLE001 - the lease can still be set below
            pass
        if self.bind_hypothesis(task.id, row.id).get("error"):
            # Someone else took it between claim and dispatch (or the lease
            # lapsed). The task already exists and will work the objective it
            # was given; it just does not own the entry.
            self._emit(
                "board.unbound", {"hypothesis_id": row.id, "task_id": task.id}
            )
        self._board_starts[row.family] = self._board_starts.get(row.family, 0) + 1
        self._board_backoff.pop(row.family, None)
        self._emit(
            "board.dispatched",
            {"hypothesis_id": row.id, "task_id": task.id, "role": role,
             "family": row.family, "statement": row.statement},
            task_id=task.id,
        )
        return task

    def _release_dead_claims(self) -> None:
        """Put entries back on the board whose worker is gone.

        The lease is the backstop, not the trigger: a worker that crashed the
        second after claiming would otherwise block its direction for the full
        TTL while the board reported *working* and nothing was running. The
        coordinator's own claim (``task_id=-row.id`` placeholder) is excluded —
        it is bound to a real task a moment later and must not be swept in that
        window, or the same direction gets started twice.
        """
        live = {t.id for t in self.store.list_tasks(self.id) if not t.terminal}
        for row in swarm.pending_claims(self.store.list_hypotheses(self.id), live):
            if row.claimed_by is not None and row.claimed_by < 0:
                continue
            self.store.set_hypothesis_status(row.id, swarm.OPEN, clear_claim=True)
            self._emit(
                "hypothesis.released",
                {"hypothesis_id": row.id, "was_claimed_by": row.claimed_by},
            )

    def _settle_hypothesis(self, task_id: int, status: str) -> None:
        """Close the board entry a finished task was holding.

        Called from ``_finish_task`` for every task. What the worker *wrote*
        outranks its exit status: a worker that recorded a grounded ``dead_end``
        refuted its direction, while a worker that ran out of turns refuted
        nothing and the entry goes back for someone cheaper.
        """
        task = self.store.get_task(task_id)
        if task is None or not getattr(task, "hypothesis_id", None):
            return
        row = self.store.get_hypothesis(int(task.hypothesis_id))
        if row is None or not row.viable():
            # A row that already reached a terminal status was settled by someone
            # else; settling it again would double-count the tally. A row that
            # never became claimable does not exist.
            return
        if row.status == swarm.CLAIMED and row.claimed_by not in (None, task_id):
            # A different live worker now holds the lease. Its outcome is the one
            # that settles the direction, not this stale task's.
            return
        facts = [f for f in self.store.list_facts(self.id) if f.task_id == task_id]
        outcome = ""
        for fact in reversed(facts):
            # ``settled_by`` only proposes; the grounding check happens here and
            # is the same rule the blackboard already applies. A dead_end the
            # writer declared ``observed`` but could not ground was downgraded to
            # ``inferred`` when it was recorded — and an ungrounded negative must
            # not close a direction, or the board fills with "disproven" routes
            # nobody actually tried.
            proposed = swarm.settled_by(str(fact.kind), bool(fact.resolves))
            if not proposed:
                continue
            if proposed == swarm.SETTLE_REFUTED:
                if fact.basis not in ("observed", "derived"):
                    continue
                if (fact.evidence or {}).get(NEGATIVE_GAP_KEY):
                    continue
            outcome = proposed
            break
        if not outcome:
            # A candidate flag is the one piece of evidence that lives outside
            # the fact stream: ``submit_flag`` writes to ``flags`` and spawns a
            # verifier. Counting only facts made the most successful worker on
            # the board look like one that had written nothing.
            submitted = any(
                flag.task_id == task_id for flag in self.store.list_flags(self.id)
            )
            outcome = swarm.settle_for(status, evidence=bool(facts) or submitted)
        if outcome == swarm.SETTLE_REFUTED:
            self.store.bump_hypothesis(row.id, refutes=1)
            self.store.set_hypothesis_status(row.id, swarm.REFUTED)
            self._emit("hypothesis.refuted", {"hypothesis_id": row.id, "task_id": task_id})
        elif outcome == swarm.SETTLE_DONE:
            self.store.bump_hypothesis(row.id, supports=1)
            self.store.set_hypothesis_status(row.id, swarm.SUPPORTED)
            self._emit("hypothesis.supported", {"hypothesis_id": row.id, "task_id": task_id})
        else:
            # Ceilings, crashes and operator stops say nothing about the
            # direction. Back on the board; the refute count carries forward so
            # a direction that keeps failing climbs less cheaply each time.
            # The lease owner goes with it — an entry that reads ``open`` but
            # still names the dead task is a claim nobody can take over.
            self.store.set_hypothesis_status(row.id, swarm.OPEN, clear_claim=True)
            self._emit(
                "hypothesis.released",
                {"hypothesis_id": row.id, "task_id": task_id, "status": str(status)},
            )

    def record_fact(
        self,
        task_id: int | None,
        kind: str,
        content: str,
        evidence: dict[str, Any] | None = None,
        basis: str = "",
        resolves: int | None = None,
        supersedes: int | None = None,
    ) -> int:
        try:
            kind_enum = FactKind(kind)
        except ValueError:
            kind_enum = FactKind.OBSERVATION
        if basis not in BASIS_VALUES:
            basis = ""
        # "Observed" is the one label later workers treat as checked. A record
        # that says it but quotes neither the command nor the raw output is a
        # memory of a run, not a record of one, so it is filed as a hypothesis
        # instead. Re-recording it with the evidence is an upgrade, not a
        # restatement (see ``_BASIS_RANK``), so nothing is lost permanently.
        gap = grounding_gap(basis, evidence)
        if gap:
            evidence = {**(evidence or {}), "grounding_gap": gap, "declared_basis": basis}
            self._emit(
                "fact.downgraded",
                {"task_id": task_id, "content": content[:300], "declared": basis,
                 "reason": gap},
                task_id=task_id,
            )
            basis = "inferred"
        # A negative result is the record most likely to be wrong in a way the
        # next run pays for, and the one whose evidence can be complete yet
        # still say nothing: command and output prove the search ran, not that
        # it covered the right space or that the detector could ever hit. The
        # fact is kept (where someone looked is useful) but marked, so neither
        # the board nor a case card presents it as settled.
        ngap = negative_gap(kind_enum, evidence)
        if ngap:
            evidence = {**(evidence or {}), NEGATIVE_GAP_KEY: ngap}
        # Restatements are dropped at the single write path so every caller
        # (worker tools, lead agent, task_complete) is covered. The digest is
        # injected into every worker prompt, so a duplicate costs tokens on
        # every later dispatch. A record carrying ``resolves`` is always
        # written: closing an unknown is a new, auditable event even when the
        # text repeats an earlier fact.
        # A record that overturns an earlier one is new information even when its
        # text overlaps that record: dropping it as a restatement would leave the
        # board showing the conclusion it was written to correct.
        if kind_enum is not FactKind.UNKNOWN and not resolves and not supersedes:
            earlier = self._restated_by(content, kind_enum, basis)
            if earlier is not None:
                self._emit(
                    "fact.merged",
                    {"fact_id": earlier.id, "task_id": task_id,
                     "content": content, "kind": str(kind_enum)},
                    task_id=task_id,
                )
                return earlier.id
        fact_id = self.store.add_fact(
            self.id, task_id, kind_enum, content, evidence or {}, basis, resolves, supersedes
        )
        if self._fact_index is not None:
            self._fact_index.append(
                (
                    Fact(
                        id=fact_id, run_id=self.id, task_id=task_id, kind=str(kind_enum),
                        content=content, evidence=evidence or {}, created_at="",
                        basis=basis, resolves=resolves, supersedes=supersedes,
                    ),
                    _fact_tokens(content),
                    _fact_hard_tokens(content),
                )
            )
        self._emit(
            "fact.added",
            {"fact_id": fact_id, "task_id": task_id, "kind": str(kind_enum),
             "content": content, "basis": basis, "resolves": resolves,
             "supersedes": supersedes},
            task_id=task_id,
        )
        # Something new was written down, so the no-progress clock restarts.
        # Deliberately not reset on a merge: a worker restating what the board
        # already says is exactly the spin this guard exists to stop.
        if task_id is not None:
            self._tools_at_fact[task_id] = int(
                (self._live.get(task_id) or {}).get("tools") or 0
            )
            # Writing a fact is the heartbeat: a worker still producing knowledge
            # still owns its hypothesis. No separate tool, no extra round trip.
            task_row = self.store.get_task(task_id)
            bound = getattr(task_row, "hypothesis_id", None)
            if bound:
                self.store.renew_hypothesis(bound, time.time())
        return fact_id

    def _restated_by(self, content: str, kind: FactKind, basis: str = "") -> Fact | None:
        """The most similar earlier fact, if ``content`` only restates it.

        An INSIGHT is a conclusion drawn from what is already on the board, so
        it merges into any earlier record. A DEAD_END, CREDENTIAL or
        OBSERVATION adds framing of its own ("do not go here", "this is a
        secret", "I ran it") and only merges into a record of the same kind.
        """
        if self._fact_index is None:
            self._fact_index = [
                (f, _fact_tokens(f.content), _fact_hard_tokens(f.content))
                for f in self.store.list_facts(self.id, limit=500)
                if f.kind != FactKind.UNKNOWN
            ]
        tokens = _fact_tokens(content)
        if not tokens:
            return None
        hard = _fact_hard_tokens(content)
        rank = _BASIS_RANK.get(basis, 0)
        best: tuple[int, Fact] | None = None
        for fact, earlier_tokens, earlier_hard in self._fact_index:
            if kind is not FactKind.INSIGHT and fact.kind != kind:
                continue
            if rank > _BASIS_RANK.get(fact.basis, 0):
                continue
            if len(tokens & earlier_tokens) / len(tokens) < _RESTATE_COVERAGE:
                continue
            if hard - earlier_hard:
                continue
            overlap = len(tokens & earlier_tokens)
            if best is None or overlap > best[0]:
                best = (overlap, fact)
        return best[1] if best else None

    def emit(self, task_id: int | None, kind: str, payload: dict[str, Any]) -> None:
        self._emit(kind, payload, task_id=task_id)

    def open_unknowns(self, limit: int = 200) -> list[Any]:
        """Unknown facts that no later fact has closed.

        The blackboard is append-only, so "resolved" is expressed as a pointer
        rather than an update: a later record carries ``resolves=<unknown id>``.
        That keeps history intact and makes the closure auditable.
        """
        facts = self.store.list_facts(self.id, limit=limit)
        closed = {f.resolves for f in facts if f.resolves}
        return [f for f in facts if f.kind == FactKind.UNKNOWN and f.id not in closed]

    def blackboard_digest(self, limit: int = 60, for_main: bool = False) -> str:
        facts = self.store.list_facts(self.id, limit=limit)
        flags = self.store.list_flags(self.id)
        lines: list[str] = []
        hypothesis_block = self._hypotheses_text(for_main=for_main)
        if hypothesis_block:
            lines += [hypothesis_block, ""]
        if flags:
            lines.append("## 候选 flag")
            for flag in flags:
                lines.append(f"- [{flag.status}] {flag.value} (from {flag.source or 'unknown'})")
            lines.append("")
        open_unknowns = self.open_unknowns()
        if open_unknowns:
            lines.append("## 未解问题（还没解决）")
            for fact in open_unknowns:
                origin = f"工单#{fact.task_id}" if fact.task_id else "主 agent"
                settle = str((fact.evidence or {}).get("would_resolve") or "").strip()
                # The settling observation is the next experiment. Carrying it
                # next to the question is what turns "still unknown" into a
                # dispatch that can actually close it.
                criterion = f"\n  判据: {_clip(settle)}" if settle else ""
                lines.append(f"- #{fact.id} [{origin}] {fact.content}{criterion}")
            lines.append("")
        if facts:
            lines.append("## 黑板记录（由早到晚）")
            # Newest first while spending the evidence budget, then rendered in
            # time order: a fact from the last ten minutes is what the next
            # worker acts on, and it is the one whose command is worth its
            # tokens. Marginal notes too small to matter are skipped rather
            # than imported wholesale.
            budget = _EVIDENCE_BUDGET
            suffixes: dict[int, str] = {}
            for fact in reversed(facts):
                suffix = _evidence_suffix(fact)
                if len(suffix) > budget:
                    suffix = ""
                budget -= len(suffix)
                suffixes[fact.id] = suffix
            # Which fact overturned which. On run 18 four reverse workers each
            # re-ran the same experiment because the board showed "the curve
            # arithmetic is equivalent" and "the curve arithmetic is not
            # equivalent" side by side, with nothing saying which came later and
            # won. The chain is what turns a pile of records into a current state.
            overturned: dict[int, int] = {
                f.supersedes: f.id for f in facts if f.supersedes
            }
            for fact in facts:
                origin = f"工单#{fact.task_id}" if fact.task_id else "主 agent"
                basis = f"/{BASIS_LABEL.get(fact.basis, fact.basis)}" if fact.basis else ""
                # A negative whose scope and control were never stated is a
                # location someone looked, not a route that is closed. Saying so
                # on the board is what stops the next worker from treating it as
                # settled and skipping the sweep that would have found the key.
                ngap = str((fact.evidence or {}).get(NEGATIVE_GAP_KEY) or "")
                kind_label = "未验证的否定" if ngap else fact.kind
                warn = f"（未验证：{ngap}）" if ngap else ""
                closed = f" → 解决 #{fact.resolves}" if fact.resolves else ""
                replaced = f" → 取代 #{fact.supersedes}" if fact.supersedes else ""
                newer = overturned.get(fact.id)
                if newer is not None:
                    # Still shown: what was believed and why it fell is how the
                    # next worker avoids repeating it. But it is not actionable.
                    warn = f"【已被 #{newer} 推翻，不要据此行动】{warn}"
                lines.append(
                    f"- #{fact.id} ({kind_label}{basis}) [{origin}] {fact.content}{warn}"
                    f"{closed}{replaced}{suffixes.get(fact.id, '')}"
                )
        return "\n".join(lines) if lines else "（空）"

    def _tasks_text(self) -> str:
        out = []
        for task in self.store.list_tasks(self.id):
            summary = str(task.result.get("summary", ""))[:300].replace("\n", " ")
            out.append(f"- #{task.id} {task.role} [{task.status}] {task.objective[:160]} :: {summary}")
        return "\n".join(out) or "（无）"

    def live_tasks(self) -> list[dict[str, Any]]:
        """Running tasks with what they last did, for the page's activity strip.

        A worker can run for half an hour without the lead saying a word; the
        strip is what keeps the page from looking dead in that stretch.
        """
        out: list[dict[str, Any]] = []
        for task in self.store.list_tasks(self.id):
            if task.terminal:
                continue
            live = self.live_progress(task.id)
            if not live:
                continue
            out.append({"task_id": task.id, "role": task.role, **live})
        return out

    def task_summaries(self) -> list[dict[str, Any]]:
        return [
            {
                "task_id": t.id,
                "role": t.role,
                "status": t.status,
                "objective": t.objective,
                "summary": str(t.result.get("summary", ""))[:800],
                "cost_usd": t.cost_usd,
                # Booked on the result event, so a task closed mid-work reports
                # 0. The tool count is the only progress signal that survives
                # that, and without it the dashboard says "did nothing" about a
                # session that ran for an hour.
                "turns": t.turns,
                "tools_called": int(t.result.get("tools_called") or 0),
                "live": self.live_progress(t.id) if not t.terminal else {},
            }
            for t in self.store.list_tasks(self.id)
        ]

    def task_result(self, task_id: int) -> dict[str, Any]:
        task = self.store.get_task(int(task_id))
        if task is None:
            return {"error": f"no task {task_id}"}
        return {
            "task_id": task.id,
            "role": task.role,
            "status": task.status,
            "objective": task.objective,
            "result": task.result,
            "error": task.error,
            "cost_usd": task.cost_usd,
        }

    def _list_artifacts(self) -> list[str]:
        """Files a worker produced, newest first.

        ``.claude/`` is excluded: it holds the vendored skill library, which is
        identical in every run and made up the first 200 entries — every task's
        artifact list was the skill index and never the worker's own output.
        ``.git/`` is excluded for the same reason: it is the empty repository
        that marks the project root, not something a worker produced.
        """
        out: list[tuple[float, str]] = []
        for path in self.workspace.work.rglob("*"):
            try:
                if not path.is_file():
                    continue
                rel = path.relative_to(self.workspace.work)
                if rel.parts and rel.parts[0] in (".claude", ".git"):
                    continue
                if path.stat().st_size >= 5_000_000:
                    continue
                out.append((path.stat().st_mtime, str(rel)))
            except OSError:
                continue
        out.sort(reverse=True)
        return [name for _, name in out[:200]]

    # ------------------------------------------------------------------ flags
    async def propose_flag(
        self,
        value: str,
        method: str,
        source: str = "",
        confidence: str = "medium",
        task_id: int | None = None,
    ) -> dict[str, Any]:
        value = value.strip()
        if not value:
            return {"error": "flag 为空"}
        existing = [f for f in self.store.list_flags(self.id) if f.value == value]
        if existing:
            flag = existing[0]
        else:
            flag = self.store.add_flag(self.id, task_id, value, source, method, confidence)
            assert flag is not None
            self._emit(
                "flag.proposed",
                {"flag_id": flag.id, "value": value, "source": source, "method": method,
                 "confidence": confidence, "task_id": task_id},
                task_id=task_id,
            )
        if flag.status in (FlagStatus.VERIFIED, FlagStatus.CONFIRMED):
            return {"flag_id": flag.id, "status": str(flag.status), "verifier_task_id": None}
        if not self.accepting_work:
            return {"flag_id": flag.id, "status": str(flag.status), "verifier_task_id": None}
        # One verifier per candidate. Workers re-submit the same value as they
        # converge, and a verifier per submission would burn a session slot and
        # produce duplicate verdicts on the same flag.
        pending = self._pending_verifiers.get(flag.id)
        if pending and not self.store.get_task(pending).terminal:
            return {
                "flag_id": flag.id,
                "status": str(flag.status),
                "verifier_task_id": pending,
                "note": "已经有验证任务在核查这个候选 flag。",
            }
        # A verifier the lead dispatched by hand is not bound to a flag, so the
        # check above cannot see it. Observed live: a stalled auto-verifier was
        # replaced while a hand-dispatched one was already confirming the same
        # flag — three verifier sessions for one value. Match on the value in
        # the objective, so an unrelated candidate can still be verified.
        for task in self.store.list_tasks(self.id):
            if task.role == "verifier" and not task.terminal and value in task.objective:
                return {
                    "flag_id": flag.id,
                    "status": str(flag.status),
                    "verifier_task_id": task.id,
                    "note": "已有验证任务在核查这个值，等它给出结论再决定是否再派。",
                }
        verifier = await self.dispatch(
            "verifier",
            objective=(
                f"独立复现或推翻这个候选 flag：{value}\n"
                f"提交者声称的推导：\n{method}\n声称的来源：{source or '（无）'}"
            ),
            context=f"候选 flag id 为 {flag.id}。用工作区里的实际文件来验证。",
        )
        self._verifier_flags[verifier.id] = flag.id
        self._pending_verifiers[flag.id] = verifier.id
        return {
            "flag_id": flag.id,
            "status": str(FlagStatus.CANDIDATE),
            "verifier_task_id": verifier.id,
            "note": "验证任务正在核查；最终 flag 由操作员确认。",
        }

    def _apply_verdict(self, task_id: int, summary: str, verdict: str = "") -> None:
        """Record what the verifier concluded.

        ``verdict`` is the structured field from ``task_complete`` and is
        preferred. It exists because reading the conclusion out of prose failed
        often enough to matter: 6 of 15 verdicts in the database came back
        "none", and a "none" leaves the flag in ``candidate`` for a human to
        adjudicate by reading the tail of the report.
        """
        verdict = str(verdict or "").strip().lower()
        if verdict not in VERDICTS:
            match = _VERDICT_RE.search(str(summary or ""))
            verdict = _VERDICT_WORDS.get(match.group(1).lower(), "") if match else ""
        if not verdict:
            # No VERDICT line. That is a real outcome only when the verifier
            # finished and chose not to give one; a verifier cancelled mid-check
            # still produced evidence the operator needs. Report the tail
            # instead of a flat "没有给出结论", which is what discarded a
            # verifier's reproduced plaintext when the run clock cut it off.
            tail = " ".join(str(summary or "").split())[-400:]
            self._emit(
                "flag.verdict",
                {"task_id": task_id, "verdict": "none",
                 "note": "验证任务没有给出 VERDICT 结论"
                         + (f"；它停下前最后说的是：{tail}" if tail else "")},
                task_id=task_id,
            )
            return
        flag_id = self._verifier_flags.pop(task_id, None)
        flag = self.store.get_flag(flag_id) if flag_id else None
        if flag is None:
            # No binding means this verifier was dispatched by hand. Picking
            # "the latest candidate" would apply its verdict to whatever flag
            # happened to be created most recently — which is how a quoted
            # near-miss in the verifier's own report got marked verified.
            self._emit(
                "flag.verdict",
                {"task_id": task_id, "verdict": "unbound",
                 "note": "验证任务没有绑定候选 flag，结论未应用到任何 flag"},
                task_id=task_id,
            )
            return
        new_status = {
            "reproduced": FlagStatus.VERIFIED,
            "refuted": FlagStatus.REJECTED,
            "inconclusive": FlagStatus.CANDIDATE,
        }[verdict]
        if flag.status == FlagStatus.CONFIRMED and new_status == FlagStatus.VERIFIED:
            # The operator confirmed while the verifier was still running and
            # the close let this verdict land afterwards. Verification cannot
            # downgrade a human decision — the status stays confirmed and the
            # verdict is recorded as the note it is.
            self.store.set_flag_status(
                flag.id, FlagStatus.CONFIRMED, note=f"确认后复核通过（任务 #{task_id}）"
            )
            self._emit(
                "flag.verdict",
                {"flag_id": flag.id, "value": flag.value, "verdict": verdict,
                 "status": str(FlagStatus.CONFIRMED), "task_id": task_id},
                task_id=task_id,
            )
            return
        self.store.set_flag_status(flag.id, new_status, note=f"验证任务 #{task_id}：{verdict}")
        self._emit(
            "flag.verdict",
            {"flag_id": flag.id, "value": flag.value, "verdict": verdict,
             "status": str(new_status), "task_id": task_id},
            task_id=task_id,
        )
        if verdict == "reproduced":
            # A verified flag still needs a human; the run pauses for confirmation.
            if not self._closed:
                self.store.set_run_status(self.id, RunStatus.NEEDS_INPUT)
            self._emit(
                "run.needs_input",
                {"reason": "flag 已复现，等待操作员确认", "flag": flag.value},
            )

    def _scan_for_flags(self, task_id: int, text: str) -> None:
        """Last-resort extraction from a worker's final report.

        Structured ``submit_flag`` is the primary channel; this catches a worker
        that forgot. A pattern from ``flag_format`` is preferred; the default
        matches the common wrappers.

        A match that the report itself calls a decoy, a wrong answer or a
        negative control is skipped. Workers quote those on purpose — a forensics
        report says "the .decoy section contains flag{...}, which is bait", and
        a verifier lists the inputs that printed ``Wrong!``. Turning those into
        candidates puts bait in front of the operator and, in one run, a
        near-miss with an extra ``!`` outranked the real flag.
        """
        if not text:
            return
        # The structured channel beats the scan. A worker that called
        # ``submit_flag`` already stated its answer; scanning its report
        # anyway turned every number in the prose into a low-confidence
        # decoy — run 83 produced nine decoys around one real flag under a
        # `[0-9]+` flag format. The scan keeps its purpose as the safety net
        # for workers that forgot to submit.
        if any(
            f.task_id == task_id and f.source != "worker 的最终报告"
            for f in self.store.list_flags(self.id)
        ):
            return
        default = r"[A-Za-z0-9_]{2,20}\{[^{}\s]{4,200}\}"
        pattern = self.challenge.flag_format or default
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(default)
        for match in regex.finditer(text):
            value = match.group(0)
            if any(f.value == value for f in self.store.list_flags(self.id)):
                continue
            if _looks_like_negative(text[max(0, match.start() - 120) : match.end() + 120]):
                continue
            flag = self.store.add_flag(
                self.id, task_id, value, "worker 的最终报告", "从报告文本里提取", "low"
            )
            if flag is None:
                continue
            self._emit(
                "flag.proposed",
                {"flag_id": flag.id, "value": value, "source": "报告文本",
                 "confidence": "low", "task_id": task_id},
                task_id=task_id,
            )

    def confirmed_flag_value(self) -> str:
        for flag in self.store.list_flags(self.id):
            if flag.status == FlagStatus.CONFIRMED:
                return flag.value
        for flag in self.store.list_flags(self.id):
            if flag.status == FlagStatus.VERIFIED:
                return flag.value
        return "（尚未确认）"

    async def confirm_flag(
        self, flag_id: int, note: str = "", writeup: bool | None = None
    ) -> None:
        """Operator confirms a flag.

        By default the run closes here and nothing else is dispatched. A report
        is written only when asked for (``writeup=True`` here,
        ``Run.write_report()``, or the run page's button): in a competition the
        next challenge matters more than the writeup, and an automatic writer
        session spends time and money before anyone wanted it.

        When a writeup *is* requested at confirmation time, order matters: the
        writer is dispatched *before* the run closes and granted a post-stop
        exemption. Closing first and then dispatching would leave the writer
        queued behind a stopped run (or cancelled by the shutdown sweep).

        When a verifier is still reviewing the flag, the confirm waits for it
        (bounded by ``limits.confirm_verifier_wait_s``). Used to be the single
        biggest hole in the workflow: the operator confirmed, the run closed
        under the in-flight verifier, and the independent-reproduction gate
        died at zero turns — 15 of 30 verifiers in the live database, one of
        them 42 seconds before its reproduction completed (run 81). A refuted
        verdict stops the confirm; a timeout lets it proceed and the verifier
        outlives the close with its verdict still recorded.
        """
        flag = self.store.get_flag(flag_id)
        if flag is None or flag.run_id != self.id:
            raise ValueError(f"运行 {self.id} 里没有 flag {flag_id}")
        if not await self._confirm_wait_for_verifier(flag):
            return
        self.store.set_flag_status(flag_id, FlagStatus.CONFIRMED, note=note or "操作员确认")
        self._emit("flag.confirmed", {"flag_id": flag_id, "value": flag.value})
        summary = f"flag 已确认：{flag.value}"
        if self._closed:
            return
        # Confirming is also the operator's answer to a paused run. The lead's
        # own `finish_run` (if any) must not close the run before the writer has
        # produced the writeup, so the close happens at the end of this method.
        self._finish_status = RunStatus.SOLVED
        self._finish_summary = summary
        self._operator_closing = True
        wanted = self.cfg.limits.writeup_on_confirm if writeup is None else writeup
        if not wanted:
            self._emit(
                "run.confirmed",
                {"flag_id": flag_id, "value": flag.value, "writeup": False,
                 "note": "已确认，未写报告；需要时在运行页点「写报告」"},
            )
            await self._maybe_distill_on_confirm()
            await self.shutdown(RunStatus.SOLVED, summary, "操作员确认 flag")
            return
        writer = await self._dispatch_writer(flag)
        self._emit("run.writer_dispatched", {"task_id": writer.id})
        # Let the writer finish (bounded), then close for real.
        await self.wait([writer.id], timeout_s=self.cfg.limits.task_timeout_s)
        self._collect_writeup()
        await self._maybe_distill_on_confirm()
        await self.shutdown(RunStatus.SOLVED, summary, "操作员确认 flag")

    async def _confirm_wait_for_verifier(self, flag: Any) -> bool:
        """Hold the confirm while a verifier is still reviewing, then decide.

        Returns False only when the verdict that arrived during the wait
        refuted the flag: confirming a value the independent reviewer just
        disproved would send a wrong answer to the platform. Any other
        outcome proceeds — and a verifier still running when the bounded
        wait expires is exempted from the shutdown sweep, so its verdict is
        recorded on the flag even though the run has closed.
        """
        in_flight = [
            t for t in self.store.list_tasks(self.id)
            if t.role == "verifier" and not t.terminal
        ]
        if not in_flight:
            return True
        wait_s = self.cfg.limits.confirm_verifier_wait_s
        self._emit(
            "flag.confirm_pending",
            {"flag_id": flag.id, "value": flag.value,
             "verifier_ids": [t.id for t in in_flight], "wait_s": wait_s,
             "note": f"验证器仍在独立复核，确认动作最多等待 {wait_s} 秒"},
        )
        await self.wait([t.id for t in in_flight], timeout_s=wait_s)
        current = self.store.get_flag(flag.id)
        if current is not None and current.status == FlagStatus.REJECTED:
            self._emit(
                "flag.confirm_refuted",
                {"flag_id": flag.id, "value": flag.value,
                 "note": "确认被验证器驳回：独立复核推翻了该候选，flag 已标为 rejected，"
                         "运行保持打开。确认这是误报可再次确认。"},
            )
            return False
        for t in self.store.list_tasks(self.id):
            if t.role == "verifier" and not t.terminal:
                self._post_stop_ok.add(t.id)
        return True

    async def _maybe_distill_on_confirm(self) -> None:
        """Distil a technique note, if the operator turned that on.

        ``distill_on_confirm`` is off by default, so this is inert unless asked
        for. It must never cost the close: the run is solved either way, and a
        failed distillation is only a lost note, not a lost solve.
        """
        if not self.cfg.limits.distill_on_confirm:
            return
        try:
            out = await self.distill_technique()
        except Exception as exc:  # noqa: BLE001 - a note is never load-bearing
            self._emit("skill.note_failed", {"run_id": self.id, "error": str(exc)})
            return
        if out.get("error"):
            self._emit("skill.note_failed", {"run_id": self.id, "error": out["error"]})

    async def _dispatch_writer(self, flag: Any) -> Any:
        writer = await self.dispatch(
            "writer",
            objective=(
                f"为已确认的 flag {flag.value} 写解题报告。"
                f"保存到 {self.workspace.root / 'writeup.md'}。"
            ),
            context=f"已确认的 flag：{flag.value}\n来源：{flag.source}\n方法：{flag.method}",
        )
        self._post_stop_ok.add(writer.id)
        return writer

    async def write_report(self) -> dict[str, Any]:
        """Write the report on request, against a run that is already closed.

        This is the other half of ``writeup_on_confirm = false``: confirming a
        flag must not start a writer, but the report still has to be reachable
        afterwards. The run is not reopened — only a writer session is started,
        which is why the extra budget exists: a run that closed at its dollar
        ceiling would otherwise hand the writer a zero ceiling and it would
        never start.
        """
        if any(
            t.role == "writer" and not t.terminal for t in self.store.list_tasks(self.id)
        ):
            return {"error": "已经有一个报告任务在跑，等它结束"}
        flag = next(
            (f for f in self.store.list_flags(self.id) if f.status == FlagStatus.CONFIRMED),
            None,
        )
        if flag is None:
            return {"error": "这次运行还没有已确认的 flag；先确认 flag 再写报告"}
        # Gateway dollars — same unit the reservation arithmetic uses. The
        # configured number is CLI dollars.
        self._extra_budget = max(0.0, self.cfg.limits.task_budget_usd) / (
            self.cfg.models.cli_factor(self.cfg.models.writer)
        )
        try:
            writer = await self._dispatch_writer(flag)
            self._emit(
                "run.writer_dispatched", {"task_id": writer.id, "on_request": True}
            )
            await self.wait([writer.id], timeout_s=self.cfg.limits.task_timeout_s)
            self._collect_writeup()
        finally:
            self._extra_budget = 0.0
        target = self.workspace.root / "writeup.md"
        return {
            "task_id": writer.id,
            "path": str(target),
            "written": target.exists(),
        }

    def _collect_writeup(self) -> None:
        """Make sure the writeup sits at the run root, wherever the writer put it.

        The writer's cwd is ``work/``, and models write "the current directory"
        literally. The run root is where an operator looks, so move it there
        rather than documenting an inconsistency.
        """
        target = self.workspace.root / "writeup.md"
        if target.exists():
            return
        for candidate in sorted(self.workspace.work.rglob("writeup*.md")):
            try:
                shutil.copy2(candidate, target)
            except OSError:
                continue
            self._emit("run.writeup", {"path": str(target), "from": str(candidate)})
            return

    async def distill_technique(self) -> dict[str, Any]:
        """Turn a solved run into a technique note in the skill library.

        The case library remembers challenges; this is how the platform
        remembers *methods*. A card is keyed to one challenge's vocabulary, so
        a Wiener solve helps the next Wiener-flavoured wording and nothing
        else. The note a writer distils here is the other shape: trigger
        conditions, steps, verified pitfalls — readable by any future worker
        of the direction through ``read_skill_doc``, whatever the wording of
        the challenge in front of it.

        Explicit like ``write_report`` (same philosophy as
        ``writeup_on_confirm = false``): it spends a session, so the operator
        asks for it — after the competition, in batch, over the solves that
        proved something.
        """
        if any(
            t.role == "writer" and not t.terminal for t in self.store.list_tasks(self.id)
        ):
            return {"error": "已经有一个报告任务在跑，等它结束"}
        flag = next(
            (f for f in self.store.list_flags(self.id) if f.status == FlagStatus.CONFIRMED),
            None,
        )
        if flag is None:
            return {"error": "只有已确认 flag 的运行才能沉淀技能：方法没被验证过，写进去就是教人走错路"}
        skill = ROLE_SKILL.get(self._dominant_direction(), "")
        if not skill:
            return {"error": "无法判断这道题属于哪个方向（分类未知且没有可归的主方向）"}
        # Gateway dollars, like the writeup grant above.
        self._extra_budget = max(0.0, self.cfg.limits.task_budget_usd) / (
            self.cfg.models.cli_factor(self.cfg.models.writer)
        )
        try:
            writer = await self.dispatch(
                "writer",
                objective=(
                    "把这次运行里被验证过的解法蒸馏成一篇可复用的技术笔记，"
                    f"保存为 {self.workspace.work / 'technique.md'}。"
                ),
                context=prompts.distill_context(
                    challenge=self.challenge.name,
                    method=flag.method or "",
                    route=[
                        f"{t.role}[{t.status}] {(t.result or {}).get('summary', '')}"
                        for t in self.store.list_tasks(self.id)
                        if t.role not in ("writer", "verifier")
                    ],
                    dead_ends=[
                        f.content
                        for f in self.store.list_facts(self.id, limit=200)
                        if f.kind == "dead_end"
                    ],
                ),
            )
            self._post_stop_ok.add(writer.id)
            self._emit("skill.distill_dispatched", {"task_id": writer.id, "skill": skill})
            await self.wait([writer.id], timeout_s=self.cfg.limits.task_timeout_s)
        finally:
            self._extra_budget = 0.0
        return self._install_learned_note(skill)

    def _learned_notes_block(self, skill: str) -> str:
        """Index the technique notes distilled from solved runs of a direction.

        A note lives in the skill directory as ``learned-*.md``, reachable
        through ``read_skill_doc`` — but only if the worker knows it exists:
        the vendored ``SKILL.md`` index does not list platform-added files,
        and an undetected note teaches nothing (the same lesson as
        ``read_skill_doc`` itself: the census found the index's own technique
        files opened zero times across 134 sessions).

        Ranked, not merely dated. "Newest eight" was the case library's failure
        mode repeated: once a direction accumulates solves, most of the index is
        a hundred notes of which two match, and the match has to win on date.
        Selection and the usage check live in :mod:`ctfagent.notes`; the paths
        listed here are what the close charges an outcome to.
        """
        root = Path(self.cfg.skills_dir) / skill
        if not root.is_dir():
            return ""
        known = notes.index(root)
        if not known:
            return ""
        picked = notes.rank(known, self._note_topic(), notes.boilerplate_of(known))
        if picked:
            # A run dispatches several directions; the index is re-rendered per
            # dispatch, so the list has to accumulate. Overwriting it with the
            # last dispatch's notes would charge only that direction's memory,
            # and a run that used a crypto note after a web dispatch would
            # report nothing.
            for note in picked:
                if note.path not in self._shown_notes:
                    self._shown_notes.append(note.path)
            try:
                self.store.set_run_notes(self.id, [str(p) for p in self._shown_notes])
            except Exception:  # noqa: BLE001 - accounting is not load-bearing
                pass
        return notes.render_index(picked)

    def _note_topic(self) -> str:
        '''The words a note should match: what this challenge is about.'''
        challenge = self.challenge
        return " ".join(
            [
                str(challenge.name or ""),
                str(challenge.description or ""),
                " ".join(str(h) for h in (challenge.hints or [])),
                str(challenge.remote or ""),
                " ".join(str(f) for f in (challenge.files or [])),
            ]
        )

    def _dominant_direction(self) -> str:
        """The direction this run actually was: the challenge's category when
        known (triage writes it back now), else the worker role that did the
        most turns."""
        category = (self.challenge.category or "").lower()
        if category in ROLE_SKILL and category not in ("triage",):
            return category
        best, best_turns = "", 0
        for task in self.store.list_tasks(self.id):
            if task.role in ("triage", "verifier", "writer"):
                continue
            # A task that never got a turn says nothing about the direction; it
            # was usually cancelled by the close itself.
            if task.turns <= best_turns:
                continue
            best, best_turns = task.role, task.turns
        return best

    def _writeback_direction(self) -> str:
        """File the run's direction on the challenge row when triage never did.

        Triage is supposed to name the category, but on the live database it
        stopped running after run 16 and 12 of 15 cards were filed as
        ``unknown`` — which costs ``caselib`` its strongest retrieval signal and
        files every note under a direction the next challenge cannot match. The
        worker that did the most turns is the next best evidence, and it exists
        on every run that did any work.

        Runs before ``caselib.save``, because the card copies the category.
        """
        if (self.challenge.category or "").lower() not in ("", "unknown"):
            return ""
        direction = self._dominant_direction()
        if not direction or not self.challenge.id:
            return ""
        # Evidence, not a guess: a worker cancelled on turn 2 says nothing about
        # what the challenge was. A wrong category is worse than ``unknown`` —
        # it files the card against a direction the next challenge will be
        # penalised for sharing — so a run that barely started leaves the row
        # alone and a later run decides.
        turns = max(
            (t.turns for t in self.store.list_tasks(self.id) if t.role == direction),
            default=0,
        )
        if turns < MIN_DIRECTION_TURNS:
            return ""
        if not self.store.set_challenge_category(self.challenge.id, direction):
            return ""
        previous = self.challenge.category
        self.challenge.category = direction
        self._emit(
            "challenge.categorized",
            {"category": direction, "previous": previous, "source": "dominant_role"},
        )
        return direction

    def _install_learned_note(self, skill: str) -> dict[str, Any]:
        """Copy the writer's note into the skill library, scrubbed and stamped.

        The session may only write inside its workspace (the guard enforces
        it), so the library write happens here — with the same flag scrubbing
        the case library applies, because a technique note that carries the
        answer teaches the next run to submit it. The filename is derived from
        the content, so re-distilling a run updates its note instead of piling
        up near-duplicates.
        """
        source = self.workspace.work / "technique.md"
        text = source.read_text(encoding="utf-8", errors="replace") if source.exists() else ""
        text = caselib.strip_flags(text).strip()
        if len(text) < 200:
            return {"error": "writer 没有产出可用的笔记（technique.md 缺失或太短），不安装"}
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        note_dir = Path(self.cfg.skills_dir) / skill
        if not note_dir.is_dir():
            return {"error": f"技能目录不存在：{note_dir}"}
        header = (
            f"<!-- 沉淀自 run {self.id}（{self.challenge.name}，{date.today().isoformat()}）。"
            f"由解出该题的运行蒸馏；flag 字面量已擦除。 -->\n\n"
        )
        # Date first in the name: the worker-side index shows the newest notes,
        # and name order is the only chronology a flat directory has.
        target = note_dir / f"learned-{date.today():%Y%m%d}-{digest}.md"
        target.write_text(header + text + "\n", encoding="utf-8", newline="\n")
        self._emit(
            "skill.note_added",
            {"path": str(target), "skill": skill, "run_id": self.id},
        )
        return {"skill": skill, "path": str(target), "written": True}

    # --------------------------------------------------------------- watchdog
    async def _budget_watchdog(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(2)
            try:
                if await self._watchdog_tick():
                    return
            except Exception:  # noqa: BLE001 - the watchdog must outlive bugs
                continue

    async def _watchdog_tick(self) -> bool:
        """One watchdog pass. Returns True when this tick closed the run.

        Split from the loop so the checks are testable without waiting on
        real timers. Order matters: silence is checked before the ceilings,
        because a run whose event stream has gone dead must be reaped even
        while its own clocks believe everything is fine — observed live on
        run 82, which sat ``running`` for half a day with a silent session
        while every per-run clock failed to fire.
        """
        limits = self.cfg.limits
        if self._stop.is_set() or self._finish_status:
            return False
        #: Where new ground stops being worth opening. Cost only moves when a
        #: session emits its result, so the run can gain several dollars between
        #: two polls; the soft stop makes that last jump the end of exploration
        #: rather than the thing that blows through the ceiling.
        soft_ceiling = limits.run_budget_usd * 0.85
        if not self._soft_stopped and self.cost_usd > soft_ceiling:
            self._soft_stopped = True
            self._emit(
                "run.budget_soft",
                {
                    "cost_usd": round(self.cost_usd, 2),
                    "run_budget_usd": limits.run_budget_usd,
                    "message": "接近预算上限：停止派发新的探索工单，在跑的收尾",
                },
            )
        # The zombie reaper. A run that has emitted nothing for this long is
        # dead regardless of what its loop thinks — the ceiling path below
        # tries to give the lead a closing turn through a client that may be
        # the very thing that is hung, so silence skips it entirely.
        if time.time() - self._last_event_ts > _ZOMBIE_SILENCE_S:
            self._accepting = False
            await self.shutdown(
                RunStatus.FAILED,
                "事件流长时间静默，看门狗强制关闭",
                "看门狗:事件静默",
            )
            return True
        reason = ""
        dollar_ceiling = False
        if self.elapsed > limits.run_timeout_s:
            reason = f"运行超过 {limits.run_timeout_s} 秒上限"
        elif self.cost_usd > limits.run_budget_usd:
            reason = f"运行超过 ${limits.run_budget_usd:.2f} 预算"
            dollar_ceiling = True
        if not reason:
            return False
        # Stop new work, let the lead account for itself, then close.
        # A dollar ceiling gets no extra turn: the session is already over
        # budget and another turn is spent, not saved.
        # The closing turn is bounded: it goes through the very client that
        # may be hung (run 82's watchdog hung here for half a day inside an
        # unbounded stream on a dead session), so it must never be trusted
        # with the close.
        self._accepting = False
        if self._main_client is not None and not dollar_ceiling:
            try:
                await asyncio.wait_for(
                    self._summarize_after_ceiling(self._main_client),
                    timeout=_CEILING_SUMMARY_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001 - a summary is best effort
                pass
        await self.shutdown(RunStatus.FAILED, reason, "预算或时间用尽")
        return True


class RunManager:
    """Owns runs, the global session pool and the admission queue."""

    def __init__(
        self,
        store: Store,
        bus: EventBus,
        cfg: AppConfig,
        backend: AgentBackend,
        sandbox: SandboxStatus,
        skills_dir: Path,
        sealed_roots: list[Path] | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.cfg = cfg
        self.backend = backend
        self.sandbox = sandbox
        self.skills_dir = skills_dir
        #: Directories every session of every run in this manager may not read.
        #: The benchmark sets it so its answer key cannot be grepped.
        self.sealed_roots = list(sealed_roots or [])
        self.pool = SessionPool(cfg.limits.max_sessions)
        self.runs: dict[int, Run] = {}
        self._root = cfg.data_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._admission = asyncio.Semaphore(max(1, cfg.limits.max_active_runs))
        self._launching: dict[int, asyncio.Task[None]] = {}

    def prepare(
        self,
        challenge: int | Any,
        objective: str = "",
        *,
        name: str = "",
        sealed_roots: list[Path] | None = None,
        folder_id: int | None = None,
    ) -> Run:
        """Create the Run object without starting its sessions.

        ``challenge`` is either an id (the operator registered a directory) or a
        :class:`~ctfagent.models.Challenge` that was typed into the web UI and
        is being registered on the spot. ``name`` overrides the display name in
        the latter case only when the challenge itself has none.

        ``sealed_roots`` adds directories this one run may not read, on top of
        the manager-wide list. The benchmark uses it for a challenge whose
        original material folder still holds the answer: sealing the manifest
        folder alone left the sibling folder readable.

        ``folder_id`` files the run under one operator-made group (a
        competition, a practice batch). It is display organisation only — it
        changes nothing about how the run executes.

        Separated from :meth:`start` so callers (the test suite, and anything
        that wants to wire a hook before the first turn) can attach to a run
        before its main-agent loop begins.
        """
        if isinstance(challenge, int):
            row = self.store.get_challenge(challenge)
            if row is None:
                raise ValueError(f"没有这道题：{challenge}")
            challenge_id = row.id
            challenge_obj = row
        else:
            challenge_obj = challenge
            if not challenge_obj.name:
                challenge_obj.name = name or "未命名题目"
            challenge_id = self.store.add_challenge(challenge_obj)
            challenge_obj.id = challenge_id
        run = self.store.create_run(
            challenge_id, objective or f"解出 {challenge_obj.name} 并找到 flag", folder_id=folder_id
        )
        ctx = RunContext(
            store=self.store,
            bus=self.bus,
            cfg=self.cfg,
            backend=self.backend,
            pool=self.pool,
            workspaces_root=self._root,
            skills_dir=self.skills_dir,
            sandbox=self.sandbox,
            sealed_roots=self.sealed_roots + list(sealed_roots or []),
        )
        live = Run(ctx, run.id, challenge_obj)
        self.runs[run.id] = live
        self._emit_queued(live)
        return live

    async def start(
        self,
        challenge: int | Any,
        objective: str = "",
        *,
        name: str = "",
        sealed_roots: list[Path] | None = None,
        folder_id: int | None = None,
    ) -> Run:
        live = self.prepare(
            challenge, objective, name=name, sealed_roots=sealed_roots, folder_id=folder_id
        )
        self.launch(live)
        return live

    def launch(self, run: Run) -> None:
        """Queue an already-prepared run for execution.

        Separated from :meth:`prepare` so a caller can put the operator's
        uploads into the workspace *before* the kickoff turn reads the file
        list. Starting first and uploading after would race the main loop.
        """
        self._launching[run.id] = asyncio.create_task(self._admit(run))

    def _emit_queued(self, run: Run) -> None:
        self.store.set_run_status(run.id, RunStatus.QUEUED)
        self.bus.publish(run.id, "run.queued", {"run_id": run.id})

    async def _admit(self, run: Run) -> None:
        try:
            async with self._admission:
                self._launching.pop(run.id, None)
                await run.start()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.bus.publish(run.id, "run.error", {"error": f"{type(exc).__name__}: {exc}"})
            self.store.set_run_status(run.id, RunStatus.FAILED, summary=str(exc), reason="启动失败")

    def get(self, run_id: int) -> Run | None:
        return self.runs.get(run_id)

    def attach(self, run_id: int) -> Run | None:
        """Rebuild a Run object for a run that exists in the DB but not in this process.

        Used by ``ctfagent confirm`` from a second terminal: the flag decision is
        real, and the writeup still has to be produced. The rebuilt Run has no
        main session (the original process owns that), so it is only good for
        dispatching the writer and closing the run out.
        """
        existing = self.runs.get(run_id)
        if existing is not None:
            return existing
        run_row = self.store.get_run(run_id)
        if run_row is None:
            return None
        challenge = self.store.get_challenge(run_row.challenge_id)
        if challenge is None:
            return None
        ctx = RunContext(
            store=self.store,
            bus=self.bus,
            cfg=self.cfg,
            backend=self.backend,
            pool=self.pool,
            workspaces_root=self._root,
            skills_dir=self.skills_dir,
            sandbox=self.sandbox,
            sealed_roots=self.sealed_roots,
        )
        live = Run(ctx, run_id, challenge, adopt=True)
        self.runs[run_id] = live
        return live

    async def revive(self, run_id: int) -> Run | None:
        """Attach a run and start listening for the operator again.

        A message on a closed run used to be refused outright: the session that
        could answer it lived in a process that may have exited hours ago. The
        transcript is on disk and the CLI can resume it, so the honest answer is
        to bring the conversation back rather than disable the chat box.

        Returns None when there is nothing to resume (no challenge row, no
        recorded session, or a run with no money left) — the caller then
        reports that instead of pretending the message was delivered.
        """
        live = self.runs.get(run_id)
        if live is not None and not live.closed:
            return live
        if live is None:
            live = self.attach(run_id)
        if live is None:
            return None
        if not live._main_session:
            # Nothing was ever recorded for the lead: talking to it now would
            # mean inventing a conversation the agent cannot see.
            self.runs.pop(run_id, None)
            return None
        if live.cost_usd > self.cfg.limits.run_budget_usd:
            # Reopening an insolvent run produces no answer at all: the
            # watchdog polls every two seconds and closes it again as soon as
            # the first turn starts. Observed on run 18 — an operator message on
            # a $35.78/$30.00 run reopened the session, sent one request, and
            # `run.finished` with the same ceiling summary landed six seconds
            # later. Refusing up front at least tells the operator what is
            # wrong; the timer restarts on reopen but the ledger does not.
            self.runs.pop(run_id, None)
            return None
        await live.reopen()
        return live

    async def stop(self, run_id: int, reason: str = "操作员停止") -> None:
        run = self.runs.get(run_id)
        if run:
            await run.shutdown(RunStatus.STOPPED, reason, reason)

    async def delete(self, run_id: int) -> bool:
        """Stop a run if it is live, then forget it entirely.

        The rail is the operator's job list: a run that is over and no longer
        wanted must leave it, or every restart re-surfaces work that was
        abandoned. Live sessions are closed first because deleting the row
        under a running worker leaves it writing into a missing run.
        """
        run = self.runs.pop(run_id, None)
        if run is not None and not run.closed:
            await run.shutdown(RunStatus.STOPPED, "运行已删除", "deleted")
        task = self._launching.pop(run_id, None)
        if task is not None:
            task.cancel()
        row = self.store.get_run(run_id)
        if row is None:
            return False
        workspace = Path(row.workspace) if row.workspace else None
        self.store.delete_run(run_id)
        if workspace is not None:
            # The run's directory sits at <data_dir>/run-<id>/; removing the
            # parent also takes care of the challenge-name level below it.
            root = workspace.parent if workspace.parent.name == f"run-{run_id}" else workspace
            shutil.rmtree(root, ignore_errors=True)
        return True

    async def stop_all(self) -> None:
        for task in list(self._launching.values()):
            task.cancel()
        for run in list(self.runs.values()):
            await run.shutdown(RunStatus.STOPPED, "服务正在关闭", "shutdown")
