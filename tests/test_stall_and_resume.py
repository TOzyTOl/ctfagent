"""The two places a run pays twice for the same analysis.

Both were measured, not hypothesised.

1. **A model stall closed the task.** Run 15: four reverse tasks went silent at
   exactly 300 s after 16-66 tool calls each. 1.91 h of analysis discarded, and
   because ``turns`` and ``cost_usd`` are only booked on the result event, the
   dashboard showed ``turns=0, cost=$0`` for all four — as if nothing had
   happened. A silent model is a transport hiccup; the transcript is intact.
2. **A finished direction could be re-dispatched from scratch.** The guard only
   looked at tasks still running, so run 15 dispatched seven reverse workers,
   each re-reading the same 1312-layer archive from the beginning.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.config import LimitsConfig, load_config
from ctfagent.models import TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.prompts import stall_resume_text
from ctfagent.workspace import SandboxStatus


class StallingBackend:
    """Goes silent on the first attempt, answers on the resumed one."""

    def __init__(self) -> None:
        self.calls: list = []

    async def run(self, spec):
        self.calls.append(spec)
        if len(self.calls) == 1:
            yield AgentEvent("init", {"session_id": "sess-first"})
            await asyncio.sleep(30)  # the model call never comes back
        else:
            yield AgentEvent("init", {"session_id": "sess-second"})
            yield AgentEvent("result", {"subtype": "success", "session_id": "sess-second"}, turns=1)


@pytest.fixture()
def manager_factory(cfg, store_and_bus):
    store, bus = store_and_bus

    def build(backend, challenge_dir: Path):
        manager = RunManager(
            store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
        )
        return manager.prepare(store.add_challenge(load_challenge(challenge_dir)))

    return build


async def test_a_model_stall_is_resumed_instead_of_closing_the_task(
    cfg, fixtures_dir: Path, manager_factory
) -> None:
    cfg.limits.stall_timeout_s = 1
    cfg.limits.stall_retries = 1
    backend = StallingBackend()
    run = manager_factory(backend, fixtures_dir / "easy_crypto")

    task = await run.dispatch("crypto", "recover the plaintext")
    await run.wait([task.id], timeout_s=20)

    # Two sessions: the one that went silent and the resumed one.
    assert len(backend.calls) == 2
    assert backend.calls[0].resume_session_id == ""
    assert backend.calls[1].resume_session_id == "sess-first"
    assert backend.calls[1].prompt == stall_resume_text()

    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status == TaskStatus.DONE


async def test_the_stall_retry_is_bounded(cfg, fixtures_dir: Path, manager_factory) -> None:
    """One retry, not a loop: a gateway that stays down must not spin."""
    cfg.limits.stall_timeout_s = 1
    cfg.limits.stall_retries = 1

    class AlwaysStalls(StallingBackend):
        async def run(self, spec):
            self.calls.append(spec)
            yield AgentEvent("init", {"session_id": "sess-first"})
            await asyncio.sleep(30)

    backend = AlwaysStalls()
    run = manager_factory(backend, fixtures_dir / "easy_crypto")
    task = await run.dispatch("crypto", "recover the plaintext")
    await run.wait([task.id], timeout_s=20)

    assert len(backend.calls) == 2  # first attempt + one retry
    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status == TaskStatus.EXHAUSTED
    assert "卡住了" in finished.error


async def test_a_finished_direction_is_not_re_dispatched_from_scratch(
    cfg, fixtures_dir: Path, manager_factory
) -> None:
    run = manager_factory(ScriptedBackend(), fixtures_dir / "easy_crypto")
    prior = await run.dispatch("reverse", "trace the VM")
    run.store.finish_task(
        prior.id, TaskStatus.EXHAUSTED, {}, "turns", session_id="sess-prior"
    )

    out = await run.tools.dispatch_task(role="reverse", objective="try again")
    assert "error" in out
    assert "可续跑" in out["error"]
    assert out["resumable_tasks"] == [prior.id]


async def test_force_still_allows_a_genuinely_different_angle(
    cfg, fixtures_dir: Path, manager_factory
) -> None:
    run = manager_factory(ScriptedBackend(), fixtures_dir / "easy_crypto")
    prior = await run.dispatch("reverse", "trace the VM")
    run.store.finish_task(
        prior.id, TaskStatus.EXHAUSTED, {}, "turns", session_id="sess-prior"
    )

    out = await run.tools.dispatch_task(role="reverse", objective="static instead", force=True)
    assert "task_id" in out


def test_resumable_tasks_need_a_session_id(cfg, fixtures_dir: Path, manager_factory) -> None:
    """Without a session id there is nothing to resume, so a fresh worker is right."""
    run = manager_factory(ScriptedBackend(), fixtures_dir / "easy_crypto")
    run.store.create_task(run.id, "reverse", "x", "")
    assert run.resumable_tasks("reverse") == []


def test_stall_retries_defaults_to_one() -> None:
    assert LimitsConfig().stall_retries == 1


def test_the_shipped_config_enables_the_retry() -> None:
    assert load_config().limits.stall_retries >= 1
