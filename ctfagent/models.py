"""Domain model: dataclasses + enums shared by the DB layer, orchestrator and UI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    NEEDS_INPUT = "needs_input"
    SOLVED = "solved"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {RunStatus.SOLVED, RunStatus.FAILED, RunStatus.STOPPED}


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"      # the worker could not proceed (missing tool, bad input)
    EXHAUSTED = "exhausted"  # hit a budget/step/time ceiling
    STOPPED = "stopped"      # killed by the operator or the run ending
    FAILED = "failed"        # crashed

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.DONE,
            TaskStatus.BLOCKED,
            TaskStatus.EXHAUSTED,
            TaskStatus.STOPPED,
            TaskStatus.FAILED,
        }


class FlagStatus(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class FactKind(StrEnum):
    OBSERVATION = "observation"
    INSIGHT = "insight"
    CREDENTIAL = "credential"
    DEAD_END = "dead_end"
    FLAG_CANDIDATE = "flag_candidate"
    #: A question the team cannot answer yet. Kept as a first-class record so it
    #: can be dispatched, rather than silently becoming a guessed conclusion.
    UNKNOWN = "unknown"


#: How a fact is grounded. This is REA's `confidence` axis reduced to what a CTF
#: run needs: "I ran it", "I derived it", "I inferred it", "I read it somewhere".
#: Empty means the fact predates this field.
BASIS_VALUES = ("observed", "derived", "inferred", "external")

#: Evidence keys naming the action a fact rests on, and the raw result it saw.
#: Evidence is otherwise free-form; these are the two things a later worker
#: needs to re-run a claim instead of trusting it, so ``observed`` without them
#: is a label with nothing behind it.
EVIDENCE_ACTION_KEYS = ("command", "request", "cmd", "url", "input")
EVIDENCE_RESULT_KEYS = ("output", "response", "stdout", "body", "result", "raw", "dump")
#: A structured RE record is both: `re_evidence_get` replays the call and its
#: envelope, so a fact quoting one is checkable without retyping a blob.
EVIDENCE_RECORD_KEY = "evidence_id"


def _has_any(evidence: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(str(evidence.get(k) or "").strip() for k in keys)


def grounding_gap(basis: str, evidence: dict[str, Any] | None) -> str:
    """Why ``basis`` is not backed by evidence, or "" when it is.

    The blackboard is re-read by every later worker, so an unqualified
    ``observed`` is a claim someone else builds on without being able to check
    it. The prompts ask for the evidence; this notices when it did not arrive.
    Orchestrator-generated facts (budget hand-offs) set ``system`` because they
    report a mechanical event, not an investigation.
    """
    if basis not in ("observed", "derived"):
        return ""
    ev = evidence or {}
    if ev.get("system"):
        return ""
    if str(ev.get(EVIDENCE_RECORD_KEY) or "").strip():
        return ""
    if not _has_any(ev, EVIDENCE_ACTION_KEYS):
        return "没有写明跑了什么命令/请求"
    if basis == "observed" and not _has_any(ev, EVIDENCE_RESULT_KEYS):
        return "没有附上原始输出/响应"
    return ""


#: Evidence keys that scope a negative claim: how much of the space the run
#: actually searched, and the sample that proved the detector can find a known
#: answer. A ``dead_end`` with neither is "I did not find it", which is a fact
#: about the search, not about the target.
EVIDENCE_SCOPE_KEYS = ("scope", "searched", "space")
EVIDENCE_CONTROL_KEYS = ("control", "positive_control", "planted")
#: Set by ``Run.record_fact`` when a dead end arrived without the two above.
#: The record is kept — it is still worth telling the next worker where someone
#: looked — but it must not be rendered as a settled result.
NEGATIVE_GAP_KEY = "negative_gap"


def negative_gap(kind: str, evidence: dict[str, Any] | None) -> str:
    """Why a ``dead_end`` is only an unverified negative, or "" when it is scoped.

    Live failure this exists for: a run recorded "seed low 24 bits free (2^24):
    no solution" as ``observed``. It had the command and the output, so
    ``grounding_gap`` passed it — but it had searched the wrong space (the seed
    itself, not the 256-bit value the generator concatenates from eight draws),
    and its BSGS self-test proved the search could find *some* key, not that it
    could find this one. The card library then showed the next run
    "已证实的死路: 2^24 has no solution", which is false.

    The evidence is not missing; its scope is unstated. So the fix is a second,
    narrower check rather than a stricter ``grounding_gap``: a negative must say
    what it covered and what control proved the detector works.
    """
    if str(kind) != FactKind.DEAD_END:
        return ""
    ev = evidence or {}
    if ev.get("system") or str(ev.get(EVIDENCE_RECORD_KEY) or "").strip():
        return ""
    if not _has_any(ev, EVIDENCE_SCOPE_KEYS):
        return "没写清搜索/实验覆盖了多大空间"
    if not _has_any(ev, EVIDENCE_CONTROL_KEYS):
        return "没跑阳性对照（先证明检测器能找回已知答案）"
    return ""


#: Worker roles. `triage` routes, the rest are directions; `verifier` independently
#: reproduces a candidate flag; `writer` produces the writeup.
ROLES = (
    "triage",
    "web",
    "crypto",
    "pwn",
    "reverse",
    "misc",
    "forensics",
    "malware",
    "ai_ml",
    "osint",
    "verifier",
    "writer",
)

#: Which vendored skill each role should load first.
ROLE_SKILL = {
    "triage": "solve-challenge",
    "web": "ctf-web",
    "crypto": "ctf-crypto",
    "pwn": "ctf-pwn",
    "reverse": "ctf-reverse",
    "misc": "ctf-misc",
    "forensics": "ctf-forensics",
    "malware": "ctf-malware",
    "ai_ml": "ctf-ai-ml",
    "osint": "ctf-osint",
    "verifier": "solve-challenge",
    "writer": "ctf-writeup",
}


#: Which roles get the reverse-engineering MCP server. `triage` needs it because
#: "is this a reversing challenge" is answered by inspecting headers; `pwn` needs
#: the mitigations block; `forensics` meets ELF/PE containers and firmware images.
#: Web, crypto and misc workers would only get ten more tools in their context.
RE_ROLES = ("triage", "reverse", "pwn", "malware", "forensics")

#: Roles that close out work already done rather than opening new ground. A run
#: near its budget ceiling still has to be able to verify a flag it already has
#: and write it up: refusing those to save cents throws away the whole run.
CLOSING_ROLES = ("verifier", "writer")

#: What a verifier is allowed to conclude. Passed as a structured field on
#: ``task_complete``: parsed out of prose, 6 of 15 verdicts in the database came
#: back as "none" because the verifier wrote its conclusion in some other shape,
#: and each of those left a flag waiting on a human to read the tail.
VERDICTS = ("reproduced", "refuted", "inconclusive")

#: A verifier re-derives the value independently, so for these categories it
#: needs the same primitives the claimant had — otherwise "reproduced" degrades
#: into "I ran their script". Other categories get no RE tools at all.
RE_CATEGORIES = ("reverse", "pwn", "malware", "forensics")

#: Display labels. The stored values stay as they are (they are part of the
#: database and the event stream); only what the operator reads is translated.
RUN_STATUS_LABEL = {
    "queued": "排队",
    "running": "运行中",
    "needs_input": "待确认",
    "solved": "已解出",
    "failed": "失败",
    "stopped": "已停止",
    "interrupted": "中断",
}

TASK_STATUS_LABEL = {
    "queued": "排队",
    "running": "运行中",
    "done": "完成",
    "blocked": "受阻",
    "exhausted": "额度用尽",
    "stopped": "已停止",
    "failed": "失败",
}

FLAG_STATUS_LABEL = {
    "candidate": "候选",
    "verified": "已复现",
    "rejected": "已驳回",
    "confirmed": "已确认",
}

FACT_KIND_LABEL = {
    "observation": "观察",
    "insight": "结论",
    "credential": "凭据",
    "dead_end": "死路",
    "flag_candidate": "候选 flag",
    "unknown": "未解问题",
}

BASIS_LABEL = {
    "observed": "实测",
    "derived": "推导",
    "inferred": "推测",
    "external": "外部资料",
    "": "",
}

CONFIDENCE_LABEL = {"high": "高", "medium": "中", "low": "低"}

EVENT_LABEL = {
    "run.started": "运行开始",
    "run.finished": "运行结束",
    "run.needs_input": "等待确认",
    "run.question": "提问",
    "run.error": "运行出错",
    "run.warning": "运行警告",
    "run.writeup": "报告已生成",
    "run.writer_dispatched": "派发报告任务",
    "cases.loaded": "载入战例",
    "run.attachments": "附件更新",
    "task.dispatched": "派发任务",
    "task.started": "任务开始",
    "task.finished": "任务结束",
    "task.result": "任务结果",
    "task.help_requested": "请求协助",
    "task.wrapup": "上限后收尾",
    "fact.added": "黑板记录",
    "fact.merged": "重复记录已合并",
    "fact.downgraded": "证据不足已降级",
    "flag.proposed": "提交候选",
    "flag.verdict": "验证结论",
    "flag.confirmed": "flag 确认",
    "chat.user": "对话",
    "main.message": "主 agent",
    "agent.event": "代理",
    "session.error": "会话出错",
    "text_delta": "增量文本",
}

ROLE_LABEL = {
    "main": "主 agent",
    "unknown": "未分类",
    "triage": "分诊",
    "web": "Web",
    "crypto": "密码",
    "pwn": "Pwn",
    "reverse": "逆向",
    "misc": "杂项",
    "forensics": "取证",
    "malware": "恶意代码",
    "ai_ml": "AI/ML",
    "osint": "情报",
    "verifier": "验证",
    "writer": "报告",
}


@dataclass
class Challenge:
    id: int
    name: str
    category: str
    description: str
    files: list[str] = field(default_factory=list)
    remote: str = ""
    flag_format: str = ""
    hints: list[str] = field(default_factory=list)
    time_limit_s: int = 3600
    source_dir: str = ""
    created_at: str = ""
    #: Set when the challenge was pulled from a competition platform. A
    #: submission addresses the challenge by this pair, not by the local id.
    competition_id: int | None = None
    external_id: str = ""


@dataclass
class Competition:
    """One remote CTF platform account (currently CTFd-compatible)."""

    id: int
    name: str
    platform: str = "ctfd"
    base_url: str = ""
    token: str = ""
    created_at: str = ""
    last_synced_at: str = ""
    last_sync_note: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.token.strip())


@dataclass
class Provider:
    """One model endpoint + token, the way a switcher keeps them in a list.

    The reason this exists as a row rather than only as ``config.toml``: changing
    gateways used to mean hand-editing three keys in a file guarded by an
    ``ANTHROPIC_AUTH_TOKEN`` environment variable, and the operator could not
    see which of the several endpoints they had tried still worked. A row keeps
    the address, the masked token, the models that endpoint actually served, and
    whether it was the last one verified.

    ``token_kind`` records *which* variable carries the secret: gateways differ
    on ``ANTHROPIC_AUTH_TOKEN`` vs ``ANTHROPIC_API_KEY`` and the child session
    has to be given the same one the gateway expects.
    """

    id: int
    name: str
    base_url: str = ""
    auth_token: str = ""
    api_key: str = ""
    #: ``anthropic`` (default) or ``openai`` (a translation proxy must sit in front).
    api_format: str = "anthropic"
    #: Model names this endpoint returned from ``/v1/models``; empty = never probed.
    models: list[str] = field(default_factory=list)
    #: Last probe result, so a saved-but-broken endpoint is visible before a run
    #: starts. ``ok`` / ``failed`` / ``""`` (never probed).
    last_probe: str = ""
    last_probe_note: str = ""
    last_probed_at: str = ""
    #: Where the row came from: ``""`` (typed here) or ``ccswitch`` (imported).
    imported_from: str = ""
    created_at: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and (self.auth_token.strip() or self.api_key.strip()))

    @property
    def secret(self) -> str:
        return self.auth_token or self.api_key


@dataclass
class Submission:
    """One attempt to send a confirmed flag to the platform."""

    id: int
    flag_id: int
    competition_id: int
    external_id: str
    status: str
    message: str = ""
    created_at: str = ""


@dataclass
class Run:
    id: int
    challenge_id: int
    objective: str
    status: str
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    cost_usd: float = 0.0
    summary: str = ""
    workspace: str = ""
    stop_reason: str = ""
    #: The main agent's CLI session. Empty until its first ``init`` event.
    main_session_id: str = ""
    #: Cumulative spend the CLI reports for that session (see ``db.migrate``).
    main_cost_usd: float = 0.0
    #: 1 once the close charged this run's outcome to the case cards / technique
    #: notes it used. ``ctfagent memory`` reads them to tell "the loop ran" from
    #: "the loop exists".
    cases_credited: int = 0
    notes_credited: int = 0
    #: Which folder the operator filed this run under (``None`` = unfiled).
    #: Runs are the unit the rail lists, so the folder hangs off the run: two
    #: attempts at the same challenge can sit in different competitions.
    folder_id: int | None = None


@dataclass
class Folder:
    """A named group of runs — one competition, or one practice batch."""

    id: int
    name: str
    created_at: str = ""


@dataclass
class Task:
    id: int
    run_id: int
    role: str
    objective: str
    context: str
    status: str
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: The board entry this task is executing, if any. Empty means the task was
    #: dispatched the old way (centrally, without a hypothesis) — both shapes
    #: coexist so the swarm can be rolled out per run.
    hypothesis_id: int | None = None

    @property
    def terminal(self) -> bool:
        return TaskStatus(self.status).terminal


@dataclass
class Fact:
    id: int
    run_id: int
    task_id: int | None
    kind: str
    content: str
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    #: observed / derived / inferred / external; empty for rows written before
    #: the column existed.
    basis: str = ""
    #: Set when this record closes an earlier `unknown` fact.
    resolves: int | None = None
    #: Set when this record overturns an earlier fact. The board renders the
    #: older one as superseded: on run 18 four reverse workers each re-ran the
    #: same experiment because the board showed "the curve arithmetic is
    #: equivalent" and "the curve arithmetic is not equivalent" with no
    #: indication of which one came later and won.
    supersedes: int | None = None


@dataclass
class Flag:
    id: int
    run_id: int
    task_id: int | None
    value: str
    source: str
    method: str
    confidence: str
    status: str
    created_at: str = ""
    note: str = ""


@dataclass
class Case:
    """One solved-or-attempted challenge, distilled for the next run.

    Written from what the run already recorded — the tasks, the facts, the
    flag's derivation — so it costs no model turns. Flag literals are stripped
    before storage: the point is the route, not the answer.
    """

    id: int
    run_id: int
    challenge_id: int
    name: str
    category: str
    outcome: str                      # solved | failed
    route: list[str] = field(default_factory=list)
    derivation: str = ""
    dead_ends: list[str] = field(default_factory=list)
    #: Questions the run ended without answering. A retry of the same challenge
    #: starts from these instead of re-discovering them.
    open_unknowns: list[str] = field(default_factory=list)
    techniques: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    elapsed_s: int = 0
    used_count: int = 0
    #: Outcomes of the runs this card was injected into, not of the run that
    #: produced it. ``used_count`` alone cannot tell a card that helped from one
    #: that was merely present; without this the library can only grow louder.
    help_count: int = 0
    harm_count: int = 0
    created_at: str = ""
    last_used_at: str = ""


@dataclass
class Event:
    id: int
    run_id: int
    task_id: int | None
    kind: str
    payload: dict[str, Any]
    created_at: str

    def sse(self) -> str:
        body = json.dumps(self.payload, ensure_ascii=False)
        # A live-only event (id 0) carries no ``id:`` line on purpose: setting
        # one would move the browser's Last-Event-ID backwards and make the next
        # reconnect replay the whole backlog.
        head = f"id: {self.id}\n" if self.id > 0 else ""
        return f"{head}event: {self.kind}\ndata: {body}\n\n"


def loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
