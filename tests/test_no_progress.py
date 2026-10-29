"""Waste guards, and the capability they must not cost.

The first version of this guard counted tool calls since the last ``record_fact``
and stopped a worker at 40. That was wrong, and the database says so: the two
most expensive sessions made 225 and 157 tool calls, but **188 and 129 of them
were distinct** (16-18% repetition). They were unpacking and tracing an
obfuscated binary — real analysis — and simply recorded facts sparsely. Cutting
them at 40 calls would have thrown the work away.

So the signal changed. **Repetition is waste; silence is not.** The tests below
pin both halves of that: a loop gets stopped, and a long run of *distinct* work
does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.config import LimitsConfig, ModelConfig
from ctfagent.models import TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


@pytest.fixture()
def build_run(cfg, store_and_bus):
    store, bus = store_and_bus

    def build(backend, challenge_dir: Path):
        manager = RunManager(
            store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
        )
        return manager.prepare(store.add_challenge(load_challenge(challenge_dir)))

    return build


def _calls(cmds: list[str]) -> list[AgentEvent]:
    events = [AgentEvent("init", {"session_id": "sess"})]
    events += [
        AgentEvent("tool_use", {"id": f"t{i}", "tool": "Bash", "input": {"command": cmd}})
        for i, cmd in enumerate(cmds)
    ]
    return events


async def test_the_same_call_in_a_loop_is_stopped(cfg, fixtures_dir: Path, build_run) -> None:
    cfg.limits.repeat_call_limit = 4
    backend = ScriptedBackend(script={"reverse": [_calls(["dir"] * 10)]})
    run = build_run(backend, fixtures_dir / "easy_crypto")

    task = await run.dispatch("reverse", "trace the VM")
    await run.wait([task.id], timeout_s=20)

    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status == TaskStatus.EXHAUSTED
    assert "重复" in finished.error


async def test_deep_analysis_of_distinct_commands_is_never_stopped(
    cfg, fixtures_dir: Path, build_run
) -> None:
    """The capability this guard must not cost.

    #28 made 225 calls with 188 distinct operations and no facts along the way.
    A worker like that has to be allowed to finish.
    """
    cfg.limits.repeat_call_limit = 4
    cfg.limits.no_progress_tools = 0
    cmds = [f"objdump -d -M intel --start-address=0x{i:04x} bin.exe" for i in range(60)]
    backend = ScriptedBackend(script={"reverse": [_calls(cmds)]})
    run = build_run(backend, fixtures_dir / "easy_crypto")

    task = await run.dispatch("reverse", "trace the VM")
    await run.wait([task.id], timeout_s=20)

    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status != TaskStatus.EXHAUSTED


async def test_alternating_calls_are_not_treated_as_a_loop(
    cfg, fixtures_dir: Path, build_run
) -> None:
    """Only *consecutive* repeats count; A,B,A,B is still two commands."""
    cfg.limits.repeat_call_limit = 3
    cmds = ["dir"] * 5
    mixed: list[str] = []
    for c in cmds:
        mixed.extend([c, "type flag.txt"])
    backend = ScriptedBackend(script={"reverse": [_calls(mixed)]})
    run = build_run(backend, fixtures_dir / "easy_crypto")

    task = await run.dispatch("reverse", "trace the VM")
    await run.wait([task.id], timeout_s=20)

    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status != TaskStatus.EXHAUSTED


async def test_the_opt_in_idle_guard_still_works(cfg, fixtures_dir: Path, build_run) -> None:
    cfg.limits.no_progress_tools = 5
    cfg.limits.repeat_call_limit = 0
    cmds = [f"echo {i}" for i in range(10)]
    backend = ScriptedBackend(script={"reverse": [_calls(cmds)]})
    run = build_run(backend, fixtures_dir / "easy_crypto")

    task = await run.dispatch("reverse", "trace the VM")
    await run.wait([task.id], timeout_s=20)

    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status == TaskStatus.EXHAUSTED
    assert "没有产生任何新结论" in finished.error


@pytest.mark.parametrize("role", ["verifier", "writer"])
async def test_closing_roles_are_exempt_from_both_guards(
    cfg, fixtures_dir: Path, build_run, role: str
) -> None:
    cfg.limits.repeat_call_limit = 3
    cfg.limits.no_progress_tools = 3
    backend = ScriptedBackend(script={role: [_calls(["dir"] * 10)]})
    run = build_run(backend, fixtures_dir / "easy_crypto")

    task = await run.dispatch(role, "do the thing")
    await run.wait([task.id], timeout_s=20)

    finished = run.store.get_task(task.id)
    assert finished is not None
    assert finished.status != TaskStatus.EXHAUSTED


def test_the_idle_guard_is_off_by_default() -> None:
    """Capability first: distinct work is not waste."""
    assert LimitsConfig().no_progress_tools == 0
    assert LimitsConfig().repeat_call_limit > 0


def test_thinking_budget_is_opt_in_and_typed() -> None:
    assert ModelConfig().thinking == {}
    assert ModelConfig().thinking_for("reverse") is None

    cfg = ModelConfig(thinking={"triage": 0, "reverse": 8000, "web": "nonsense"})
    assert cfg.thinking_for("triage") == 0      # 0 means "disabled"
    assert cfg.thinking_for("reverse") == 8000
    assert cfg.thinking_for("web") is None      # junk is ignored, not passed on
    assert cfg.thinking_for("crypto") is None


def test_the_thinking_budget_reaches_the_session_options() -> None:
    from ctfagent.backend import ClaudeSDKBackend, SessionSpec
    from ctfagent.config import AppConfig

    app = AppConfig()
    app.models.thinking = {"reverse": 8000, "triage": 0}
    backend = ClaudeSDKBackend(app)
    spec = SessionSpec(
        role="reverse", prompt="x", cwd=Path("."), system_prompt="",
        model="deepseek-v4-pro", max_turns=5, max_budget_usd=1.0,
    )
    assert backend._options(spec).thinking == {"type": "enabled", "budget_tokens": 8000}

    spec.role = "triage"
    assert backend._options(spec).thinking == {"type": "disabled"}

    spec.role = "web"
    assert backend._options(spec).thinking is None
