"""Run-level patrol: decide when a run has stopped making progress.

The task-level guard already exists (``no_progress_tools`` counts tool calls
since the last fact, ``repeat_call_limit`` catches identical calls). Both die
with the task. The failure this module targets is one level up: four workers
finish "successfully", each reporting a conclusion, and the run's knowledge
does not move — nothing new on the blackboard, no new evidence, the same
experiment run twice under different names.

Everything here is deterministic and costs nothing: it reads numbers the
orchestrator already has and answers one question — *is a patrol worth a
session?* The model is only called after the answer is yes, once, and with a
90-second leash.

Design constraints:

- No import of ``orchestrator`` or ``Run``. This module must stay a leaf so it
  cannot feed the god object's growth.
- No hidden state: every rule takes its inputs explicitly, so the thresholds
  are testable and tunable without a running system.
- The patrol may only *stop* or *dispatch*, never rewrite an objective, write a
  fact, or touch a flag. Least authority is what keeps a checker from becoming
  a second author.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import swarm

#: A task that adds no fact and surfaces no new tool output moved nothing.
#: Weights favour facts: one recorded conclusion outweighs two exploratory
#: commands whose output the board already had.
FACT_WEIGHT = 2
OUTPUT_WEIGHT = 1

#: How many consecutive finished tasks may score zero before we look.
STALL_WINDOW = 3

#: A task running this many times longer than its category's p90 is "slow",
#: not "hard". 2.0 keeps normal variance out of the trigger.
SLOW_RATIO = 2.0

#: A patrol is a session like any other. Cap per run so the checker cannot
#: become the workload; the orchestrator also holds at most one at a time.
MAX_PATROLS = 3

#: Roles whose job is to close a run, not to advance it. They write no facts by
#: design, so counting them as "no progress" reads a healthy run as a stalled
#: one — replaying run 95 (solved) against the database flagged 27 quiet tasks
#: that were almost entirely verifier and writer turns.
IGNORED_ROLES = frozenset({"verifier", "writer"})

#: Reason codes, stable strings: they land in the events table and get counted.
STALLED = "stalled_tasks"
SLOW = "slow_task"
REPEAT_DIRECTION = "repeat_direction"
UNWORKED = "unworked_hypothesis"

#: How long an open board entry may sit untouched before it is worth a look.
#:
#: The coordinator starts an open entry within one tick (2 s) unless the family
#: is busy, the budget is spent, or the entry keeps bouncing back from refused
#: dispatches. So an entry that is *still* open after ten minutes is not
#: "waiting its turn" — either the board cannot afford it (``board.refused``),
#: the direction is wrong for the challenge, or nobody can claim it (a family a
#: live lead worker already occupies). Those are exactly the three questions a
#: patrol can answer without a shell. Ten minutes is far longer than any real
#: claim latency measured so far (median 1.0 s) and short enough to matter
#: inside a 30–80 minute window.
UNWORKED_AFTER_S = 600.0

#: Prefix that marks a dispatch as a patrol. ``_worker_spec`` reads it to hand
#: the session a read-only tool set: a patrol reads the board and answers in one
#: turn, so giving it a shell would buy nothing and widen what it can break.
PATROL_MARK = "[巡检]"

#: The only tools a patrol gets. No shell, no network, no file writes — the
#: answer is a judgement about text the orchestrator already collected.
PATROL_TOOLS = (
    "Read",
    "Grep",
    "Glob",
    "mcp__worker__report_progress",
    "mcp__worker__task_complete",
)

#: One round trip is the whole job. A patrol that needs five turns is a worker.
PATROL_MAX_TURNS = 3

#: Ceiling in **CLI dollars** (the unit ``SessionSpec.max_budget_usd`` takes).
#: On flash one CLI dollar is worth about 20 cents of gateway money, so this is
#: roughly $0.10 — the price of one cheap turn, which is what it should cost.
PATROL_CEILING_CLI_USD = 0.5


def mark(text: str) -> str:
    """Prefix a patrol objective so the spec builder can recognize it."""
    return f"{PATROL_MARK}\n{text}"


def is_patrol(objective: str) -> bool:
    return (objective or "").lstrip().startswith(PATROL_MARK)


@dataclass(frozen=True)
class TaskTrace:
    """What one finished task contributed, as counts rather than prose."""

    task_id: int
    role: str
    status: str
    objective: str = ""
    facts_added: int = 0
    new_outputs: int = 0
    seconds: float = 0.0
    turns: int = 0
    repeated_calls: int = 0

    @property
    def score(self) -> int:
        return self.facts_added * FACT_WEIGHT + self.new_outputs * OUTPUT_WEIGHT

    @property
    def moved(self) -> bool:
        return self.score > 0


@dataclass(frozen=True)
class Trigger:
    reason: str
    detail: str
    task_ids: tuple[int, ...] = ()
    severity: str = "medium"


@dataclass
class PatrolState:
    """Per-run patrol bookkeeping. The orchestrator owns one of these."""

    dispatched: int = 0
    last_reason: str = ""
    covered: set[int] = field(default_factory=set)
    #: Board entries already looked at, by hypothesis id. Kept separate from
    #: ``covered`` on purpose: a hypothesis id and a task id are different
    #: namespaces, and one set would silently mark an unrelated task as covered.
    covered_hypotheses: set[int] = field(default_factory=set)

    def spent(self) -> bool:
        return self.dispatched >= MAX_PATROLS


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Empty input returns 0 so callers need no guard."""
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def category_baseline(history: list[float], fallback: float) -> float:
    """p90 of past durations for one category, or ``fallback`` when unknown.

    p90 rather than the mean: a run's own history is short, and one 40-minute
    outlier would otherwise raise the bar for every later task in the category.
    """
    if len(history) < 3:
        return fallback
    return percentile(history, 90) or fallback


