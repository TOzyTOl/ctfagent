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
from ctfagent.models import FlagStatus
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
