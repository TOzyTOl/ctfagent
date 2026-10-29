"""The WSL pwn runtime: the workflow that makes native exploitation possible.

The host is Windows and cannot execute ELF, so pwn without a documented Linux
runtime is structural: every worker rediscovers that /mnt/d carries no exec
bits (observed live — run 5's workers improvised a ~/work copy on their own,
paying turns for it). The block is injected only into the roles that exploit
binaries, and only when the tools it names are actually inside the distro:
promising an environment that is not there sends the worker into the mount
trap with no way out.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ctfagent.workspace as workspace
from ctfagent.backend import ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


@pytest.fixture()
def run(cfg, tmp_path: Path, fixtures_dir: Path, store_and_bus, monkeypatch):
    """A run whose sandbox reports a distro, with the toolkit probe faked.

    The real probe shells out to ``wsl.exe`` — machine-specific, and a test
    must not depend on this host having a distro or what is inside it.
    """
    cfg.skills_dir = tmp_path / "skills"
    cfg.skills_dir.mkdir()
    store, bus = store_and_bus
    monkeypatch.setattr(
        workspace, "wsl_pwn_toolkit", lambda distro: "pwntools、patchelf、ROPgadget、GEF"
    )
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(),
        SandboxStatus("none", False, "test", wsl="Ubuntu-20.04"), cfg.skills_dir,
    )
    return manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))


def _spec(run, role: str):
    task = run.store.create_task(run.id, role, "obj", "ctx")
    return run._worker_spec(task)


def test_the_block_names_the_two_rules_that_cost_runs(run) -> None:
    block = SandboxStatus("none", False, "t", wsl="Ubuntu-20.04").pwn_guidance()
    assert "Ubuntu-20.04" in block and "pwntools" in block
    # the exec-bit trap, and the artifacts-stay-in-workspace rule the
    # verifier's independent reproduction depends on
    assert "/mnt/d" in block and "chmod" in block
    assert "复现" in block


def test_a_distro_without_tools_says_nothing(monkeypatch) -> None:
    """Silence beats a promised environment that is not there."""
    monkeypatch.setattr(workspace, "wsl_pwn_toolkit", lambda distro: "")
    status = SandboxStatus("none", False, "t", wsl="Ubuntu-20.04")
    assert status.pwn_guidance() == ""


def test_the_pwn_worker_gets_the_workflow_the_web_worker_does_not(run) -> None:
    assert "patchelf" in _spec(run, "pwn").system_prompt
    # context is not free: a web worker carries nothing of it
    assert "patchelf" not in _spec(run, "web").system_prompt


def test_the_verifier_gets_it_only_for_a_pwn_challenge(run) -> None:
    # this run's challenge is crypto: the verifier must not carry pwn context
    assert "patchelf" not in _spec(run, "verifier").system_prompt

    # same run, category re-pointed to pwn: re-running the exploit needs the
    # same WSL workflow the claimant had
    previous = run.challenge.category
    run.challenge.category = "pwn"
    try:
        assert "patchelf" in _spec(run, "verifier").system_prompt
    finally:
        run.challenge.category = previous
