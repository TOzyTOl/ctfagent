"""Foundations: the clocks, the board, and what the worker is told it has.

Three gaps found by auditing the five things that have to be solid before any
capability work means anything — toolchain, execution chain, coordination, the
blackboard, and tasks that never end.

1. ``task_timeout_s`` was declared in config and enforced nowhere. It was only
   used as the wait timeout for the writer. Run 15's task #70 ran 3540 s against
   a 1800 s limit.
2. The blackboard digest took the newest 40 facts. Runs produce 51-74, so the
   oldest 11-34 were dropped — and the oldest are the foundational records.
3. The environment block told a worker about WSL and Git Bash but not that
   pwntools, z3 and gmpy2 were installed, so run 18's five workers hand-rolled a
   256-bit bignum library for 90 turns each.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.config import LimitsConfig
from ctfagent.models import TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus, _python_toolkit


class SilentBackend:
    """Starts a session, then never says anything again."""

    def __init__(self) -> None:
        self.calls: list = []

    async def run(self, spec):
        self.calls.append(spec)
        yield AgentEvent("init", {"session_id": "sess-silent"})
        await asyncio.sleep(60)


@pytest.fixture()
def build_run(cfg, store_and_bus):
    store, bus = store_and_bus

    def build(backend, challenge_dir: Path):
        manager = RunManager(
            store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
        )
        return manager.prepare(store.add_challenge(load_challenge(challenge_dir)))

    return build


async def test_a_task_cannot_outlive_its_wall_clock(cfg, fixtures_dir: Path, build_run) -> None:
    """Turns and dollars are ceilings, not clocks."""
    cfg.limits.task_timeout_s = 1
    cfg.limits.stall_timeout_s = 60  # keep the stall clock out of the way
    cfg.limits.stall_retries = 0
    run = build_run(SilentBackend(), fixtures_dir / "easy_crypto")

    task = await run.dispatch("crypto", "recover the plaintext")
    await run.wait([task.id], timeout_s=20)

    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status == TaskStatus.EXHAUSTED
    assert "任务超过" in finished.error


async def test_a_task_that_times_out_is_resumable(
    cfg, fixtures_dir: Path, build_run
) -> None:
    """The transcript survived, so the next worker continues instead of restarting."""
    cfg.limits.task_timeout_s = 1
    cfg.limits.stall_timeout_s = 60
    cfg.limits.stall_retries = 0
    run = build_run(SilentBackend(), fixtures_dir / "easy_crypto")

    task = await run.dispatch("crypto", "recover the plaintext")
    await run.wait([task.id], timeout_s=20)

    assert [t.id for t in run.resumable_tasks("crypto")] == [task.id]


def test_the_board_carries_the_whole_run(cfg, fixtures_dir: Path, build_run) -> None:
    """The oldest facts are the foundational ones; dropping them costs re-derivation."""
    run = build_run(ScriptedBackend(), fixtures_dir / "easy_crypto")
    for i in range(60):
        run.record_fact(
            None, "observation", f"事实-{i:02d}", {"command": "x", "output": "y"}, "observed"
        )

    digest = run.blackboard_digest(limit=cfg.limits.blackboard_facts)
    assert "事实-00" in digest  # the oldest survives
    assert "事实-59" in digest


def test_the_default_board_size_covers_a_real_run() -> None:
    # The largest run in the database produced 74 facts.
    assert LimitsConfig().blackboard_facts >= 74


def test_the_environment_names_the_installed_toolkit() -> None:
    """A worker that is not told z3 exists writes its own solver."""
    line = _python_toolkit()
    if not line:
        pytest.skip("no CTF packages installed in this interpreter")
    guidance = SandboxStatus("none", False, "test").guidance
    assert "# Python 工具链" in guidance
    assert line in guidance


class SilentClient:
    """The lead's persistent client, going quiet mid-turn."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str):
        self.sent.append(text)
        return self._turn()

    async def _turn(self):
        yield AgentEvent("init", {"session_id": "sess-lead"})
        await asyncio.sleep(30)


async def test_the_lead_turn_is_bounded_by_a_stall_clock(
    cfg, fixtures_dir: Path, build_run
) -> None:
    """Before this, a hung main session sat for the whole 5400 s run clock."""
    cfg.limits.stall_timeout_s = 1
    run = build_run(ScriptedBackend(), fixtures_dir / "easy_crypto")
    emitted: list[tuple[str, dict]] = []
    run._emit = lambda kind, payload, task_id=None: emitted.append((kind, payload))

    final = await run._stream(SilentClient(), "帮我看看这题", None, "main")

    # An empty payload is what keeps the run open: ``_session_spent({})`` is
    # False, so the chat loop waits for the operator instead of closing.
    assert final == {}
    kinds = [k for k, _ in emitted]
    assert "session.stalled" in kinds
    assert run._lead_turn_active is False


async def test_a_lead_stall_is_not_retried(cfg, fixtures_dir: Path, build_run) -> None:
    """A lead turn has side effects; replaying it would dispatch twice."""
    cfg.limits.stall_timeout_s = 1
    run = build_run(ScriptedBackend(), fixtures_dir / "easy_crypto")
    run._emit = lambda kind, payload, task_id=None: None
    client = SilentClient()

    await run._stream(client, "派个 worker 去看", None, "main")

    assert client.sent == ["派个 worker 去看"]  # sent once, not replayed


async def test_a_normal_lead_turn_still_returns_its_result(
    cfg, fixtures_dir: Path, build_run
) -> None:
    cfg.limits.stall_timeout_s = 30
    run = build_run(ScriptedBackend(), fixtures_dir / "easy_crypto")

    class QuickClient:
        def send(self, text: str):
            return self._turn()

        async def _turn(self):
            yield AgentEvent("init", {"session_id": "s"})
            yield AgentEvent("result", {"subtype": "success", "session_id": "s"})

    final = await run._stream(QuickClient(), "hi", None, "main")
    assert final.get("subtype") == "success"
