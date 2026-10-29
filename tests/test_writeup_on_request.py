"""Confirming a flag must not start a writer.

A competition is a queue of challenges, and the writeup is the one piece of work
that can be done after the event. Starting a writer on confirmation spends a
session, a minute or two of wall clock and a slice of the run budget on
something nobody asked for — while the next challenge waits.

The report is still reachable: ``Run.write_report()``, the run page's 写报告
button, or ``ctfagent writeup <run>``, against the same finished run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.backend import ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.config import LimitsConfig
from ctfagent.models import FlagStatus, RunStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


@pytest.fixture()
def run(cfg, fixtures_dir: Path, store_and_bus):
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    return manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))


def _flag(run, value: str = "flag{confirmed}"):
    flag = run.store.add_flag(run.id, None, value, "work/solve.py", "m", "high")
    assert flag is not None
    return flag


def _writers(run):
    return [t for t in run.store.list_tasks(run.id) if t.role == "writer"]


def test_writeup_is_off_by_default() -> None:
    assert LimitsConfig().writeup_on_confirm is False


async def test_confirming_closes_the_run_without_a_writer(run) -> None:
    flag = _flag(run)
    await run.confirm_flag(flag.id)

    assert run.store.get_flag(flag.id).status == FlagStatus.CONFIRMED
    assert _writers(run) == []
    assert run.store.get_run(run.id).status == RunStatus.SOLVED
    assert run.closed is True


async def test_confirming_with_writeup_true_still_writes_the_report(run) -> None:
    """The old behaviour stays available for anyone who wants it."""
    flag = _flag(run)
    await run.confirm_flag(flag.id, writeup=True)

    assert len(_writers(run)) == 1
    assert run.store.get_run(run.id).status == RunStatus.SOLVED


async def test_write_report_refuses_when_no_flag_is_confirmed(run) -> None:
    _flag(run)  # still a candidate
    out = await run.write_report()
    assert "error" in out
    assert "确认" in out["error"]


async def test_write_report_dispatches_a_writer_on_a_closed_run(run) -> None:
    flag = _flag(run)
    await run.confirm_flag(flag.id)
    assert _writers(run) == []

    out = await run.write_report()

    assert "error" not in out
    assert len(_writers(run)) == 1
    assert out["path"].endswith("writeup.md")


async def test_write_report_grants_its_own_budget(run) -> None:
    """A run that closed at its dollar ceiling still has to be able to write."""
    flag = _flag(run)
    await run.confirm_flag(flag.id)
    # Pretend the run spent its whole allowance.
    run.cost_usd = run.cfg.limits.run_budget_usd

    out = await run.write_report()

    assert "error" not in out
    assert len(_writers(run)) == 1


def test_pocs_are_shown_as_raw_packets_not_curl() -> None:
    """The operator replays PoCs in Burp, so a curl command is work they redo.

    Both surfaces carry the rule: the report the writer produces, and what the
    lead says while explaining an approach in chat. Workers are deliberately
    left alone — during solving, whichever form is fastest wins.
    """
    from ctfagent import prompts

    report = prompts.writer_prompt("ch", "flag{x}", "board", "tasks")
    assert "原始请求包" in report
    assert "```http" in report
    # The example has to survive the f-string: single braces in the rendered text.
    assert '{"template":"${T(java.lang.Runtime)}","params":{}}' in report
    # Non-HTTP directions must not be forced into packet form.
    assert "逆向、密码、取证、pwn" in report

    lead = prompts.main_system_prompt(1, "challenge text", ["ctf-web"])
    assert "原始请求包" in lead
    assert "```http" in lead
