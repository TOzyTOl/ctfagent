"""蜂群 B 阶段（「蜂巢」）的编排测试：板子派人，而不是主 agent 派人。

三种东西在这里一起被证明，而且都不需要模型、不需要真实会话：

1. **协调器的规则**（`Run._board_tick`）——没有 open 假设不开工、一个方向同时
   只有一个 worker、派工被拒绝时假设必须回到板上（否则它会永远显示「在做」）。
2. **生命周期的结算**（`Run._settle_hypothesis`）——带观测依据的 dead_end 才算
   证伪，撞上限/崩溃只是把假设放回板上。这一条如果写反，板上会堆满「被证伪」
   却从来没人真试过的方向。
3. **预热**——板子派出 worker 之后，空闲的主 agent 会被叫醒去 wait，而不是坐在
   那里等一条永远不会来的用户消息。

`ScriptedBackend` 的会话立刻返回一个成功的 result，所以一个 tick 里派出的任务会
在下一个事件循环回合结束，测试只需要让出控制权几次。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ctfagent import swarm
from ctfagent import orchestrator as orch
from ctfagent.backend import ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.models import TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


def _hyp(family: str, statement: str, **kw) -> swarm.Hypothesis:
    kw.setdefault("criterion", "引号报错或布尔条件改变返回内容")
    kw.setdefault("evidence", "登录参数 id 直接拼进查询")
    return swarm.Hypothesis(family=family, statement=statement, **kw)


class Board:
    """A prepared run in board mode, with the coordinator armed by hand.

    ``run.start()`` would also open the main session; these tests are about the
    coordinator, so the loop is started on its own and shut down the same way.
    """

    def __init__(self, store: Store, bus: EventBus, cfg, fixtures_dir: Path, mode="board"):
        cfg.limits.swarm = mode
        self.store = store
        self.cfg = cfg
        self.manager = RunManager(
            store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"),
            cfg.skills_dir,
        )
        challenge = load_challenge(fixtures_dir / "easy_crypto")
        challenge.category = "crypto"
        self.run = self.manager.prepare(store.add_challenge(challenge))

    def post(self, family: str, statement: str, **kw) -> int:
        out = self.run.post_hypothesis(
            family=family,
            statement=statement,
            criterion=kw.pop("criterion", "引号报错或布尔条件改变返回内容"),
            evidence=kw.pop("evidence", "登录参数 id 直接拼进查询"),
            **kw,
        )
        assert "error" not in out, out
        return int(out["hypothesis_id"])

    async def stop(self) -> None:
        await self.run.shutdown("failed", "test cleanup", "test")


@pytest.fixture()
def board(cfg, fixtures_dir, store_and_bus):
    store, bus = store_and_bus
    b = Board(store, bus, cfg, fixtures_dir)
    yield b


async def test_off_by_default_arms_no_coordinator(cfg, fixtures_dir, store_and_bus) -> None:
    """The old shape must stay the old shape: no hidden loop in normal runs."""
    store, bus = store_and_bus
    b = Board(store, bus, cfg, fixtures_dir, mode="off")
    assert b.run.swarm_enabled is False
    b.run._start_board_loop()
    assert b.run._board_task is None


async def test_empty_board_starts_nothing(board: Board) -> None:
    """``idle`` is a result, not an instruction to invent work."""
    started = await board.run._board_tick()
    assert started == []
    assert board.store.list_tasks(board.run.id) == []


async def test_the_soft_ceiling_stops_the_board_but_leaves_the_entries_open(
    board: Board,
) -> None:
    """The 15% reserve only works if the coordinator honours it.

    Past 85% of the run budget the lead may no longer open new ground. The
    board starts nothing but exploration roles, so if it ignored the same
    ceiling it would spend the reserve the verifier needs — and a run that
    holds an unverified flag at the ceiling has wasted what it spent.
    """
    board.post("crypto", "模数可分解", priority=5)
    board.run._soft_stopped = True
    assert await board.run._board_tick() == []
    assert board.store.list_tasks(board.run.id) == []
    # Not withdrawn: the entry stays on the board, and a re-enabled run picks it
    # up on the next tick instead of paying for a new hypothesis.
    rows = board.store.list_hypotheses(board.run.id)
    assert [h.status for h in rows] == [swarm.OPEN]
    board.run._soft_stopped = False
    assert len(await board.run._board_tick()) == 1


async def test_the_board_ledger_holds_a_worker_back_before_the_ceiling(
    board: Board,
) -> None:
    """The 15% reserve must be visible to the dispatch, not only in the docs.

    ``_committed`` alone would let the board hand a worker its full role
    ceiling while the closing roles' share is already promised to someone
    else; folding in ``swarm.Budget`` makes the later dispatch the smaller of
    the two remainders.
    """
    run = board.run
    budget = run._board_budget
    assert budget is not None, "board mode must own a reservation ledger"
    run.cfg.limits.run_budget_usd = 100.0
    budget.ceiling = 100.0
    budget.settle(999, 70.0)  # 70 of the 85 usable (100 − 15% reserve) is spent
    run.cfg.limits.role_budget_usd["crypto"] = 20.0
    run.cfg.models.roles["crypto"] = "deepseek-v4-flash"
    run.cfg.models.cli_cost_factor["deepseek-v4-flash"] = 1.0

    hid = board.post("crypto", "模数可分解", priority=5)
    task = board.store.create_task(run.id, "crypto", "obj", "ctx", hid)
    ceiling, reservation = run._reserve_ceiling(
        task, "crypto", 0.0, started_by_board=True
    )
    # 15 free gateway dollars are all the board has to offer, whatever the role
    # ceiling says.
    assert reservation == pytest.approx(15.0)
    assert ceiling == pytest.approx(15.0)

    # A lead-dispatched session is not held back by this ledger: only the
    # board's own claims spend the reserve, and the closing roles must always
    # be able to start.
    plain = board.store.create_task(run.id, "crypto", "obj", "ctx")
    ceiling, reservation = run._reserve_ceiling(
        plain, "crypto", 0.0, started_by_board=False
    )
    assert ceiling == pytest.approx(20.0)


async def test_a_finished_board_task_releases_its_hold_and_books_its_spend(
    board: Board,
) -> None:
    """Settlement has to happen on every exit path, or the pool drains."""
    run = board.run
    budget = run._board_budget
    board.post("crypto", "模数可分解", priority=5)
    started = await run._board_tick()
    assert len(started) == 1
    task_id = started[0]
    # The scripted session spends nothing; the hold must come off anyway.
    for _ in range(40):
        await asyncio.sleep(0)
        if board.store.get_task(task_id).terminal:
            break
    assert task_id not in budget.holds
    assert budget.reserved == pytest.approx(0.0)
    assert budget.spent == pytest.approx(board.store.get_task_cost(task_id))


async def test_an_empty_idle_board_is_announced_once(board: Board) -> None:
    """``idle`` is a result the operator must see — and see exactly once.

    An empty board with nothing running is the one state where only the lead
    (and through it the operator) can act. Without the event the UI shows a
    quiet run with no explanation; without the latch a 2-second tick fills the
    rail with the same line.
    """
    idle = lambda: [
        e for e in board.store.events_since(board.run.id)
        if e.kind == "board.idle"
    ]
    assert await board.run._board_tick() == []
    assert await board.run._board_tick() == []
    assert len(idle()) == 1

    # Posting work ends the stretch; the next empty stretch is announced again.
    hid = board.post("crypto", "模数可分解", priority=5)
    started = await board.run._board_tick()
    assert len(started) == 1
    for _ in range(40):
        await asyncio.sleep(0)
        if board.store.get_task(started[0]).terminal:
            break
    board.store.set_hypothesis_status(hid, swarm.ABANDONED)
    assert await board.run._board_tick() == []
    assert len(idle()) == 2


async def test_an_exhausted_board_ledger_refuses_before_the_session_starts(
    board: Board,
) -> None:
    """A refusal must happen at dispatch, not inside a zero-ceiling session.

    The CLI rejects a zero budget in about a second, so letting the dispatch
    through would spend a session slot, write a ``task.started``/``failed``
    pair, and leave the entry needing the backoff to stop a retry each tick.
    Refusing here keeps the event honest: one ``board.refused`` with a reason.
    """
    run = board.run
    budget = run._board_budget
    assert budget is not None
    run.cfg.limits.run_budget_usd = 100.0
    budget.ceiling = 100.0
    budget.settle(999, 100.0)  # everything spent: no usable pool at all

    hid = board.post("crypto", "模数可分解", priority=5)
    assert await run._board_tick() == []
    assert board.store.list_tasks(run.id) == []
    assert board.store.get_hypothesis(hid).status == swarm.OPEN
    refused = [
        e for e in board.store.events_since(run.id)
        if e.kind == "board.refused"
    ]
    assert len(refused) == 1 and "额度" in refused[0].payload["error"]


async def test_open_hypothesis_starts_its_own_specialist(board: Board) -> None:
    hid = board.post("crypto", "模数可分解", priority=5)
    started = await board.run._board_tick()
    assert len(started) == 1
    task = board.store.get_task(started[0])
    assert task.role == "crypto"
    assert task.hypothesis_id == hid
    # And the worker was told what it is claiming, including the falsifier.
    assert "模数可分解" in task.context and "证伪判据" in task.context
    row = board.store.get_hypothesis(hid)
    # The lease may already have been settled and released by the scripted
    # worker (which ends instantly); either way the entry must never be left
    # claimed by a task that is gone.
    if row.status == swarm.CLAIMED:
        assert row.claimed_by == task.id
    await asyncio.sleep(0.05)
    await board.stop()


async def test_one_worker_per_family(board: Board) -> None:
    board.post("crypto", "模数可分解", priority=9)
    board.post("crypto", "指数过小可以开方", priority=8)
    board.post("web", "登录页存在注入", priority=1)
    started = await board.run._board_tick()
    roles = [board.store.get_task(t).role for t in started]
    assert roles.count("crypto") == 1
    await asyncio.sleep(0.05)
    await board.stop()


async def test_the_board_opens_several_directions_at_once(board: Board) -> None:
    """Different families run side by side, up to the board's own bound.

    The cap is a fan-out width, not a global switch: with room for three
    directions the coordinator opens three *different* families in one tick, and
    the family rule still holds inside it (no two workers of one family). A
    board bound of 2 made a lead that posted four falsifiable ideas execute them
    two at a time while the clock ran.
    """
    board.cfg.limits.swarm_max_workers = 3
    board.cfg.limits.workers_per_run = 4
    for family, statement in (
        ("crypto", "模数可分解"),
        ("web", "登录页存在注入"),
        ("misc", "隐写藏在 PNG 尾部"),
        ("forensics", "时间线里有重放"),
    ):
        board.post(family, statement)
    started = await board.run._board_tick()
    assert len(started) == 3
    roles = [board.store.get_task(t).role for t in started]
    assert len(set(roles)) == 3
    # The fourth direction stays open for the next free slot, not dropped.
    open_left = [
        h for h in board.store.list_hypotheses(board.run.id) if h.status == swarm.OPEN
    ]
    assert len(open_left) == 1
    await asyncio.sleep(0.05)
    await board.stop()


async def test_the_board_never_exceeds_the_run_pool(board: Board) -> None:
    """``swarm_max_workers`` cannot outbid ``workers_per_run``.

    Two knobs, and the smaller one wins: the run pool is the resource bound
    (sessions, dollars), the board bound only decides how much of it this
    mechanism may claim on its own. A board configured wider must not queue
    claimed-but-waiting sessions behind the pool semaphore — those hold their
    hypotheses while doing nothing.
    """
    board.cfg.limits.swarm_max_workers = 5
    board.cfg.limits.workers_per_run = 2
    for family in ("crypto", "web", "misc", "forensics"):
        board.post(family, f"{family} 方向的一条假设")
    started = await board.run._board_tick()
    assert len(started) == 2
    await asyncio.sleep(0.05)
    await board.stop()


async def test_a_crashed_worker_does_not_retire_its_family(board: Board) -> None:
    """Occupancy is liveness, not a lifetime counter.

    A worker that died in its first seconds left the direction on the board;
    if the coordinator counted "this family was started once" instead of "this
    family has a live worker", a single crash would park every remaining entry
    of that direction for the rest of the run.
    """
    hid = board.post("crypto", "模数可分解")
    task = board.store.create_task(board.run.id, "crypto", "obj", "", hid)
    board.run.bind_hypothesis(task.id, hid)
    board.store.finish_task(task.id, TaskStatus.FAILED, {}, "crashed")
    board.run._board_starts["crypto"] = 1  # it *was* started, once

    started = await board.run._board_tick()
    assert len(started) == 1
    assert board.store.get_task(started[0]).role == "crypto"
    await asyncio.sleep(0.05)
    await board.stop()



async def test_lead_tasks_occupy_the_pool_the_board_must_fit_in(board: Board) -> None:
    """A lead-dispatched task is a slot, not a veto.

    The coordinator's limit is shared with the lead's own dispatches — the same
    sessions, the same dollars. With the pool full it starts nothing; with one
    slot free it starts one direction and no more, which is the difference
    between "the board competes for capacity" and "any live task freezes the
    board" (that second reading would let a slow triage hold every open entry
    for its whole clock).
    """
    # Fill the pool to whatever the *smaller* of the two knobs allows — a board
    # bound wider than ``workers_per_run`` cannot open more slots than the pool
    # has, so sizing the lead tasks off ``swarm_max_workers`` alone would leave
    # a free slot the board is entitled to use.
    limit = min(
        int(board.cfg.limits.swarm_max_workers), int(board.cfg.limits.workers_per_run)
    )
    assert limit >= 2
    for i in range(limit):
        board.store.create_task(board.run.id, "web", f"lead 直接派的方向 {i}", "")
    board.post("crypto", "模数可分解")
    assert await board.run._board_tick() == []

    # One task finishes → one free slot → exactly one direction starts.
    lead = board.store.list_tasks(board.run.id)[0]
    board.store.finish_task(lead.id, TaskStatus.DONE, {}, "")
    started = await board.run._board_tick()
    assert len(started) == 1
    assert board.store.get_task(started[0]).role == "crypto"
    await asyncio.sleep(0.05)
    await board.stop()



async def test_refused_dispatch_returns_the_entry_to_the_board(board: Board) -> None:
    """A claim with no task behind it would read as ``working`` forever."""
    hid = board.post("crypto", "模数可分解")

    async def refuse(*_a, **_kw):
        raise RuntimeError("no money")

    board.run.dispatch = refuse  # type: ignore[method-assign]
    assert await board.run._board_tick() == []
    assert board.store.get_hypothesis(hid).status == swarm.OPEN
    await board.stop()


async def test_a_refused_family_is_not_retried_on_every_tick(board: Board) -> None:
    """The coordinator ticks every two seconds; the reasons a dispatch is
    refused do not heal on that clock.

    Without a backoff a run that ran out of money writes the same
    ``board.refused`` event twice a minute — three hundred identical error rows
    in ten minutes, and the operator reads each one as a new failure. Other
    families keep their chance: the refusal may be about one direction's cost.
    """
    board.post("crypto", "模数可分解")
    board.post("web", "登录页存在注入")
    attempts: list[str] = []

    async def refuse(role, *_a, **_kw):
        attempts.append(role)
        raise RuntimeError("no money")

    board.run.dispatch = refuse  # type: ignore[method-assign]
    assert await board.run._board_tick() == []
    assert attempts == ["crypto"]

    # The next tick leaves the refused family alone…
    assert await board.run._board_tick() == []
    assert attempts == ["crypto", "web"]
    # …and the tick after that starts nothing at all, rather than re-refusing.
    # The refreshed deadline is pinned by the constant: a two-second tick
    # against a one-minute window cannot legitimately expire inside a test.
    assert orch._BOARD_REFUSAL_BACKOFF_S >= 30 * orch._BOARD_TICK_S
    assert await board.run._board_tick() == []
    assert len(attempts) == 2
    await board.stop()


async def test_a_successful_start_clears_the_backoff(board: Board) -> None:
    """Once a family gets a worker, the coordinator must be free to use it
    again when that worker is done — a backoff that outlives the refusal would
    park the direction for a minute after the reason has gone."""
    board.post("crypto", "模数可分解")
    attempts: list[str] = []

    async def refuse(role, *_a, **_kw):
        attempts.append(role)
        raise RuntimeError("no money")

    real = board.run.dispatch
    board.run.dispatch = refuse  # type: ignore[method-assign]
    await board.run._board_tick()
    board.run._board_backoff.clear()  # the operator topped up the budget

    board.run.dispatch = real  # type: ignore[method-assign]
    started = await board.run._board_tick()
    assert started, "the retry after a cleared refusal must start a worker"
    assert "crypto" not in board.run._board_backoff
    await asyncio.sleep(0.05)
    await board.stop()


async def test_board_digest_shows_directions_and_state(board: Board) -> None:
    hid = board.post("crypto", "模数可分解")
    digest = board.run.blackboard_digest(for_main=True)
    assert "## 假设板" in digest and f"#{hid}" in digest
    assert "待认领" in digest and "证伪判据" in digest
    await board.stop()


class TestSettlement:
    """What a finished task does to the direction it was holding."""

    def _claimed_task(self, board: Board, family="crypto", statement="模数可分解"):
        hid = board.post(family, statement)
        task = board.store.create_task(board.run.id, family, "obj", "", hid)
        board.run.bind_hypothesis(task.id, hid)
        return hid, task

    def test_grounded_dead_end_refutes(self, board: Board) -> None:
        hid, task = self._claimed_task(board)
        board.run.record_fact(
            task.id, "dead_end", "模数不可分解：试了 10^6 以内所有因子",
            {
                # Both halves are mandatory: the board already refuses to treat
                # a negative without them as settled (models.negative_gap).
                "scope": "p,q < 10^6 全枚举",
                "control": "同一个枚举器在另一题找到了 17",
                "command": "python trial_div.py n 1000000",
                "output": "no factor found among 78498 primes",
            },
            "observed",
        )
        board.run._settle_hypothesis(task.id, TaskStatus.DONE)
        row = board.store.get_hypothesis(hid)
        assert row.status == swarm.REFUTED and row.refutes == 1

    def test_ungrounded_dead_end_does_not_refute(self, board: Board) -> None:
        """Without scope and control it is a location someone looked at."""
        hid, task = self._claimed_task(board)
        board.run.record_fact(
            task.id, "dead_end", "这条路走不通", {}, "inferred"
        )
        board.run._settle_hypothesis(task.id, TaskStatus.DONE)
        assert board.store.get_hypothesis(hid).status == swarm.SUPPORTED

    def test_hitting_a_ceiling_releases_the_entry(self, board: Board) -> None:
        hid, task = self._claimed_task(board)
        board.run._settle_hypothesis(task.id, TaskStatus.EXHAUSTED)
        row = board.store.get_hypothesis(hid)
        assert row.status == swarm.OPEN and row.refutes == 0
        # The owner goes with the lease. An entry that reads ``open`` but still
        # names the dead task is a claim no coordinator can hand over (and every
        # rendering shows a worker that no longer exists).
        assert row.claimed_by is None

    def test_a_done_task_without_evidence_does_not_close_the_direction(self, board: Board) -> None:
        """``done`` with nothing on the blackboard is a stop, not a conclusion.

        ``task_complete`` accepts only done/blocked, so a worker that ran out of
        ideas closes ``done`` while having recorded no fact. Treating that as a
        settled direction would retire a route nobody disproved — the same
        mistake as counting a crash as a refutation, one status over.
        """
        hid, task = self._claimed_task(board)
        board.run._settle_hypothesis(task.id, TaskStatus.DONE)
        assert board.store.get_hypothesis(hid).status == swarm.OPEN
        assert board.store.get_hypothesis(hid).claimed_by is None

    def test_flag_candidate_supports(self, board: Board) -> None:
        """A recorded flag candidate is a positive observation: the direction
        produced something, so it closes as supported instead of going back on
        the board for a second worker."""
        hid, task = self._claimed_task(board)
        board.run.record_fact(
            task.id,
            "flag_candidate",
            "疑似 flag 值",
            {"command": "python solve.py", "output": "flag{easy_rsa}"},
            "observed",
        )
        board.run._settle_hypothesis(task.id, TaskStatus.DONE)
        assert board.store.get_hypothesis(hid).status == swarm.SUPPORTED

    def test_a_foreign_task_cannot_settle_someone_elses_entry(self, board: Board) -> None:
        hid, task = self._claimed_task(board)
        other = board.store.create_task(board.run.id, "web", "obj", "")
        board.run._settle_hypothesis(other.id, TaskStatus.DONE)
        assert board.store.get_hypothesis(hid).status == swarm.CLAIMED


class TestClosure:
    """证伪之后，方向必须真的关死：可见、不可重派、不可静默重挂。"""

    def _refuted(self, board: Board, family="pwn", statement="slot 越界能泄 PIE"):
        hid = board.post(family, statement)
        task = board.store.create_task(board.run.id, family, "obj", "", hid)
        board.run.bind_hypothesis(task.id, hid)
        board.run.record_fact(
            task.id, "dead_end", "slot 下标有 0x1f 边界检查，无越界",
            {
                "scope": "submit/view/withdraw/restore 全部下标 0..31/-1 实测",
                "control": "readelf 确认 cmp eax,0x1f; ja 无符号比较",
                "command": "overs.py --remote",
                "output": "idx=-1,32 均被拒",
            },
            "observed",
        )
        board.run._settle_hypothesis(task.id, TaskStatus.DONE)
        assert board.store.get_hypothesis(hid).status == swarm.REFUTED
        return hid

    def test_refuted_direction_is_shown_to_the_lead(self, board: Board) -> None:
        hid = self._refuted(board)
        digest = board.run.blackboard_digest(for_main=True)
        assert f"#{hid}" in digest
        assert "已关闭方向" in digest
        assert "已证伪" in digest
        assert board.store.get_hypothesis(hid).statement in digest

    def test_refuted_direction_cannot_be_rebound(self, board: Board) -> None:
        hid = self._refuted(board)
        task = board.store.create_task(board.run.id, "pwn", "obj", "", hid)
        out = board.run.bind_hypothesis(task.id, hid)
        assert "error" in out
        row = board.store.get_hypothesis(hid)
        assert row.status == swarm.REFUTED

    def test_reposting_a_refuted_direction_does_not_reopen_it(self, board: Board) -> None:
        hid = self._refuted(board)
        out = board.run.post_hypothesis(
            family="pwn",
            statement="slot 越界能泄 PIE",
            criterion="slot 32 能读 .bss",
            evidence="重试",
        )
        assert out.get("created") is False
        assert out.get("status") == swarm.REFUTED
        assert "note" in out
        assert board.store.get_hypothesis(hid).status == swarm.REFUTED

    def test_a_released_lease_does_not_lose_a_grounded_refutation(self, board: Board) -> None:
        hid = board.post("pwn", "堆地址能推 PIE")
        task = board.store.create_task(board.run.id, "pwn", "obj", "", hid)
        board.run.bind_hypothesis(task.id, hid)
        # Cleanup ran first (crash path): the lease is gone, the row is open.
        board.store.set_hypothesis_status(hid, swarm.OPEN, clear_claim=True)
        board.run.record_fact(
            task.id, "dead_end", "堆与 PIE 间距 62MB~655MB 随机",
            {
                "scope": "8 次进程各测 maps",
                "control": "同一命令在 ASLR off 下间距恒定",
                "command": "rel.py",
                "output": "heap-pie: 62308352..655327232",
            },
            "observed",
        )
        board.run._settle_hypothesis(task.id, TaskStatus.DONE)
        assert board.store.get_hypothesis(hid).status == swarm.REFUTED

    def test_settling_twice_does_not_double_count(self, board: Board) -> None:
        hid = self._refuted(board)
        assert board.store.get_hypothesis(hid).refutes == 1
        # A duplicate settle (e.g. timeout cleanup racing finish) must be a no-op.
        task = board.store.create_task(board.run.id, "pwn", "obj", "", hid)
        board.run._settle_hypothesis(task.id, TaskStatus.DONE)
        assert board.store.get_hypothesis(hid).refutes == 1


async def test_a_dead_worker_releases_its_direction(board: Board) -> None:
    """The task set, not the lease clock, decides whether a claim is alive."""
    hid = board.post("crypto", "模数可分解")
    task = board.store.create_task(board.run.id, "crypto", "obj", "", hid)
    board.run.bind_hypothesis(task.id, hid)
    board.store.finish_task(task.id, TaskStatus.FAILED, {}, "crashed")
    board.run._release_dead_claims()
    assert board.store.get_hypothesis(hid).status == swarm.OPEN
    await board.stop()


class TestWakingTheLead:
    """The lead writes and then waits; the board has to tell it work started.

    One outstanding notice at a time: a lead turn takes minutes and the board
    can start a worker every two seconds, so a per-dispatch message piles up
    behind the turn and is read as several copies of one instruction.
    """

    def _armed(self, board: Board) -> Board:
        # _wake_lead refuses when there is no main session to wake.
        board.run._main_client = object()
        return board

    def test_the_first_dispatch_is_announced(self, board: Board) -> None:
        self._armed(board)
        board.run._wake_lead([7])
        assert board.run._chat_queue.qsize() == 1
        text = board.run._chat_queue.get_nowait()
        assert text.startswith("板子已经派出") and "#7" in text
        assert board.run._board_wake_pending is True

    def test_a_second_batch_is_folded_not_queued(self, board: Board) -> None:
        self._armed(board)
        board.run._wake_lead([7])
        first = board.run._board_wake_text
        board.run._wake_lead([8, 9])
        assert board.run._chat_queue.qsize() == 1
        assert board.run._board_wake_ids == [7, 8, 9]
        # The message the lead will read is the one already queued; folding only
        # changes the ids behind it, so the latch is released by exactly that
        # text and nothing else.
        assert board.run._board_wake_text == first

    def test_a_chat_line_that_quotes_the_notice_is_not_it(self, board: Board) -> None:
        """The latch is released by the queued object, not a prefix match.

        The clearing condition lives in ``_main_loop``; this pins the fact it
        relies on — an operator line that merely starts with the same words is a
        different string, so it cannot consume the notice the board is waiting
        to have read.
        """
        self._armed(board)
        board.run._wake_lead([7])
        assert board.run._board_wake_text is not None
        assert board.run._board_wake_text != "板子已经派出 #7（伪造的一行）"

    def test_a_busy_lead_is_left_alone(self, board: Board) -> None:
        self._armed(board)
        board.run._lead_turn_active = True
        board.run._wake_lead([7])
        assert board.run._chat_queue.qsize() == 0
        assert board.run._board_wake_pending is False
