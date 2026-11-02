"""蜂巢模式的端到端：主 agent 挂单，板子派人，worker 回来把票投进闸门。

`test_swarm_board.py` 证明的是协调器自己的规则（一条一条调用 `_board_tick`）。
这个文件证明的是另一件事：**接上线之后它真的会自己跑起来**。主 agent 从头到尾
没有调用过 `dispatch_task`，worker 是板子起的；worker 提交的 flag 仍然走
`propose_flag → verifier → 人工确认` 那条原来的链，一个字没动。

这也是「A 阶段的星型形态没有被改坏」的反面证据：同一个 run 里，主 agent 用老办法
派工的路径仍然可用（`test_swarm_off_keeps_the_star_shape`）。

用 `ScriptedBackend`：没有模型、没有网络、没有子进程。协调器的 2 秒节拍在这里
被打掉（`_BOARD_TICK_S`），否则每个用例都要多等一拍。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ctfagent import orchestrator as orch
from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.models import FlagStatus, RunStatus, TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.worker_tools import WorkerTools
from ctfagent.workspace import SandboxStatus

FLAG = "flag{h1ve}"


class Lead:
    """主 agent 的替身：只挂单，不派工。

    `posted` 记下它写进板子的假设 id —— 用例拿它来断言「派出去的那个工单绑定的
    正是主 agent 写的那条」，而不是板子自己凭空起的。
    """

    def __init__(self, family: str = "crypto", crash_first: bool = False) -> None:
        self.run: Any = None
        self.family = family
        self.posted: list[int] = []
        self.dispatch_calls: list[dict[str, Any]] = []
        self.worker_tasks: list[int] = []
        #: The first crypto worker ends on a ceiling instead of a conclusion.
        #: That is what a real crash or turn limit looks like from the board's
        #: side, and the direction must come back for a second try.
        self.crash_first = crash_first
        self.crypto_starts = 0

    async def driver(self, spec, text: str) -> list[AgentEvent]:
        if spec.role != "main" or self.run is None:
            return []
        if not text.startswith("Start working"):
            return []
        out = self.run.tools.post_hypothesis(
            family=self.family,
            statement="模数可分解",
            criterion="费马分解在 10^6 窗口内找不到因子",
            evidence="n 的 p 与 q 相差不到 10^6",
            priority=5,
        )
        assert "error" not in out, out
        self.posted.append(int(out["hypothesis_id"]))
        return [AgentEvent("text", {"text": "挂在板上：模数可分解"})]

    async def dispatch(self, original, role: str, objective: str, context: str = "", **kw):
        """板子派下来的工单真正走一次 dispatch，然后由替身扮演 worker 的写回。"""
        self.dispatch_calls.append({"role": role, "objective": objective})
        task = await original(role, objective, context, **kw)
        self.worker_tasks.append(task.id)
        tools = WorkerTools(self.run, task.id)
        if role == "crypto":
            self.crypto_starts += 1
            if self.crash_first and self.crypto_starts == 1:
                # The first worker ends on its turn ceiling: the scripted
                # backend below returns ``error_max_turns`` for this session, so
                # no write-back happens and no fact is recorded.
                return task
            tools.record_fact(
                "n 分解为两个相差 10^6 以内的素数（费马）",
                kind="insight",
                basis="observed",
                evidence={
                    "command": "python -c 'fermat(n)'",
                    "output": "p=2305843009213706309 q=2305843009214681609",
                },
            )
            await tools.submit_flag(
                FLAG,
                method="费马分解后 d = e^-1 mod phi，再解密",
                source="work/solve.py",
                confidence="high",
            )
            tools.task_complete(summary=f"解出 {FLAG}", status="done")
        elif role == "verifier":
            tools.task_complete(
                summary=f"VERDICT: reproduced\n独立重算得到 {FLAG}",
                status="done",
            )
        return task


@pytest.fixture(autouse=True)
def fast_tick(monkeypatch) -> None:
    """协调器每 2 秒一拍对测试是纯等待。"""
    monkeypatch.setattr(orch, "_BOARD_TICK_S", 0.01)


class CeilingFirstBackend(ScriptedBackend):
    """The first crypto worker ends on a turn ceiling, the rest succeed.

    A worker that dies has no result to write back — the ceiling is the CLI's
    verdict on the session, not something the worker chose. Scripting it at the
    event level is what makes the test exercise the real path
    (``_status_from_result`` → ``_finish_task`` → ``_settle_hypothesis``)
    instead of a short circuit inside the stand-in worker.
    """

    def __init__(self) -> None:
        super().__init__()
        self.crypto_runs = 0

    async def run(self, spec):
        if spec.role == "crypto":
            self.crypto_runs += 1
            if self.crypto_runs == 1:
                self.calls.append(spec)
                yield AgentEvent(
                    "result",
                    {"subtype": "error_max_turns", "is_error": True, "result": "", "session_id": ""},
                    turns=150,
                )
                return
        async for event in super().run(spec):
            yield event


async def _wait_for(predicate, tries: int = 600, step: float = 0.02) -> bool:
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(step)
    return False


def _flag(store: Store, run_id: int, value: str = FLAG):
    return next((f for f in store.list_flags(run_id) if f.value == value), None)


def _build(store: Store, bus: EventBus, cfg, fixtures_dir: Path, mode: str, backend, lead: Lead):
    cfg.limits.swarm = mode
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    challenge = load_challenge(fixtures_dir / "easy_crypto")
    challenge.category = "crypto"
    run = manager.prepare(store.add_challenge(challenge))
    lead.run = run
    original = run.dispatch
    run.dispatch = lambda role, objective, context="", **kw: lead.dispatch(  # type: ignore[method-assign]
        original, role, objective, context, **kw
    )
    return run


async def test_board_mode_runs_the_whole_gate_without_a_single_lead_dispatch(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """主 agent 只挂单；板子派工；flag 仍然走 verifier → 人工确认。"""
    store, bus = store_and_bus
    lead = Lead()
    backend = ScriptedBackend()
    backend.driver = lead.driver
    run = _build(store, bus, cfg, fixtures_dir, "board", backend, lead)

    await run.start()
    assert await _wait_for(lambda: _flag(store, run.id) is not None), (
        "板子没有派人，或者 worker 没有写回"
    )
    flag = _flag(store, run.id)
    assert flag is not None
    # 派出去的那张工单绑的正是主 agent 写的那条假设。
    task = store.get_task(lead.worker_tasks[0])
    assert task.hypothesis_id == lead.posted[0]
    assert task.role == "crypto"

    assert await _wait_for(lambda: store.get_flag(flag.id).status == FlagStatus.VERIFIED)
    assert await _wait_for(
        lambda: store.get_run(run.id).status == RunStatus.NEEDS_INPUT, tries=100
    ), "验证器复现之后 run 必须停下来等人，而不是自己判赢"

    # 门禁链的下半段没有因为蜂群化而变松：仍然要人按一次。
    await run.confirm_flag(flag.id, note="checked by hand")
    assert store.get_flag(flag.id).status == FlagStatus.CONFIRMED
    assert store.get_run(run.id).status == RunStatus.SOLVED
    await run.shutdown(RunStatus.SOLVED, "done", "test")


async def test_a_worker_that_dies_returns_its_direction_to_the_board(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """撞上限的 worker 不能锁死一条方向：同一拍之后板子必须把它重新派出去。

    这一条是蜂群最容易被写错的地方——「会话没了」被当成「方向错了」，或者反过来，
    方向永远显示「在做」而没人真的在做。两件事同时断言：假设回到 open，且第二个
    worker 确实是同一条假设、同一个 family。
    """
    store, bus = store_and_bus
    lead = Lead(crash_first=True)
    backend = CeilingFirstBackend()
    backend.driver = lead.driver
    run = _build(store, bus, cfg, fixtures_dir, "board", backend, lead)

    await run.start()
    assert await _wait_for(lambda: len(lead.worker_tasks) >= 2, tries=800), (
        f"第一个 worker 撞上限之后，板子没有把它重新派出去（只起了 "
        f"{len(lead.worker_tasks)} 个）"
    )
    first, second = store.get_task(lead.worker_tasks[0]), store.get_task(lead.worker_tasks[1])
    assert first.id != second.id
    assert first.hypothesis_id == second.hypothesis_id == lead.posted[0]
    assert second.role == "crypto"
    row = store.get_hypothesis(int(lead.posted[0]))
    assert row.refutes == 0, "撞上限不是证伪，不能被记成一次 refute"
    await run.shutdown(RunStatus.STOPPED, "operator stop", "test")


async def test_swarm_off_keeps_the_star_shape(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """默认形态一个字没变：没有协调器，主 agent 派工照旧。"""
    store, bus = store_and_bus
    lead = Lead()
    lead.driver = _never_posts(lead)  # type: ignore[assignment]
    backend = ScriptedBackend()
    backend.driver = lead.driver

    async def star_driver(spec, text: str) -> list[AgentEvent]:
        if spec.role != "main" or not text.startswith("Start working"):
            return []
        out = await lead.run.tools.dispatch_task(role="crypto", objective="老办法派的工")
        await lead.run.tools.wait_tasks([out["task_id"]], timeout_s=5)
        return []

    backend.driver = star_driver
    run = _build(store, bus, cfg, fixtures_dir, "off", backend, lead)
    assert run._board_task is None

    await run.start()
    assert await _wait_for(lambda: bool(store.list_tasks(run.id)))
    assert run._board_task is None, "off 模式下不许有隐藏的协调器"
    assert lead.dispatch_calls and lead.dispatch_calls[0]["role"] == "crypto"
    await run.shutdown(RunStatus.STOPPED, "operator stop", "test")


def _never_posts(lead: Lead):
    async def driver(spec, text: str) -> list[AgentEvent]:
        return []

    return driver


async def _first(getter, tries: int, step: float = 0.02):
    for _ in range(tries):
        value = getter()
        if value:
            return value[0]
        await asyncio.sleep(step)
    return None
