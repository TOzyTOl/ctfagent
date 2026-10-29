"""End-to-end orchestration with no LLM: the scripted backend plays every agent.

The point of this file is to prove the *loop* — dispatch, structured write-back,
blackboard aggregation, independent verification, human confirmation, writeup —
independently of whether a model behaves. The driver below is a small stand-in
for the main agent: it calls the real orchestrator tools exactly as the main
agent would, and the assertions are about what the orchestrator did with them.

The main session is a ``ScriptedBackend`` client; its kickoff turn runs the
driver, so the driver's ``dispatch_task`` calls go through the real code path
(tool -> Run.dispatch -> worker session -> WorkerTools -> store).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.models import FlagStatus, RunStatus, TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.worker_tools import WorkerTools
from ctfagent.workspace import SandboxStatus

FLAG = "flag{easy_rsa}"


class Harness:
    """Plays the main agent and the workers.

    - ``driver``: the main session's turn. Dispatches and waits through the real
      ``OrchestratorTools``.
    - ``worker``: a wrapper around ``Run.dispatch`` that attaches the worker-side
      write-back for each role, so the scripted worker session produces exactly
      the structured result a real one would.
    """

    def __init__(self) -> None:
        self.run: Any = None
        self.dispatched: list[dict[str, Any]] = []
        self.flag: dict[str, Any] | None = None
        self.waited: list[dict[str, Any]] = []

    # -- main agent ------------------------------------------------------
    async def driver(self, spec, text: str) -> list[AgentEvent]:
        if spec.role != "main" or self.run is None or not text.startswith("Start working"):
            return []
        tools = self.run.tools

        crypto = await tools.dispatch_task(
            role="crypto",
            objective="Recover the RSA plaintext from params.txt",
            context="p is just above 2^61 and q differs by less than 10^6 (Fermat).",
        )
        self.dispatched.append(crypto)
        waited = await tools.wait_tasks([crypto["task_id"]], timeout_s=10)
        self.waited = waited["tasks"]

        self.flag = await tools.propose_flag(
            value=FLAG,
            method="Fermat factorisation, d = e^-1 mod phi, m = c^d mod n.",
            source="work/solve.py",
            confidence="high",
        )
        verifier_id = self.flag["verifier_task_id"]
        assert verifier_id, self.flag
        self.waited += (await tools.wait_tasks([verifier_id], timeout_s=10))["tasks"]
        return [AgentEvent("text", {"text": "dispatched, verified, awaiting operator"})]

    # -- workers ---------------------------------------------------------
    async def dispatch(self, original, role: str, objective: str, context: str = "", **kw):
        task = await original(role, objective, context, **kw)
        tools = WorkerTools(self.run, task.id)
        if role == "crypto":
            tools.record_fact(
                "n factors as 2305843009213706309 * 2305843009214681609 (Fermat)",
                kind="insight",
                basis="observed",
                evidence={
                    "command": "python solve.py",
                    "output": "p=2305843009213706309 q=2305843009214681609",
                },
            )
            await tools.submit_flag(
                FLAG,
                method="d = e^-1 mod phi; m = c^d mod n; to_bytes -> b'flag{easy_rsa}'",
                source="work/solve.py",
                confidence="high",
            )
            tools.task_complete(
                summary="Recovered the plaintext: flag{easy_rsa}",
                status="done",
                findings=["RSA modulus factored with Fermat's method"],
                artifacts=["solve.py"],
            )
        elif role == "verifier":
            tools.task_complete(
                summary="VERDICT: reproduced\nFactored n independently; re-derived b'flag{easy_rsa}'.",
                status="done",
                findings=["independent re-derivation matched"],
            )
        elif role == "writer":
            (self.run.workspace.work / "writeup.md").write_text(
                "# easy-crypto\n\nFermat factorisation of n.\n", encoding="utf-8"
            )
            tools.task_complete(summary="wrote writeup.md", status="done")
        return task


@pytest.fixture()
def store_and_bus(cfg):
    store = Store(cfg.db_path)
    bus = EventBus(store)
    yield store, bus
    store.close()


async def test_unknowns_are_tracked_and_closed(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """An open question is a blackboard item until a fact closes it.

    This is the REA-derived rule that matters most for a stuck reverse task:
    "I don't know X" must survive as dispatchable state instead of turning into
    a confident guess at the end of a worker's turn.
    """
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    unknown_id = run.record_fact(
        None,
        "unknown",
        "check() 里那张 256 字节常量表是 XOR 密钥还是置换表？",
        {"why_it_matters": "决定要逆向还是直接爆破", "would_resolve": "跑一遍 check 并观察索引"},
        "inferred",
    )
    assert [f.id for f in run.open_unknowns()] == [unknown_id]
    digest = run.blackboard_digest()
    assert "## 未解问题" in digest and f"#{unknown_id}" in digest
    assert "推测" in digest  # basis is shown next to the kind

    run.record_fact(
        None,
        "insight",
        "常量表是 XOR 密钥，逐字节异或即可还原。",
        {"command": "python solve.py"},
        "derived",
        resolves=unknown_id,
    )
    assert run.open_unknowns() == []
    digest = run.blackboard_digest()
    assert "## 未解问题" not in digest
    assert f"→ 解决 #{unknown_id}" in digest

    # An unknown value is not allowed to masquerade as a grounding level.
    fact_id = run.record_fact(None, "insight", "随手记", {}, "certain")
    fact = next(f for f in store.list_facts(run.id) if f.id == fact_id)
    assert fact.basis == ""


async def test_end_to_end_offline(cfg, fixtures_dir: Path, store_and_bus) -> None:
    store, bus = store_and_bus
    harness = Harness()
    backend = ScriptedBackend()
    backend.driver = harness.driver
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )

    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))

    # Prepare first so the harness can wrap dispatch before the main loop starts.
    run = manager.prepare(challenge_id)
    harness.run = run
    original = run.dispatch
    run.dispatch = lambda role, objective, context="", **kw: harness.dispatch(  # type: ignore[method-assign]
        original, role, objective, context
    )
    await run.start()

    for _ in range(400):
        if harness.flag is not None:
            break
        await asyncio.sleep(0.05)
    assert harness.flag is not None, "main agent never proposed a flag"
    assert harness.waited and harness.waited[0]["status"] == TaskStatus.DONE
    flag_id = harness.flag["flag_id"]

    # The verifier reproduced it: VERIFIED, and the run paused for a human.
    for _ in range(400):
        if store.get_flag(flag_id).status == FlagStatus.VERIFIED:
            break
        await asyncio.sleep(0.05)
    assert store.get_flag(flag_id).status == FlagStatus.VERIFIED
    assert store.get_run(run.id).status == RunStatus.NEEDS_INPUT

    # Structured write-back landed on the blackboard.
    facts = store.list_facts(run.id)
    assert any("Fermat" in f.content for f in facts)

    # The operator confirms. Writing the report is a separate, explicit act
    # (``writeup_on_confirm`` defaults to false), so this test asks for it —
    # the point here is the full loop, not the default.
    await run.confirm_flag(flag_id, note="checked by hand", writeup=True)
    assert store.get_flag(flag_id).status == FlagStatus.CONFIRMED
    assert store.get_run(run.id).status == RunStatus.SOLVED
    writeup = run.workspace.work / "writeup.md"
    assert writeup.exists() and "Fermat" in writeup.read_text(encoding="utf-8")

    # Every transition left an ordered event.
    events = store.recent_events(run.id, limit=2000)
    kinds = {e.kind for e in events}
    for expected in (
        "run.started",
        "task.dispatched",
        "task.result",
        "task.finished",
        "flag.proposed",
        "flag.verdict",
        "flag.confirmed",
        "run.finished",
    ):
        assert expected in kinds, sorted(kinds)
    assert [e.id for e in events] == sorted(e.id for e in events)


async def test_flag_refuted_by_verifier(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """A refuted candidate is marked rejected and the run does not pause."""
    store, bus = store_and_bus

    class Refute(Harness):
        async def dispatch(self, original, role: str, objective: str, context: str = "", **kw):
            task = await original(role, objective, context, **kw)
            tools = WorkerTools(self.run, task.id)
            if role == "verifier":
                tools.task_complete(
                    summary="VERDICT: refuted\nThe solve script hardcodes the flag; nothing is decrypted.",
                    status="done",
                )
            return task

    harness = Refute()
    backend = ScriptedBackend()
    backend.driver = harness.driver
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = manager.prepare(challenge_id)
    harness.run = run
    original = run.dispatch
    run.dispatch = lambda role, objective, context="", **kw: harness.dispatch(  # type: ignore[method-assign]
        original, role, objective, context
    )
    await run.start()

    for _ in range(400):
        if harness.flag is not None:
            break
        await asyncio.sleep(0.05)
    assert harness.flag is not None
    flag_id = harness.flag["flag_id"]
    for _ in range(400):
        if store.get_flag(flag_id).status == FlagStatus.REJECTED:
            break
        await asyncio.sleep(0.05)
    assert store.get_flag(flag_id).status == FlagStatus.REJECTED
    assert store.get_run(run.id).status != RunStatus.NEEDS_INPUT
    await run.shutdown(RunStatus.FAILED, "no flag", "test cleanup")


async def test_stop_cancels_workers(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """An operator stop ends the run and leaves no task running."""
    store, bus = store_and_bus

    class Hanging(Harness):
        async def dispatch(self, original, role: str, objective: str, context: str = "", **kw):
            task = await original(role, objective, context, **kw)
            await asyncio.sleep(60)  # never finishes on its own
            return task

    harness = Hanging()
    backend = ScriptedBackend()
    backend.driver = harness.driver
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = manager.prepare(challenge_id)
    harness.run = run
    original = run.dispatch
    run.dispatch = lambda role, objective, context="", **kw: harness.dispatch(  # type: ignore[method-assign]
        original, role, objective, context
    )
    await run.start()

    for _ in range(100):
        if store.list_tasks(run.id):
            break
        await asyncio.sleep(0.05)
    await run.shutdown(RunStatus.STOPPED, "operator stop", "operator stop")
    assert store.get_run(run.id).status == RunStatus.STOPPED
    tasks = store.list_tasks(run.id)
    assert tasks
    assert all(t.status != TaskStatus.RUNNING for t in tasks)


async def test_streamed_text_becomes_the_stored_message(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """Token deltas are live-only; the finished message is the full reply.

    The result string the CLI reports is only the final text block, so storing
    that alone would make a reload show less than the live page did.
    """
    store, bus = store_and_bus
    backend = ScriptedBackend()
    backend.script["main"] = [[
        AgentEvent("text_delta", {"text": "先看附件，"}),
        AgentEvent("text_delta", {"text": "再决定方向。"}),
        AgentEvent("text", {"text": "先看附件，再决定方向。"}),
        AgentEvent(
            "result",
            {"subtype": "success", "is_error": False, "result": "再决定方向。", "session_id": ""},
        ),
    ]]
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    await run.start()
    await asyncio.sleep(0.2)
    await run.shutdown(RunStatus.STOPPED, "test", "test")

    assistant = [m for m in store.list_messages(run.id) if m["role"] == "assistant"]
    assert [m["content"] for m in assistant] == ["先看附件，再决定方向。"]

    # Deltas reached the bus (they are what the page grows a bubble from) but
    # were never written to the events table.
    kinds = [e.kind for e in store.recent_events(run.id, limit=500)]
    assert "text_delta" not in kinds
    assert "main.message" in kinds


def test_re_tools_go_to_the_roles_that_need_them(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """The `re` server is per-role, and the verifier inherits it for RE categories.

    A web worker with ten binary tools in its context is worse at its job; a
    verifier without them can only re-run the claimant's script, which is not
    independent reproduction.
    """
    from types import SimpleNamespace

    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_rev")))
    task = SimpleNamespace(role="", objective="x", context="", id=1)

    def spec_for(role: str):
        task.role = role
        return run._worker_spec(task)

    for role in ("triage", "reverse", "pwn", "malware", "forensics"):
        spec = spec_for(role)
        assert "re" in spec.mcp_servers, role
        assert "mcp__re__re_inspect" in spec.allowed_tools, role
        assert "mcp__re__re_inspect" not in spec_for("web").allowed_tools
    assert "re" not in spec_for("web").mcp_servers
    assert "re" not in spec_for("crypto").mcp_servers

    # easy-rev is a reverse challenge, so its verifier gets the same primitives.
    verifier = spec_for("verifier")
    assert "re" in verifier.mcp_servers
    assert "Binary reconnaissance" in verifier.system_prompt


def test_a_pasted_url_still_gets_the_proxy_bypass(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """``remote`` is empty for a pasted URL, so the host must come from the text.

    Live symptom: the operator pasted the target into the description, every
    worker kept the operator's proxy, and a challenge port answered the proxy's
    own ``502`` — which workers then recorded as the target being down.
    """
    from types import SimpleNamespace

    from ctfagent.models import Challenge

    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    challenge = Challenge(
        id=0,
        name="web thing",
        category="web",
        description="打 http://hbc2.haobachang.com:46931/ 这题，找到 flag",
    )
    run = manager.prepare(store.add_challenge(challenge))
    task = SimpleNamespace(role="web", objective="x", context="", id=1)
    spec = run._worker_spec(task)

    assert spec.extra_env["NO_PROXY"].startswith("hbc2.haobachang.com")
    assert spec.extra_env["no_proxy"] == spec.extra_env["NO_PROXY"]
    assert "hbc2.haobachang.com" in spec.system_prompt
    assert "NO_PROXY" in spec.system_prompt
    # The remote-service discipline used to key off ``remote`` alone, so a
    # pasted URL never got it.
    assert "Reaching the target, not the proxy" in spec.system_prompt


def test_a_local_challenge_keeps_the_operator_proxy(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    from types import SimpleNamespace

    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    task = SimpleNamespace(role="crypto", objective="x", context="", id=1)
    spec = run._worker_spec(task)

    assert "NO_PROXY" not in spec.extra_env
    assert "Reaching the target, not the proxy" not in spec.system_prompt
    assert "PowerShell 5.1" in spec.system_prompt


def test_a_worker_resolves_python_from_the_project_venv(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A worker's `python` has to be the interpreter the packages went into.

    Workers inherit the server's PATH. Running the server from ``.venv`` does
    not put ``.venv/Scripts`` on PATH, so a worker's bare ``python`` resolved to
    whatever the operator's shell had — on this host a system interpreter
    without pwntools, z3 or gmpy2 — while ``ctfagent doctor`` reported the venv.
    The two never met, and a reversing worker that needed z3 could not import it.
    """
    import os
    import sys

    from types import SimpleNamespace

    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    task = SimpleNamespace(role="crypto", objective="x", context="", id=1)
    spec = run._worker_spec(task)

    first = spec.extra_env["PATH"].split(os.pathsep)[0]
    assert Path(first) == Path(sys.executable).parent


