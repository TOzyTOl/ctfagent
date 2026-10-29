"""The case library: what earlier runs learned, in a form the next run can use.

A finished run leaves three things behind — its tasks, its blackboard, and the
derivation of whatever flag it proposed. This module distils those into one
compact card per run and reads the cards back for the next challenge of the same
shape. It is deterministic: no model is called, so writing a case costs nothing
and cannot fail halfway through a run that has already ended.

Two rules keep the library from becoming noise:

* **The flag literal is never stored.** A card records *how* a value was
  derived, because a model shown an old flag will eventually repeat it. That is
  the one failure mode a memory of past answers can introduce.
* **A card must teach something.** A failed run with no dead ends and no
  observations produces nothing: "it did not work" is not a lesson.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .models import NEGATIVE_GAP_KEY, Case, FactKind, TaskStatus
from .textmatch import identifiers, tokens

if TYPE_CHECKING:  # pragma: no cover
    from .orchestrator import Run

#: Anything shaped like a flag. Stripped from every card before it is stored.
_FLAG_SHAPED = re.compile(r"[A-Za-z0-9_]{2,20}\{[^{}\s]{4,200}\}")
#: A task that produced nothing worth reading.
_UNINFORMATIVE = (TaskStatus.STOPPED, TaskStatus.FAILED, TaskStatus.QUEUED)

MAX_ROUTE = 8
MAX_DEAD_ENDS = 8
MAX_UNKNOWNS = 6
MAX_TECHNIQUES = 12
MAX_DERIVATION = 1200
#: Cap on the whole injected block. Cards are re-rendered into every dispatch,
#: so a large library would tax every turn of every run.
MAX_RENDER_CHARS = 4000

#: Words that say nothing about *which* challenge this is. They appear in every
#: description on a platform and in every card: ``http``, ``flag``, ``web``, the
#: attachment extensions. Measured on the live library, two cards from different
#: challenges shared ``http`` + the platform host and an unrelated reverse card
#: was pulled into a command-execution challenge's prompt on that alone.
_GENERIC = frozenset(
    """
    http https http2 www com cn net org io html htm php asp aspx jsp json xml yaml
    api flag ctf server client web app application index home login admin user
    account test data file files path dir shell cmd tool tools solve solved
    bin exe dll so zip tar gz png jpg jpeg txt pdf py ps1
    none null true false unknown page site url uri
    找出 找到 题目 要求 提示 需要 可以 使用 这个 那个 下面 上面 里面 然后 就是
    """.split()
)

#: A token that appears in at least this share of a library of at least
#: ``BOILERPLATE_MIN_CARDS`` cards is this library's boilerplate, not a signal:
#: on the live database the platform host was in 11 of 15 cards. Comparing on
#: raw tokens let that host alone satisfy the admission gate.
BOILERPLATE_SHARE = 0.7
BOILERPLATE_MIN_CARDS = 8
#: Shared informative tokens needed before a card counts as *used* by a run.
MIN_USED_SHARED = 2


def strip_flags(text: str) -> str:
    return _FLAG_SHAPED.sub("<flag>", text or "")


def _one_line(text: str, limit: int) -> str:
    text = " ".join(strip_flags(str(text or "")).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _route(run: "Run") -> list[str]:
    """Which directions were tried, in order, and how each ended.

    A task with no summary contributes only its role and status. The objective
    is deliberately left out: it is the lead's prose, often a paragraph, and it
    is the longest field available — a card built from eight objectives would
    cost a thousand tokens on every dispatch. The summary is what the worker
    concluded, and that is the reusable part.
    """
    out: list[str] = []
    for task in run.store.list_tasks(run.id):
        # ``verifier[exhausted]（没有留下总结）`` teaches nothing and is not a
        # direction anyone can choose; a verdict is.
        if task.status in _UNINFORMATIVE or task.role == "verifier":
            continue
        summary = str((task.result or {}).get("summary", ""))
        tail = _one_line(summary, 180) if summary.strip() else "（没有留下总结）"
        out.append(f"{task.role}[{task.status}] {tail}")
        if len(out) >= MAX_ROUTE:
            break
    return out


def _derivation(run: "Run") -> str:
    """The confirmed flag's derivation, or the best claim if none was confirmed.

    A low-confidence candidate is one the orchestrator lifted out of a report
    with a regex (``_scan_for_flags``); its ``method`` is a placeholder, not a
    derivation. It is used only when nothing better exists — observed on run 3,
    where the placeholder outranked a high-confidence candidate because a
    verifier had marked a near-miss ``verified``.
    """
    flags = run.store.list_flags(run.id)
    if not flags:
        return ""

    def rank(flag: Any) -> tuple[int, int]:
        status = {"confirmed": 0, "verified": 1, "candidate": 2}.get(flag.status, 9)
        return status, {"high": 0, "medium": 1, "low": 2}.get(flag.confidence, 3)

    fallback = ""
    for flag in sorted(flags, key=rank):
        method = strip_flags(flag.method or "").strip()
        if not method:
            continue
        if flag.confidence == "low":
            fallback = fallback or method
            continue
        return method[:MAX_DERIVATION]
    return fallback[:MAX_DERIVATION]


def _dead_ends(run: "Run") -> list[str]:
    facts = run.store.list_facts(run.id, limit=400)
    out: list[str] = []
    for f in facts:
        if f.kind != FactKind.DEAD_END or f.resolves:
            continue
        text = _one_line(f.content, 240)
        # A negative that never stated its search space or a positive control
        # travels with that warning attached. Without it the card reads as "this
        # route is closed" and the next run skips the one sweep that would have
        # found the key — the exact way the second solve of one challenge was
        # talked out of the answer the first solve had already proved.
        gap = str((f.evidence or {}).get(NEGATIVE_GAP_KEY) or "")
        if gap:
            text = f"【未验证的否定·勿作结论】{text}（{gap}）"
        out.append(text)
    # Later dead ends are usually the more specific ones (they came after the
    # obvious routes were ruled out).
    return out[-MAX_DEAD_ENDS:]


def _open_unknowns(run: "Run") -> list[str]:
    """Questions the run ended without answering.

    A run's unknowns are its most portable product: they are the exact points
    where it ran out of ideas, written by the worker that hit the wall. The
    easyrs run recorded "is the 48-byte constant really the comparison target"
    and "what does GetTickCount return at runtime" and closed with both still
    open — a retry of that binary should begin there, not rediscover them.
    """
    try:
        facts = run.open_unknowns()
    except Exception:  # noqa: BLE001 - a card must never fail a close
        return []
    return [_one_line(f.content, 200) for f in facts][:MAX_UNKNOWNS]


def _techniques(route: list[str], derivation: str) -> list[str]:
    """Tool and symbol names the run actually used — the card's search key.

    Taken from the route and the derivation rather than from a model-written
    summary: those are the words a later challenge of the same shape will also
    contain (``gdb``, ``memcmp``, ``pyinstaller``, ``strace``).
    """
    text = " ".join(route) + " " + derivation
    return identifiers(text)[:MAX_TECHNIQUES]


def distill(run: "Run") -> Case | None:
    """Build the card for a finished run, or None when it teaches nothing."""
    route = _route(run)
    derivation = _derivation(run)
    dead_ends = _dead_ends(run)
    open_unknowns = _open_unknowns(run)
    if not route and not dead_ends:
        return None
    confirmed = any(f.status == "confirmed" for f in run.store.list_flags(run.id))
    if not confirmed and not dead_ends and not open_unknowns:
        # A run that ended without an answer, without recording what it ruled
        # out, and without saying what it still needed leaves nothing a later
        # run can use.
        return None
    elapsed = 0
    run_row = run.store.get_run(run.id)
    if run_row is not None and run_row.started_at and run_row.finished_at:
        from datetime import datetime  # noqa: PLC0415 - only needed on this path

        try:
            start = datetime.strptime(run_row.started_at, "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(run_row.finished_at, "%Y-%m-%d %H:%M:%S")
            elapsed = max(0, int((end - start).total_seconds()))
        except ValueError:
            elapsed = 0
    return Case(
        id=0,
        run_id=run.id,
        challenge_id=run.challenge.id,
        name=run.challenge.name,
        category=run.challenge.category or "unknown",
        outcome="solved" if confirmed else "failed",
        route=route,
        derivation=derivation,
        dead_ends=dead_ends,
        open_unknowns=open_unknowns,
        techniques=_techniques(route, derivation),
        cost_usd=round(run.cost_usd, 2),
        elapsed_s=elapsed,
    )


def save(run: "Run") -> Case | None:
    """Distil and persist the run's card. Safe to call on every close."""
    case = distill(run)
    if case is None:
        return None
    case.id = run.store.upsert_case(case)
    return case


