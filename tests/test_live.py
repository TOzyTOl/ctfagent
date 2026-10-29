"""Live end-to-end run against the real Claude Agent SDK and a real endpoint.

Skipped unless ``CTFAGENT_LIVE=1``. It costs money and takes minutes; run it
before a competition, or after upgrading the SDK:

    $env:CTFAGENT_LIVE="1"
    python -m pytest tests/test_live.py -q -s

It solves the `easy_crypto` fixture end to end and asserts the whole loop:
dispatch -> worker solves -> flag proposed -> independent verifier reproduces ->
run pauses for the operator -> confirmation -> writeup -> run solved.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ctfagent.backend import ClaudeSDKBackend
from ctfagent.challenge import load_challenge
from ctfagent.config import AppConfig
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.models import FlagStatus, RunStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import probe_sandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("CTFAGENT_LIVE") != "1",
    reason="set CTFAGENT_LIVE=1 to run against a real model endpoint",
)


@pytest.mark.live
async def test_live_solves_easy_crypto(tmp_path: Path, fixtures_dir: Path) -> None:
    cfg = AppConfig(
        data_dir=tmp_path / "workspaces",
        db_path=tmp_path / "live.db",
        challenges_dir=tmp_path / "challenges",
        skills_dir=Path(__file__).resolve().parent.parent / "plugins" / "ctf-skills" / "skills",
    )
    cfg.limits.max_sessions = 4
    cfg.limits.workers_per_run = 2
    cfg.limits.run_timeout_s = 900
    cfg.limits.run_budget_usd = float(os.environ.get("CTFAGENT_LIVE_BUDGET_USD", "1.5"))
    cfg.limits.task_max_turns = 40
    cfg.limits.stall_timeout_s = 300
    cfg.ensure_dirs()

    store = Store(cfg.db_path)
    bus = EventBus(store)
    manager = RunManager(
        store, bus, cfg, ClaudeSDKBackend(cfg), probe_sandbox(cfg), cfg.skills_dir
    )
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = await manager.start(challenge_id)

    flag = None
    for _ in range(1200):  # up to 10 minutes
        flags = [f for f in store.list_flags(run.id) if f.status in (FlagStatus.VERIFIED, FlagStatus.CANDIDATE)]
        if flags:
            flag = flags[-1]
            break
        await asyncio.sleep(0.5)

    assert flag is not None, "no flag candidate was proposed"
    assert flag.value == "flag{easy_rsa}", flag.value

    # Wait for the verifier's verdict.
    for _ in range(600):
        if store.get_flag(flag.id).status == FlagStatus.VERIFIED:
            break
        await asyncio.sleep(0.5)
    assert store.get_flag(flag.id).status == FlagStatus.VERIFIED
    assert store.get_run(run.id).status == RunStatus.NEEDS_INPUT

    # A human confirms; the writeup lands at the run root. Asked for explicitly:
    # the default is to close on confirmation and write the report on request.
    await run.confirm_flag(flag.id, note="live test", writeup=True)
    assert store.get_run(run.id).status == RunStatus.SOLVED
    writeup = run.workspace.root / "writeup.md"
    assert writeup.exists(), list(run.workspace.root.rglob("*"))
    assert "flag{easy_rsa}" in writeup.read_text(encoding="utf-8")

    await manager.stop_all()
    store.close()