def stalled_window(
    traces: list[TaskTrace],
    window: int = STALL_WINDOW,
    ignore_roles: frozenset[str] = IGNORED_ROLES,
) -> list[TaskTrace]:
    """The trailing run of zero-score tasks, if it reaches ``window``.

    Closing roles are dropped before the tail is measured: a verifier writing no
    fact is doing its job, and letting it count would make every healthy run end
    in a false stall.
    """
    tail: list[TaskTrace] = []
    for trace in reversed([t for t in traces if t.role not in ignore_roles]):
        if trace.moved:
            break
        tail.append(trace)
    tail.reverse()
    return tail if len(tail) >= window else []


def evaluate(
    *,
    traces: list[TaskTrace],
    live: list[tuple[int, str, float]] = (),  # (task_id, role, seconds so far)
    baseline_s: float = 0.0,
    has_candidate_flag: bool = False,
    state: PatrolState | None = None,
    window: int = STALL_WINDOW,
    slow_ratio: float = SLOW_RATIO,
    ignore_roles: frozenset[str] = IGNORED_ROLES,
    unworked: tuple[tuple[int, str, float, str], ...] = (),
    unworked_after_s: float = UNWORKED_AFTER_S,
) -> Trigger | None:
    """Return the strongest reason to patrol, or None.

    Order matters: a stalled run is worth more than a slow task, and a repeat
    direction is the cheapest thing to say, so it comes last.

    ``unworked`` is ``(hypothesis_id, family, age_seconds, statement)`` for
    entries that are still ``open`` on the board. It ranks above a slow task:
    a slow worker is at least working, while an open entry nobody can start is
    work the run has already paid the post for and is not doing. It ranks below
    a stalled tail, which is measured work that produced nothing.
    """
    state = state or PatrolState()
    if state.spent():
        return None

    # A candidate flag means the run is converging; extra eyes add nothing and
    # the operator is already being asked to confirm.
    if has_candidate_flag:
        return None

    tail = stalled_window(traces, window, ignore_roles)
    fresh = [t for t in tail if t.task_id not in state.covered]
    if fresh:
        roles = sorted({t.role for t in fresh})
        return Trigger(
            reason=STALLED,
            detail=(
                f"最近 {len(tail)} 个工单没有产出任何新结论或新证据"
                f"（{'、'.join(roles)}），共 {sum(t.turns for t in tail)} 轮、"
                f"{sum(t.seconds for t in tail) / 60:.0f} 分钟"
            ),
            task_ids=tuple(t.task_id for t in fresh),
            severity="high",
        )

    stale = [
        (hid, family, age, statement)
        for hid, family, age, statement in unworked
        if age >= unworked_after_s and hid not in state.covered_hypotheses
    ]
    if stale:
        # The one that has been waiting longest. An entry that just crossed the
        # line may still be in the queue behind a live family; the oldest one
        # that *still* did not start is the one the board cannot pick up.
        hid, family, age, statement = max(stale, key=lambda row: row[2])
        return Trigger(
            reason=UNWORKED,
            detail=(
                f"假设 #{hid}（{family}）在板上挂了 {age / 60:.0f} 分钟没被认领："
                f"{statement[:80]}"
            ),
            task_ids=(hid,),
            severity="medium",
        )

    if baseline_s > 0:
        limit = baseline_s * slow_ratio
        for task_id, role, seconds in live:
            if task_id in state.covered or seconds < limit:
                continue
            return Trigger(
                reason=SLOW,
                detail=(
                    f"{role} 工单 #{task_id} 已跑 {seconds / 60:.0f} 分钟，"
                    f"是该分类 p90（{baseline_s / 60:.0f} 分钟）的 "
                    f"{seconds / baseline_s:.1f} 倍"
                ),
                task_ids=(task_id,),
                severity="medium",
            )

    for trace in traces:
        if trace.moved or trace.status != "failed" or trace.task_id in state.covered:
            continue
        again = [
            t
            for t in traces
            if t.role == trace.role and t.task_id > trace.task_id and not t.moved
        ]
        if again:
            return Trigger(
                reason=REPEAT_DIRECTION,
                detail=(
                    f"{trace.role} 方向在 #{trace.task_id} 无结论后又被派了一次"
                    f"（#{again[0].task_id}），两次都没有新证据"
                ),
                task_ids=(trace.task_id, again[0].task_id),
                severity="medium",
            )

    return None