def _overlap(a: set[str], b: set[str]) -> float:
    """Share of ``a``'s tokens that ``b`` also has. Empty ``a`` matches nothing."""
    if not a:
        return 0.0
    return len(a & b) / len(a)


def informative(text: str, shared: frozenset[str] = frozenset()) -> set[str]:
    """Tokens that can tell two challenges apart.

    Generic words (see ``_GENERIC``) and this library's own boilerplate are
    dropped: a token every card carries is not evidence that *this* card is
    about *this* challenge.
    """
    return {t for t in tokens(text) if t not in _GENERIC and t not in shared}


def boilerplate_tokens(cases: list[Case]) -> frozenset[str]:
    """Tokens that appear in most cards — a platform host, a shared URL stem."""
    if len(cases) < BOILERPLATE_MIN_CARDS:
        return frozenset()
    counts: dict[str, int] = {}
    for case in cases:
        for token in tokens(case.name + " " + " ".join(case.route) + " " + case.derivation):
            counts[token] = counts.get(token, 0) + 1
    floor = BOILERPLATE_SHARE * len(cases)
    return frozenset(t for t, n in counts.items() if n >= floor)


def _card_text(case: Case) -> str:
    return " ".join([case.name, *case.route, case.derivation, *case.dead_ends,
                     *case.open_unknowns, *case.techniques])


