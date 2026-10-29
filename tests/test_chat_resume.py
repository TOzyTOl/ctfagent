"""Chat stays usable after the process that owned the run is gone.

Live symptom: the workspace composer read ``disabled`` on every run started
before a server restart, and posting to it answered ``?notice=detached``. The
lead's transcript was on disk the whole time — only its session id was never
recorded, so nothing knew what to resume.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.models import RunStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


def _manager(store, bus, cfg, backend) -> RunManager:
    return RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )


def test_the_main_session_id_is_recorded_so_chat_can_resume(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    store, bus = store_and_bus
    run = _manager(store, bus, cfg, ScriptedBackend()).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    )
    run._record_event(
        AgentEvent("init", {"session_id": "sess-main", "subtype": "status"}),
        None,
        "main",
    )
    run._record_event(
        AgentEvent(
            "result",
            {"subtype": "success", "is_error": False, "result": "", "session_id": "sess-main"},
            cost_usd=0.70,
        ),
        None,
        "main",
        cost=0.70,
    )
    saved = store.get_run(run.id)
    assert saved.main_session_id == "sess-main"
    assert saved.main_cost_usd == 0.70


async def test_a_restart_does_not_charge_the_transcript_twice(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A resumed client reports the transcript's total, not this turn's cost.

    Without seeding from what was already booked, the first chat turn after a
    restart bills the entire history to the run a second time.
    """
    store, bus = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run_id = store.create_run(challenge_id, "solve").id
    store.set_main_session(run_id, "sess-main", 1.00)
    store.set_run_status(run_id, RunStatus.FAILED)
    store.add_run_cost(run_id, 1.00)

    backend = ScriptedBackend()
    backend.script["main"] = [
        [AgentEvent("result", {"subtype": "success", "is_error": False, "session_id": "sess-main"},
                    cost_usd=1.25)],
    ]
    manager = _manager(store, bus, cfg, backend)
    live = await manager.revive(run_id)
    assert live is not None

    await live.chat("继续找 flag")
    for _ in range(200):
        if store.get_run(run_id).cost_usd > 1.0:
            break
        await asyncio.sleep(0.02)
    assert backend.client_specs and backend.client_specs[0].resume_session_id == "sess-main"
    # The 1.00 already on the transcript is not billed again: only the delta.
    assert round(store.get_run(run_id).cost_usd, 4) == 1.25


async def test_revive_refuses_a_run_with_nothing_to_resume(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    store, bus = store_and_bus
    run = store.create_run(
        store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")), "solve"
    )
    assert await _manager(store, bus, cfg, ScriptedBackend()).revive(run.id) is None


async def test_revive_refuses_a_run_that_is_over_its_dollar_ceiling(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Reopening an insolvent run cannot produce an answer, so it is refused.

    The watchdog polls every two seconds and compares ``cost_usd`` against
    ``run_budget_usd``; reopening restarts the clock but not the ledger, so the
    next poll closes the run again before the lead's first turn finishes.
    """
    store, bus = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run_id = store.create_run(challenge_id, "solve").id
    store.set_main_session(run_id, "sess-main", 6.0)
    store.set_run_status(run_id, RunStatus.FAILED, summary="超过预算")
    store.add_run_cost(run_id, cfg.limits.run_budget_usd + 2.0)

    manager = _manager(store, bus, cfg, ScriptedBackend())
    assert await manager.revive(run_id) is None
    # Nothing was left half-attached: a later revive must not find a live run.
    assert manager.get(run_id) is None
    assert store.get_run(run_id).status == RunStatus.FAILED


async def test_revive_reopens_a_closed_run_for_chat(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The operator's next sentence reaches a lead whose run already closed."""
    store, bus = store_and_bus
    backend = ScriptedBackend()
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run_id = store.create_run(challenge_id, "solve").id
    store.set_main_session(run_id, "sess-main", 0.5)
    store.set_run_status(run_id, RunStatus.FAILED, summary="时限用尽")

    manager = _manager(store, bus, cfg, backend)
    live = await manager.revive(run_id)
    assert live is not None and not live.closed
    await live.wait_until_listening(timeout=5)
    await live.chat("接着之前的思路，flag 可能在 config 里")

    for _ in range(200):
        messages = store.list_messages(run_id)
        if any(m["role"] == "assistant" for m in messages) and backend.client_turns.get("main", 0):
            break
        await asyncio.sleep(0.02)

    assert "Start working on this challenge" not in " ".join(backend.client_texts)
    assert "接着之前的思路" in " ".join(backend.client_texts)
    # Reopening is not pretending nothing happened: the conversation the
    # operator is reading is the one on the page.
    assert store.get_run(run_id).status in (RunStatus.RUNNING, RunStatus.NEEDS_INPUT)
    await live.shutdown(RunStatus.STOPPED, "test", "test")
