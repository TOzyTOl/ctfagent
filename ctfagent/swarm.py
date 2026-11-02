"""黑板市场：受控蜂群的确定性核心。

现有体系是星型——主 agent 既探索攻击面又决定派谁、派什么。蜂群化不是把
角色变多，而是把「谁决定下一步做什么」从中心移到板上：主 agent 只写假设，
worker 按自己的方向认领。这个模块管这条路径上所有不该由模型判断的部分：

- 假设的规范化与去重（两条换种说法的假设不能变成两份工单）
- 认领排序（谁最适合、哪条假设没人做）
- 租约（worker 挂了，它认领的假设必须能被回收，而不是永久锁死）
- 预算预约（无中心就没有人替全群体踩刹车，预约是唯一替代）
- 收敛（板上没活了，是收敛，不是"再造一批活"）

刻意不做的事：

- **不从文本里猜假设家族。** ``family`` 由提出者显式给出，因为它会被用来
  分派方向，关键词匹配在这里是随机分派。
- **不碰 ``orchestrator`` / ``Run``。** 本模块只吃纯数据，保持叶子。
- **不给结论投票。** 谁对谁错沿黑板既有的 ``supersedes`` / ``resolves``
  链与 verifier 判定，不引入新的共识机制——多数派不等于正确。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: A hypothesis that cannot be falsified is not work, it is a hope. Requiring a
#: falsification criterion at posting time is what stops the board filling with
#: "maybe there is a vulnerability" entries that no worker can ever finish.
NEEDS_CRITERION = "假设必须带证伪判据"
NEEDS_EVIDENCE = "假设必须带触发它的观测"
DUPLICATE = "同族同表述的假设已在板上"

#: Hypothesis lifecycle. ``claimed`` is leased, not owned: a worker that dies
#: mid-task must not hold its hypothesis forever.
OPEN = "open"
CLAIMED = "claimed"
SUPPORTED = "supported"
REFUTED = "refuted"
ABANDONED = "abandoned"
TERMINAL = frozenset({SUPPORTED, REFUTED, ABANDONED})

#: Seconds a claim survives without a heartbeat. Chosen against the measured
#: stall timeout (900 s): shorter than that, so a wedged session loses its claim
#: before the stall clock fires, and a healthy one renews on every fact it posts.
DEFAULT_TTL_S = 300.0

#: Share of the run budget kept free for closing roles. A verifier that cannot
#: start because the board spent everything reproduces nothing, and the run dies
#: holding an unverifiable flag.
CLOSING_RESERVE_FRACTION = 0.15


#: The families a hypothesis may name. Deliberately a literal, not a scan of
#: ``ROLE_SKILL``: ``claim`` matches (worker role -> family), so a family that is
#: not a role can never be picked up — and on the board that failure looks
#: exactly like a hypothesis nobody wants. ``tests/test_swarm.py`` asserts this
#: set equals the solver roles, so the two cannot drift apart.
FAMILIES = (
    "web",
    "crypto",
    "pwn",
    "reverse",
    "misc",
    "forensics",
    "malware",
    "ai_ml",
    "osint",
    "triage",
)

#: Roles that hold a direction. A worker in one direction does not spend the
#: run's money on another direction's hypothesis; ``triage`` is the generalist
#: and may take anything, because at the start nobody knows which direction the
#: challenge is in.
DIRECTION_ROLES = frozenset(FAMILIES) - {"triage"}


def normalize(statement: str) -> str:
    """Collapse the superficial differences that make one idea look like two.

    Whitespace is dropped entirely, not squeezed: Chinese statements carry no
    word boundaries, so "登录页 存在注入" and "登录页存在注入" must land on the
    same key or the board gets a duplicate hypothesis per phrasing.
    """
    text = (statement or "").strip().lower()
    text = re.sub(r"[^\w一-鿿]+", "", text, flags=re.UNICODE)
    return text


@dataclass
class Hypothesis:
    """One falsifiable direction. The unit of work on the board."""

    family: str
    statement: str
    criterion: str = ""      # 什么观测出现就说明这条假设错了
    evidence: str = ""       # 是哪条观测让你想到它的
    origin: str = "lead"
    basis: str = "inferred"
    priority: int = 0
    status: str = OPEN
    claimed_by: int | None = None  # task id
    lease_until: float = 0.0
    supports: int = 0        # 被观察证实的次数
    refutes: int = 0         # 被观察证伪的次数
    #: Storage identity. Zero means "not persisted yet" — the pure functions
    #: above never read these, so the same dataclass serves the board and the
    #: table without a second model to keep in sync.
    id: int = 0
    run_id: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.family, normalize(self.statement))

    def viable(self) -> bool:
        return self.status not in TERMINAL


def post(h: Hypothesis) -> None:
    """Raise unless the hypothesis is postable. Cheap guard, no model call."""
    if not (h.criterion or "").strip():
        raise ValueError(NEEDS_CRITERION)
    if not (h.evidence or "").strip():
        raise ValueError(NEEDS_EVIDENCE)
    if not (h.family or "").strip():
        raise ValueError("假设必须给出 family（由提出者显式指定，不靠文本推断）")
    # A family outside the vocabulary is the one validation that has to happen
    # here rather than at claim time: nothing would ever pick it up, and the
    # board does not distinguish "wrong name" from "nobody was interested".
    if h.family.strip().lower() not in FAMILIES:
        raise ValueError(
            f"未知 family {h.family!r}；只能取 {'/'.join(FAMILIES)}"
            "（family 决定谁能认领，写错的方向会永远躺在板上）"
        )


def dedupe(items: list[Hypothesis]) -> tuple[list[Hypothesis], list[Hypothesis]]:
    """Merge same-key hypotheses; return (board, rejected-as-duplicates).

    The stronger copy survives: more supporting observations first, then higher
    priority, then the one with a real evidence trail. Merging keeps the refute
    count too, so a direction that already failed twice cannot be reposted as if
    it were fresh.
    """
    board: dict[tuple[str, str], Hypothesis] = {}
    dropped: list[Hypothesis] = []
    for h in items:
        prev = board.get(h.key)
        if prev is None:
            board[h.key] = h
            continue
        prev.supports = max(prev.supports, h.supports)
        prev.refutes = max(prev.refutes, h.refutes)
        if len(h.evidence) > len(prev.evidence):
            # Keep the more specific trail. A real command or observation is
            # longer than "凭感觉", and it is what the next worker re-runs.
            prev.evidence = h.evidence
        if h.priority > prev.priority:
            prev.priority = h.priority
        dropped.append(h)
    return list(board.values()), dropped


def is_expired(h: Hypothesis, now: float) -> bool:
    return h.status == CLAIMED and h.lease_until > 0 and now > h.lease_until


def reclaim(board: list[Hypothesis], now: float) -> list[Hypothesis]:
    """Return hypotheses whose lease lapsed, resetting them to ``open``."""
    freed: list[Hypothesis] = []
    for h in board:
        if is_expired(h, now):
            h.status = OPEN
            h.claimed_by = None
            h.lease_until = 0.0
            freed.append(h)
    return freed


def renew(h: Hypothesis, now: float, ttl_s: float = DEFAULT_TTL_S) -> None:
    """Heartbeat. A worker that posts a fact proves it is alive."""
    if h.status == CLAIMED:
        h.lease_until = now + ttl_s


def claim(
    board: list[Hypothesis],
    *,
    role: str,
    task_id: int,
    now: float,
    families: dict[str, str] | None = None,
    ttl_s: float = DEFAULT_TTL_S,
) -> Hypothesis | None:
    """Pick the best hypothesis for this worker and lease it.

    Scoring, in order: not currently claimed by anyone, family matches this
    worker's direction, has not already been refuted, higher priority. Ties go to
    the longest-waiting open hypothesis, which is what keeps one direction from
    starving the rest of the board.

    A direction worker only ever takes its own family. The old version merely
    *preferred* a match, so a web worker could pick up a pwn hypothesis once the
    web ones ran out — and then spend the run on a direction it has neither the
    skill for nor the tools for (no gdb in a web session). ``triage`` is the
    exception and stays general: at the start nobody knows where the challenge
    is, and that is the role whose job is to find out.
    """
    families = families or {}
    mine = families.get(role)
    best: Hypothesis | None = None
    best_score = -10**9
    for h in board:
        if not h.viable() or h.status != OPEN:
            continue
        if mine and h.family != mine:
            continue
        score = h.priority
        if mine and mine == h.family:
            score += 5
        score -= 4 * h.refutes
        score += min(h.supports, 2)
        if score > best_score:
            best, best_score = h, score
    if best is None:
        return None
    best.status = CLAIMED
    best.claimed_by = task_id
    best.lease_until = now + ttl_s
    return best


@dataclass
class Budget:
    """Reservation ledger for the sessions the board starts.

    The run pool's own ledger (``Run._committed``) keeps sessions from reading
    the same remainder; this one answers a different question — how much of the
    budget is still *allowed to an exploring worker*, as opposed to reserved
    for the closing roles. ``Run._reserve_ceiling`` folds both in by taking the
    smaller remainder.
    """

    ceiling: float
    reserve_fraction: float = CLOSING_RESERVE_FRACTION
    reserved: float = 0.0
    spent: float = 0.0
    holds: dict[int, float] = field(default_factory=dict)

    def free(self) -> float:
        usable = self.ceiling * (1.0 - self.reserve_fraction)
        return max(0.0, usable - self.reserved - self.spent)

    def reserve(self, task_id: int, amount: float, *, closing: bool = False) -> bool:
        """Take a reservation. Closing roles draw on the reserve, not the pool."""
        if amount <= 0:
            return True
        if closing:
            if self.spent + self.reserved + amount > self.ceiling:
                return False
        elif amount > self.free():
            return False
        self.holds[task_id] = self.holds.get(task_id, 0.0) + amount
        self.reserved += amount
        return True

    def settle(self, task_id: int, actual_cost: float) -> float:
        """Release a hold and book the real cost. Returns the unspent residue."""
        held = self.holds.pop(task_id, 0.0)
        self.reserved = max(0.0, self.reserved - held)
        cost = max(0.0, actual_cost)
        self.spent += cost
        return max(0.0, held - cost)


def describe(h: Hypothesis) -> str:
    """Render a hypothesis for the worker's prompt.

    The falsification criterion goes in the same block as the claim, not in a
    separate instruction: a worker that reads "prove this" without "this is how
    it would be wrong" reports agreement instead of evidence.
    """
    lines = [
        "## 你认领的假设",
        f"方向：{h.family}",
        f"假设：{h.statement}",
        f"证伪判据：{h.criterion}",
        f"触发观测：{h.evidence}",
    ]
    if h.refutes:
        lines.append(f"注意：这条方向此前已被证伪 {h.refutes} 次，重走要有新证据。")
    lines += [
        "",
        "结论要么给一条 basis=observed 的事实支撑它，要么给出带范围与对照的否定——"
        "「没找到」不是「不存在」。",
    ]
    return "\n".join(lines)


def convergence(
    board: list[Hypothesis],
    *,
    has_candidate_flag: bool = False,
    running: bool = False,
) -> str:
    """What the board means right now.

    ``idle`` matters: an empty board with nothing running is a *result*, not a
    prompt to invent more hypotheses. A swarm that fills its own queue can never
    finish.

    Read by the orchestrator's board coordinator, not by a model: whether the
    board is *working*, *waiting for a claim* or *finished* decides whether a
    worker is started at all, and that decision has to be free.

    ``running`` covers workers with no hypothesis behind them (the lead still
    dispatches in this mode, and a closing role may be alive). Work in a
    direction nobody wrote down is still work: the coordinator must not open a
    second front over it just because the board looks empty.
    """
    if running or any(h.status == CLAIMED for h in board):
        return "working"
    if any(h.status == OPEN for h in board):
        return "postable"
    if has_candidate_flag:
        return "needs_input"
    return "idle"


#: How a finished task's outcome maps onto the hypothesis it was holding.
#: ``refuted`` is the one that has to be earned: a worker that ran out of time
#: did not disprove anything, it just did not finish. Letting "the session
#: killed itself" count as a refutation is how a board fills with directions
#: that look disproven when nobody tried them.
SETTLE_DONE = "done"
SETTLE_REFUTED = "refuted"
SETTLE_ABANDONED = "abandoned"

#: Statuses that mean "the worker stopped, not the direction": ceilings,
#: operator stops and crashes say nothing about the hypothesis itself, so the
#: entry goes back on the board for a cheaper claim instead of being settled.
UNFINISHED_STATUSES = frozenset({"exhausted", "stopped", "failed", "queued", "running"})


def settle_for(task_status: str, *, evidence: bool = True) -> str:
    """Which outcome a task's terminal status implies for its own hypothesis.

    Used only as the *default*: what a worker actually wrote on the blackboard
    outranks its exit status (see ``Run._settle_hypothesis``).

    ``evidence`` is whether the task left anything behind — a fact, a candidate
    flag, a resolved unknown. ``task_complete`` only accepts done/blocked, so a
    worker that gave up closes ``done`` with an empty result; that is a stop,
    not a conclusion, and retiring the direction would quietly bury a route
    nobody disproved.
    """
    if str(task_status).lower() in UNFINISHED_STATUSES:
        return SETTLE_ABANDONED
    if not evidence:
        return SETTLE_ABANDONED
    return SETTLE_DONE


def pending_claims(board: list[Hypothesis], live_task_ids: set[int]) -> list[Hypothesis]:
    """Claims whose owning task is gone.

    A lease is a timer, and a timer is the wrong instrument for this: a worker
    that crashed five seconds after claiming would hold its direction for the
    full TTL, while the board reads *working* and no coordinator starts anyone.
    The task set is the fact; the lease is only the backstop for a task that is
    still marked live but wedged. Releasing on the task set, not on the clock, is
    what makes the hand-off immediate.
    """
    return [
        h
        for h in board
        if h.status == CLAIMED and (h.claimed_by is None or h.claimed_by not in live_task_ids)
    ]


def settled_by(fact_kind: str, resolves: bool = False) -> str:
    """What a recorded fact does to the hypothesis of the task that wrote it.

    ``dead_end`` is the only kind that closes a direction negatively, and even
    then only when it carries the scope+control evidence the blackboard already
    demands (``models.negative_gap``) — a worker cannot refute a direction by
    declaring it refuted, or the board becomes a record of first impressions.
    """
    kind = str(fact_kind or "").lower()
    if kind == "dead_end":
        return SETTLE_REFUTED
    if resolves:
        return SETTLE_DONE
    return ""