def relevant(
    store: Any,
    challenge: Any,
    limit: int = 3,
) -> list[Case]:
    """Cards worth putting in front of this challenge.

    Scored on the challenge's own words (name, description, hints, remote, file
    names) and on the technique names the card recorded. A same-category card is
    admitted even with no vocabulary overlap: the category is the strongest
    signal there is, and a challenge that shares none of a card's words is
    usually still the same kind of work.

    Lexical overlap is computed on *informative* tokens only. Two properties of
    the live library made the old raw comparison wrong: a token in every card
    (the platform host) and generic words (``http``, ``flag``) satisfied the
    "real vocabulary overlap" gate between a command-execution challenge and a
    reversing card. Resemblance has to come from words that describe the work.
    """
    cases = store.list_cases(limit=400)
    if not cases:
        return []
    boilerplate = boilerplate_tokens(cases)
    text = " ".join(
        [
            str(getattr(challenge, "name", "") or ""),
            str(getattr(challenge, "description", "") or ""),
            " ".join(str(h) for h in (getattr(challenge, "hints", None) or [])),
            str(getattr(challenge, "remote", "") or ""),
            " ".join(str(f) for f in (getattr(challenge, "files", None) or [])),
        ]
    )
    wanted = informative(text, boilerplate)
    category = str(getattr(challenge, "category", "") or "unknown")

    scored: list[tuple[float, Case]] = []
    challenge_id = getattr(challenge, "id", 0)
    for case in cases:
        score = 0.0
        same_challenge = bool(challenge_id) and case.challenge_id == challenge_id
        if same_challenge:
            # A retry of the same challenge is the single most useful card there
            # is: it records what this exact artifact already gave up.
            score += 8.0
        same_category = category != "unknown" and case.category == category
        if same_category:
            score += 3.0
        elif case.category and case.category not in ("unknown", category):
            score -= 2.0
        card_tokens = informative(_card_text(case), boilerplate)
        shared = len(wanted & card_tokens)
        overlap = _overlap(wanted, card_tokens)
        score += 4.0 * overlap
        # Whole-token comparison, not a substring test: ``pie`` is a substring of
        # ``pier-beacon`` and ``http`` of every URL, and either would otherwise
        # pull an unrelated card into the block.
        score += sum(1.0 for t in case.techniques if t in wanted and t not in boilerplate)
        if score <= 0:
            continue
        if not (same_challenge or same_category) and (shared < 2 or overlap < 0.15):
            # One shared word is a coincidence, not a resemblance. Only the same
            # challenge, the category, or a real vocabulary overlap can admit a
            # card that is not otherwise an obvious match.
            continue
        if case.outcome == "solved":
            score += 1.0  # a tiebreaker, never the reason a card is admitted
        # A card that was present on runs that lost more often than on runs that
        # won is losing its place. The shift is deliberately gentle: one failed
        # run is noise, and a category can be hard for reasons that have nothing
        # to do with the card. Cards that never proved anything keep their score.
        #
        # ``help_count``/``harm_count`` count runs on which the card was *used*
        # (see ``used_case_ids``), not runs that merely had it in a prompt. When
        # they counted exposure, a card shown on five winning runs read as five
        # helps regardless of whether anything in it was ever applied — the
        # library got louder without getting better.
        decided = case.help_count + case.harm_count
        if decided >= 2:
            score += 1.5 * ((case.help_count / decided) - 0.5)
        scored.append((score, case))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    # The prompt block is rebuilt on every dispatch, so its size is a recurring
    # cost, not a one-off. Three cards of a few hundred tokens each is the
    # budget; a fourth displaces blackboard facts that are about *this* run.
    picked: list[Case] = []
    remaining = MAX_RENDER_CHARS
    for _, case in scored:
        if len(picked) >= limit:
            break
        size = len(render([case]))
        if size > remaining:
            continue
        remaining -= size
        picked.append(case)
    return picked