def test_a_verifier_negative_sample_is_not_a_flag(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """Quoting a wrong input in a verdict must not create or verify a flag.

    Live symptom: a verifier reported ``VERDICT: reproduced`` and listed the
    negative controls it ran, among them a one-character-off near-miss. The
    report scan turned those into candidates, and the verdict was applied to
    the most recent candidate because the verifier was dispatched by hand and
    had no flag binding — so a wrong string ended up marked verified.
    """
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    good = store.add_flag(run.id, None, FLAG, "worker", "Fermat", "high")
    assert good is not None
    task = store.create_task(run.id, "verifier", "verify", "")

    summary = (
        "VERDICT: reproduced\n"
        f"printf '{FLAG}\n' | ./chall  ->  Correct!\n"
        "printf 'flag{wrong}\n' | ./chall  ->  Wrong!\n"
        "printf 'flag{easy_rsa!}\n' | ./chall  ->  Wrong!\n"
    )
    run._finish_task(task.id, "verifier", TaskStatus.DONE, "", {"result": summary}, "")

    values = {f.value for f in store.list_flags(run.id)}
    assert values == {FLAG}, f"negative samples became flags: {values}"
    # Unbound verifier: the verdict is reported but applied to nothing.
    assert store.get_flag(good.id).status == FlagStatus.CANDIDATE


def test_a_bound_verifier_verdict_still_applies(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """The guard against guessing must not stop the normal path from working."""
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    flag = store.add_flag(run.id, None, FLAG, "worker", "Fermat", "high")
    assert flag is not None
    task = store.create_task(run.id, "verifier", "verify", "")
    run._verifier_flags[task.id] = flag.id

    run._finish_task(task.id, "verifier", TaskStatus.DONE, "", {"result": "VERDICT: reproduced\nok"}, "")
    assert store.get_flag(flag.id).status == FlagStatus.VERIFIED


async def test_a_spent_lead_closes_the_run(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """A lead whose session hit its ceiling must end the run, not wait forever.

    Live symptom: the main session hit ``error_max_budget_usd``, every later
    turn returned the same error instantly, and the run sat in ``running``
    with no ``run.finished`` event — $14 against a $5 budget.
    """
    store, bus = store_and_bus
    backend = ScriptedBackend()
    backend.script["main"] = [[
        AgentEvent(
            "result",
            {"subtype": "error_max_budget_usd", "is_error": True, "result": "",
             "session_id": "sess-main"},
            cost_usd=2.0,
        ),
    ]]
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    await run.start()

    for _ in range(200):
        if store.get_run(run.id).status != RunStatus.RUNNING:
            break
        await asyncio.sleep(0.02)
    assert store.get_run(run.id).status == RunStatus.FAILED
    assert "预算" in store.get_run(run.id).summary
    kinds = [e.kind for e in store.recent_events(run.id, limit=500)]
    assert "run.finished" in kinds
    # It stopped after the first turn instead of asking a dead session again.
    assert backend.client_turns.get("main", 0) == 1


async def test_a_task_that_hit_its_ceiling_can_be_resumed(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """An exhausted worker hands off, and the follow-up continues its transcript.

    Live cost of not doing this: four reverse tasks each burned their full
    ceiling from a clean transcript, rediscovering the same ground. The
    session id is what makes the hand-off cheap, so the follow-up must carry it
    — and the ceiling must be raised by what that transcript already spent,
    because the CLI counts prior spend against a resumed invocation's budget.
    """
    store, bus = store_and_bus
    cfg.limits.run_budget_usd = 10.0
    backend = ScriptedBackend()
    backend.script["reverse"] = [[
        AgentEvent(
            "result",
            {"subtype": "error_max_budget_usd", "is_error": True, "result": "",
             "session_id": "sess-exhausted"},
            cost_usd=1.0,
        ),
    ]]
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_rev")))

    first = await run.dispatch("reverse", "unpack and trace", "")
    for _ in range(200):
        if store.get_task(first.id).terminal:
            break
        await asyncio.sleep(0.02)
    assert store.get_task(first.id).status == TaskStatus.EXHAUSTED
    assert store.get_task(first.id).session_id == "sess-exhausted"
    handoff = [f for f in store.list_facts(run.id, task_id=first.id) if f.kind == "observation"]
    assert handoff and "resume_task_id" in handoff[0].content

    second = await run.dispatch("reverse", "continue", "", resume_task_id=first.id)
    for _ in range(200):
        if store.get_task(second.id).terminal:
            break
        await asyncio.sleep(0.02)
    resumed = [c for c in backend.calls if c.role == "reverse"]
    assert resumed[-1].resume_session_id == "sess-exhausted"
    # The prior $1.00 is inside the CLI's counter, so the ceiling has to include it.
    assert resumed[-1].max_budget_usd > 1.0
    assert "continuing an earlier session" in resumed[-1].prompt


async def test_no_session_is_spawned_once_the_run_is_out_of_money(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A dispatch after the run budget is gone ends as exhausted, not as a crash.

    The CLI rejects a zero ceiling and exits 1, which the orchestrator would
    record as a failure. Reporting it as budget exhaustion is both true and
    what makes the hand-off (and the resume path) apply.
    """
    store, bus = store_and_bus
    backend = ScriptedBackend()
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_rev")))
    run.cost_usd = cfg.limits.run_budget_usd

    task = await run.dispatch("reverse", "anything", "")
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)
    assert store.get_task(task.id).status == TaskStatus.EXHAUSTED
    assert backend.calls == [], "a session was spawned with no budget left"


async def test_only_an_exhausted_task_is_resumed(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """A task that reached a conclusion must not be replayed.

    Resuming a done task would reload its transcript to arrive at the same
    answer — paying twice for one result.
    """
    store, bus = store_and_bus
    backend = ScriptedBackend()
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_rev")))

    done = store.create_task(run.id, "reverse", "finished", "")
    store.finish_task(done.id, TaskStatus.DONE, {}, "", session_id="sess-done")

    task = await run.dispatch("reverse", "continue", "", resume_task_id=done.id)
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)
    assert backend.calls[-1].resume_session_id == ""
    ignored = [f for f in store.list_facts(run.id, task_id=task.id) if "续跑请求被忽略" in f.content]
    assert ignored, "the ignored resume was not recorded"


def test_the_prompts_open_with_a_calibrated_sample_not_a_survey(
    cfg, fixtures_dir: Path
) -> None:
    """The three lessons of run 15, where the agent can still read them.

    Run 15 spent 6.5 h on a 1312-layer nested ZIP. Its post-mortem named four
    causes, and three of them are prompt-shaped: the attack tool was pointed at
    the real target before it was ever calibrated on a sample with a known
    answer, the hypothesis space was walked one guess at a time, and long jobs
    were started in the foreground. Prompts are the only place the agent can
    still be told; the orchestrator's guards cover the rest.
    """
    from ctfagent import prompts

    kickoff = prompts.main_kickoff("challenge text", "/work", "/work/attachments")
    # The opening objective is a working sample, and it comes before triage.
    assert kickoff.index("minimal end-to-end sample") < kickoff.index("triage")
    assert "poll" in kickoff.lower()

    lead = prompts.main_system_prompt(1, "challenge text", ["ctf-misc"])
    assert "One live task per direction" in lead
    assert "resume_task_id" in lead
    assert "Enumerate the hypotheses once" in lead

    worker = prompts.worker_system_prompt(
        "misc", "ctf-misc", "challenge text", "", "PowerShell 5.1",
    )
    assert "Calibrate the tool" in worker
    assert "hypothesis space" in worker
    assert "background" in worker


def test_an_empty_skill_library_is_reported_when_the_run_is_prepared(
    cfg, fixtures_dir: Path, store_and_bus, tmp_path: Path
) -> None:
    """A run with no skills must say so, not start quietly without them.

    The skill library is the technique index every worker leans on. When the
    vendored tree lost its files, the runs kept starting and nothing surfaced:
    the operators found out from a transcript, days later.
    """
    store, bus = store_and_bus
    empty = tmp_path / "empty-skills"
    empty.mkdir()
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), empty
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    warnings = [e for e in store.recent_events(run.id) if e.kind == "run.warning"]
    assert warnings, "preparing a run with zero skills must emit run.warning"
    assert "技能库为空" in warnings[0].payload["warning"]


def test_a_populated_skill_library_prepares_without_a_warning(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    assert not [e for e in store.recent_events(run.id) if e.kind == "run.warning"]


def test_a_library_missing_one_role_skill_is_named(
    cfg, fixtures_dir: Path, store_and_bus, tmp_path: Path
) -> None:
    """A partly-stocked library is the quiet failure, and it must be named.

    ``run.warning`` used to fire only when *nothing* installed, which misses the
    likelier case: ten skills present, ``ctf-pwn`` gone. The pwn worker is then
    started with an allowlist naming a skill that does not exist and works from
    memory, while the operator sees a healthy run.
    """
    store, bus = store_and_bus
    partial = tmp_path / "partial-skills"
    partial.mkdir()
    for name in ("solve-challenge", "ctf-web", "ctf-reverse"):
        (partial / name).mkdir()
        (partial / name / "SKILL.md").write_text(
            f"---\nname: {name}\n---\n", encoding="utf-8"
        )
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), partial
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    warnings = [e for e in store.recent_events(run.id) if e.kind == "run.warning"]
    assert warnings, "a library missing role skills must emit run.warning"
    missing = warnings[0].payload["missing"]
    assert "ctf-pwn" in missing
    assert "ctf-web" not in missing
    assert "角色技能缺失" in warnings[0].payload["warning"]
