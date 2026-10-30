"""The verifier's conclusion has to arrive as a field, not a sentence.

Measured on the database: 15 verifier runs produced 6 ``verdict=none``. Each of
those left a flag in ``candidate`` with nothing automated attached to it, so the
operator had to read the tail of the report and decide by hand. Verification is
the gate between "a worker thinks it found something" and "the team believes it";
a gate that only works 60% of the time is the weakest link in the chain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.backend import ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.models import FlagStatus, TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


@pytest.fixture()
def run(cfg, fixtures_dir: Path, store_and_bus):
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    return manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))


def _bind(run, value: str = "flag{x}"):
    flag = run.store.add_flag(run.id, None, value, "work/solve.py", "m", "high")
    assert flag is not None
    run._verifier_flags[99] = flag.id
    return flag


def test_a_structured_verdict_beats_the_prose(run) -> None:
    """The field is what the orchestrator asked for; prose is only a fallback."""
    flag = _bind(run)
    run._apply_verdict(99, "VERDICT: reproduced\nlooks right to me", "refuted")
    assert run.store.get_flag(flag.id).status == FlagStatus.REJECTED


@pytest.mark.parametrize(
    "summary",
    [
        "VERDICT: reproduced",
        "**VERDICT**: reproduced",
        "verdict = reproduced",
        "结论：复现成功",
        "结论: 复现",
    ],
)
def test_the_fallback_reader_accepts_the_shapes_verifiers_actually_write(
    run, summary: str
) -> None:
    flag = _bind(run)
    run._apply_verdict(99, summary, "")
    assert run.store.get_flag(flag.id).status == FlagStatus.VERIFIED


def test_an_unrecognised_field_falls_back_to_the_prose(run) -> None:
    flag = _bind(run)
    run._apply_verdict(99, "VERDICT: refuted", "pretty sure")
    assert run.store.get_flag(flag.id).status == FlagStatus.REJECTED


def test_no_verdict_anywhere_leaves_the_flag_for_the_operator(run) -> None:
    flag = _bind(run)
    run._apply_verdict(99, "I checked the artifact but ran out of time.", "")
    # Still a candidate: the operator decides. The point is that it is not
    # silently marked verified, and not silently rejected either.
    assert run.store.get_flag(flag.id).status == FlagStatus.CANDIDATE


def test_inconclusive_keeps_the_flag_a_candidate(run) -> None:
    flag = _bind(run)
    run._apply_verdict(99, "", "inconclusive")
    assert run.store.get_flag(flag.id).status == FlagStatus.CANDIDATE


def test_complete_task_keeps_only_a_recognised_verdict(run) -> None:
    run.complete_task(7, status="done", summary="s", verdict="pretty sure")
    assert run._results[7]["verdict"] == ""

    run.complete_task(8, status="done", summary="s", verdict="REFUTED")
    assert run._results[8]["verdict"] == "refuted"


def test_the_verifier_prompt_asks_for_the_field() -> None:
    from ctfagent.prompts import verifier_prompt

    text = verifier_prompt("challenge", "board")
    assert "`verdict`" in text
    assert "not only as a sentence" in text


# ------------------------------------------------------- confirm vs verifier
# Live evidence: 15 of 30 verifiers in the database died at zero turns because
# the operator confirmed the flag while they were mid-flight and the close
# swept them away. Run 81's verifier was killed 42 seconds before its
# reproduction completed. Confirmation now waits for the verdict, a refutation
# stops the confirm, and a timeout lets the verifier outlive the close.


def _in_flight_verifier(run, flag_id: int):
    """Register a verifier task that is alive but will not finish by itself.

    The done-event must be registered by hand: ``Run.wait`` only waits on
    tasks that went through ``dispatch``, and a store-row task without one
    makes the confirm's wait a silent no-op.
    """
    import asyncio

    task = run.store.create_task(run.id, "verifier", "复核", "")
    run.store.mark_task_started(task.id)
    run._verifier_flags[task.id] = flag_id
    run._done[task.id] = asyncio.Event()
    return task


async def test_confirm_waits_for_a_verifier_that_lands_reproduced(
    run, cfg
) -> None:
    flag = _bind(run)
    verifier = _in_flight_verifier(run, flag.id)
    cfg.limits.confirm_verifier_wait_s = 10

    async def land_soon() -> None:
        import asyncio

        await asyncio.sleep(0.2)
        run.complete_task(verifier.id, verdict="reproduced", summary="VERDICT: reproduced")
        run._finish_task(verifier.id, "verifier", TaskStatus.DONE, "", {}, "")

    import asyncio

    landing = asyncio.create_task(land_soon())
    await run.confirm_flag(flag.id)
    await landing

    assert run.store.get_flag(flag.id).status == FlagStatus.CONFIRMED
    assert run.store.get_run(run.id).status == "solved"


async def test_a_refuted_verdict_during_the_wait_stops_the_confirm(run, cfg) -> None:
    flag = _bind(run)
    verifier = _in_flight_verifier(run, flag.id)
    cfg.limits.confirm_verifier_wait_s = 10

    import asyncio

    async def refute_soon() -> None:
        await asyncio.sleep(0.2)
        run.complete_task(verifier.id, verdict="refuted", summary="VERDICT: refuted")
        run._finish_task(verifier.id, "verifier", TaskStatus.DONE, "", {}, "")

    refuting = asyncio.create_task(refute_soon())
    await run.confirm_flag(flag.id)
    await refuting

    assert run.store.get_flag(flag.id).status == FlagStatus.REJECTED
    assert run.store.get_run(run.id).status != "solved"  # the confirm did not close it
    kinds = [e.kind for e in run.store.events_since(run.id)]
    assert "flag.confirm_refuted" in kinds

    # The operator, now informed, can confirm again — nothing is in flight.
    await run.confirm_flag(flag.id, note="看过驳回理由，坚持确认")
    assert run.store.get_flag(flag.id).status == FlagStatus.CONFIRMED


async def test_confirm_proceeds_after_the_wait_and_verifier_outlives_the_close(
    run, cfg
) -> None:
    flag = _bind(run)
    verifier = _in_flight_verifier(run, flag.id)
    cfg.limits.confirm_verifier_wait_s = 1  # verdict will never land in time

    await run.confirm_flag(flag.id)

    assert run.store.get_flag(flag.id).status == FlagStatus.CONFIRMED
    assert run.store.get_run(run.id).status == "solved"
    assert verifier.id in run._post_stop_ok  # exempt from the shutdown sweep

    # The verifier lands after the close; its verdict is still recorded, and
    # a reproduced result cannot downgrade the human's confirmation.
    run.complete_task(verifier.id, verdict="reproduced", summary="VERDICT: reproduced")
    run._finish_task(verifier.id, "verifier", TaskStatus.DONE, "", {}, "")
    assert run.store.get_flag(flag.id).status == FlagStatus.CONFIRMED
    verdicts = [e for e in run.store.events_since(run.id) if e.kind == "flag.verdict"]
    assert verdicts, "verdict must be recorded even after the run closed"


# ------------------------------------------------------------ zombie watchdog
# Run 82 sat `running` for half a day: its lead session went silent, every
# per-run clock failed to fire, and the ceiling path would have hung inside
# the very client that was dead. The watchdog now reaps silence directly and
# bounds the closing turn.


async def test_the_watchdog_reaps_a_silent_run(run) -> None:
    import time

    run._last_event_ts = time.time() - 1300
    assert await run._watchdog_tick() is True
    assert run.store.get_run(run.id).status == "failed"
    row = run.store.get_run(run.id)
    assert "静默" in (row.stop_reason or "")


async def test_the_ceiling_closing_turn_cannot_hang_the_close(run) -> None:
    import asyncio
    import time

    async def hung_stream(*args, **kwargs):  # the client is the thing that died
        await asyncio.Event().wait()

    run._stream = hung_stream  # type: ignore[method-assign]
    run._main_client = object()
    run._started = time.time() - 99999  # far past the wall clock

    result = await asyncio.wait_for(run._watchdog_tick(), timeout=10)
    assert result is True
    assert run.store.get_run(run.id).status == "failed"


async def test_the_lead_cannot_close_a_run_solved_by_itself(run) -> None:
    """finish_run(solved) is coerced to needs_input.

    Observed on the lcg_step experiment run: verifier reproduced, and the lead
    called finish_run(solved) — closing the run without the operator decision
    the docs promise ("reproduced → needs_input，等你确认"). A worker cannot
    declare victory, and neither can the lead; solved is the operator's word,
    spoken through confirm_flag.
    """
    flag = run.store.add_flag(run.id, None, "flag{x}", "s", "m", "high")
    assert flag is not None
    run.store.set_flag_status(flag.id, FlagStatus.VERIFIED)
    run.request_finish("solved", "验证器都通过了，我直接收尾")

    assert run._finish_status == "needs_input"
    assert run.store.get_run(run.id).status == "needs_input"