def render(cases: list[Case]) -> str:
    """The prompt block. Empty when there is nothing to say."""
    if not cases:
        return ""
    lines = [
        "# 战例库（本机以往做过的同类题）",
        "这些是之前的运行留下的记录，不是当前题目的证据。用它们来选路线、避开已知死路；"
        "里面的任何具体值（地址、密钥、字符串）都属于那道题，不要直接搬过来当结论。",
        "",
    ]
    for case in cases:
        head = f"## {case.name}（{case.category}，{case.outcome}，${case.cost_usd:.2f}"
        if case.elapsed_s:
            # ``elapsed_s`` is a float; ``//`` on it prints "10.0 分钟" in the
            # worker's prompt. Same class of leak as the benchmark table.
            head += f"，{int(case.elapsed_s) // 60} 分钟"
        lines.append(head + "）")
        if case.techniques:
            lines.append(f"技法：{', '.join(case.techniques[:8])}")
        if case.route:
            lines.append("路线：")
            lines.extend(f"- {item}" for item in case.route)
        if case.derivation:
            lines.append(f"关键推导：{_one_line(case.derivation, 600)}")
        if case.dead_ends:
            lines.append("这次运行试过但没有结果的方向（先看清楚它有没有做过阳性对照，"
                         "没做过的不等于目标上不存在）：")
            lines.extend(f"- {_one_line(item, 180)}" for item in case.dead_ends)
        if case.open_unknowns:
            lines.append("这次运行结束时仍未解的问题：")
            lines.extend(f"- {_one_line(item, 180)}" for item in case.open_unknowns)
        lines.append("")
    return "\n".join(lines).strip()


def run_written_text(store: Any, run_id: int) -> str:
    """Everything a run wrote down, as one string: facts, conclusions, derivation.

    The corpus the usage check compares card/note vocabulary against. Kept in
    one place so the card and the note credit the same way; two implementations
    would drift and one memory would start crediting exposure again.
    """
    return " ".join(
        [f.content for f in store.list_facts(run_id, limit=400)]
        + [
            str((t.result or {}).get("summary", ""))
            + " "
            + " ".join((t.result or {}).get("findings", []) or [])
            for t in store.list_tasks(run_id)
        ]
        + [f.method for f in store.list_flags(run_id)]
    )


def used_case_ids(store: Any, run_id: int) -> list[int]:
    """Cards this run's own records show were applied, not merely shown.

    Exposure is not use. Measured on the live database, ``help_count`` read 5/0
    for a failed card because it had been injected into five runs that happened
    to win afterwards — the retrieval score then rewarded it forever. A card is
    *used* when at least ``MIN_USED_SHARED`` of its distinctive tokens (its
    techniques, or words from the route it recommends) appear in something this
    run wrote: the blackboard, its task conclusions, or the confirmed flag's
    derivation. Those are the places a worker names the tool or step it took,
    and they are written after the card was in front of it.
    """
    cards = [store.get_case(cid) for cid in store.get_run_cases(run_id)]
    cards = [c for c in cards if c is not None]
    if not cards:
        return []
    bl = boilerplate_tokens(store.list_cases(limit=400))
    written = informative(run_written_text(store, run_id), bl)
    used: list[int] = []
    for case in cards:
        signature = {
            t for t in (tok.lower() for tok in case.techniques) if t not in _GENERIC and t not in bl
        }
        if len(signature & written) >= MIN_USED_SHARED:
            used.append(case.id)
    return used


def attribute(store: Any, run_id: int, solved: bool) -> int:
    """Charge this run's outcome to the cards it actually used.

    Called once per run, after the close has decided whether a flag was
    confirmed. Returns how many cards were credited. It is separate from
    ``save`` on purpose: a reopened run is re-distilled, but its outcome is
    charged once per *run*; ``claim_case_credit`` is what makes that true.

    Only a run that reached a verdict is charged (solved or failed). A run the
    operator stopped says nothing about the cards it was shown, and charging it
    would burn the one attribution the run gets.

    Cards that were shown but never applied are not charged at all: crediting
    every loaded card was the mechanism by which a card that misled a run could
    still gain help points from the run's eventual success, and by which a
    winner on the board stopped meaning "this helped" (see ``used_case_ids``).
    """
    if not store.claim_case_credit(run_id):
        return 0
    credited = 0
    for case_id in used_case_ids(store, run_id):
        if case_id <= 0 or store.get_case(case_id) is None:
            continue
        store.credit_case(case_id, solved)
        credited += 1
    return credited