PATROL_OBJECTIVE = """体检这个 run 的状态，给一个结论。

你**只能**从下面四个结论里选一个，不许给建议清单，不许展开推理过程：

- `CONTINUE` —— 方向没错，只是还没到时候。给一句为什么。
- `REPIVOT` —— 当前方向已经走死，应该换一个假设。给一句新假设。
- `STOP <工单号>` —— 某个工单在空转，应该停掉。给一句依据。
- `CLOSE <假设号>` —— 板上某条假设不值得再等（例：该方向的活已经有人在同一个 family 里做、\
这条方向本身不可证伪、或者题面已经把它排除）。给一句依据。**不要**用它关闭一条只是还没轮到\
的假设：板子每 2 秒认领一次，挂了很久还没被认领，通常意味着预算或 family 冲突，那是 CONTINUE 的理由。

判断依据只有三条，按优先级：
1. 黑板上的「未解问题」是不是同一个问题被反复提出、没人推进；
2. 已记录的事实里，有没有互相矛盾的（谁推翻了谁，后来者有没有用上）；
3. 最近几个工单的结论，是不是同一个观察换了说法。

不要读代码、不要跑命令、不要提新实验方案——那是 worker 的事。
"""


def build_prompt(trigger: Trigger, digest: str, recent: list[TaskTrace]) -> str:
    """Assemble the patrol turn: reason, blackboard, and the last few verdicts."""
    lines = [
        f"触发原因：{trigger.reason} —— {trigger.detail}",
        "",
        "## 黑板",
        digest.strip() or "（空）",
        "",
        "## 最近的工单",
    ]
    for trace in recent[-6:]:
        lines.append(
            f"- #{trace.task_id} {trace.role} [{trace.status}] "
            f"新结论 {trace.facts_added} · 新证据 {trace.new_outputs} · "
            f"{trace.turns} 轮 · {trace.seconds / 60:.0f} 分钟"
            + (f" · {trace.objective[:60]}" if trace.objective else "")
        )
    lines += ["", PATROL_OBJECTIVE]
    return "\n".join(lines)


def apply_close(store: Any, run_id: int, hypothesis_id: int) -> str:
    """Abandon one board entry on a patrol's word. Returns ``""`` or a refusal.

    Three rules, and each one is a way this could have done real damage:

    - Only an ``open`` entry. A claimed one has a worker on it — abandoning it
      would leave the worker finishing an objective whose row says nobody wants
      it. A closed one already has a verdict; a second verdict is a contradiction.
    - Only this run's entry. A patrol reads text, and text can name any id.
    - Only on the event: the verdict is advisory everywhere else. This is the
      single place where a patrol's judgement changes the board, and it is
      deliberately not wired to ``_settle_hypothesis`` (which must stay grounded
      in a worker's observations).
    """
    row = store.get_hypothesis(int(hypothesis_id))
    if row is None:
        return f"假设 #{hypothesis_id} 不存在"
    if int(row.run_id) != int(run_id):
        return f"假设 #{hypothesis_id} 不属于这个 run"
    if row.status != swarm.OPEN:
        return f"假设 #{hypothesis_id} 当前是 {row.status}，只有 open 的才能被关闭"
    store.set_hypothesis_status(int(hypothesis_id), swarm.ABANDONED, clear_claim=True)
    return ""


_VERDICT_RE = re.compile(r"^\s*`?(CONTINUE|REPIVOT|STOP|CLOSE)\b\s*(\d+)?", re.I)


def parse_verdict(text: str) -> tuple[str, int | None, str]:
    """Read ``(action, id, reason)`` out of the patrol's reply.

    ``id`` is whatever the action names: a task id for ``STOP`` and a
    hypothesis id for ``CLOSE``. The two namespaces share one slot because the
    dispatch path is the same — ``STOP`` cancels a task, ``CLOSE`` abandons a
    board entry — and the caller knows which it asked for.
    """
    for raw in (text or "").splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        match = _VERDICT_RE.match(line)
        if not match:
            continue
        action = match.group(1).upper()
        target = int(match.group(2)) if match.group(2) else None
        reason = line[match.end():].lstrip(" -—:：").strip()
        if action == "STOP" and target is None:
            return ("CONTINUE", None, "巡检给了 STOP 但没给工单号，按继续处理")
        if action == "CLOSE" and target is None:
            return ("CONTINUE", None, "巡检给了 CLOSE 但没给假设号，按继续处理")
        return (action, target, reason[:200])
    return ("CONTINUE", None, "巡检没有给出可解析的结论，按继续处理")
