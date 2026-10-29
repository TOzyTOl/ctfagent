"""Regression tests for the ways a run wasted money or trusted bad output.

Each test names the live symptom it prevents: a ceiling recorded as "done", a
blackboard that doubled every finding, artifact lists made of the skill library,
decoy strings promoted to flags, a tool call mistaken for a hang, and duplicate
verifier sessions for one value.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.models import FlagStatus, RunStatus, TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.tools import tool_specs
from ctfagent.worker_tools import WorkerTools
from ctfagent.workspace import SandboxStatus

FLAG = "flag{easy_rsa}"


@pytest.fixture()
def store_and_bus(cfg):
    store = Store(cfg.db_path)
    bus = EventBus(store)
    yield store, bus
    store.close()


def _manager(store, bus, cfg, backend=None) -> RunManager:
    return RunManager(
        store, bus, cfg, backend or ScriptedBackend(),
        SandboxStatus("none", False, "test"), cfg.skills_dir,
    )


async def test_a_ceiling_outranks_the_workers_own_done(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A worker that said "done" and then hit its budget is EXHAUSTED.

    Live symptom: task 16 called ``task_complete(done)`` and immediately hit its
    dollar ceiling. The explicit status won, so the task looked finished, got no
    hand-off, and its transcript could not be resumed — while its findings
    stopped mid-investigation.
    """
    store, bus = store_and_bus
    backend = ScriptedBackend()
    backend.script["reverse"] = [[
        AgentEvent(
            "result",
            {"subtype": "error_max_budget_usd", "is_error": True, "result": "",
             "session_id": "sess-1"},
            cost_usd=1.0,
        ),
    ]]
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    original = run.dispatch

    async def dispatch(role: str, objective: str, context: str = "", **kw):
        task = await original(role, objective, context, **kw)
        if role == "reverse":
            WorkerTools(run, task.id).task_complete(
                summary="got as far as the RC4 key", status="done"
            )
        return task

    run.dispatch = dispatch  # type: ignore[method-assign]
    task = await run.dispatch("reverse", "unpack", "")
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.EXHAUSTED
    handoff = [f for f in store.list_facts(run.id, task_id=task.id) if "撞上" in f.content]
    assert handoff, "no hand-off was recorded, so the next worker repeats the route"


def test_findings_do_not_duplicate_the_blackboard(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """``task_complete`` findings that repeat a recorded fact are dropped.

    Live symptom: 36 of 36 findings across three tasks were verbatim copies of
    facts the worker had already written. The copy also lost the ``basis``, so a
    claim the worker had marked ``inferred`` came back looking verified.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "reverse", "unpack", "")
    run.record_fact(
        task.id, "observation", "RC4 key is 48 bytes at 0x4b80e0",
        {"command": "python solve.py --key", "output": "key=12 34 ... (48 bytes) @0x4b80e0"},
        basis="observed",
    )

    run.complete_task(
        task.id,
        status="done",
        summary="done",
        findings=["RC4  key is 48 bytes at 0x4b80e0", "staged.rpt decrypts to a landing log"],
    )

    contents = [f.content for f in store.list_facts(run.id, task_id=task.id)]
    assert contents.count("RC4 key is 48 bytes at 0x4b80e0") == 1
    assert any("landing log" in c for c in contents)
    original = next(f for f in store.list_facts(run.id, task_id=task.id) if "0x4b80e0" in f.content)
    assert original.basis == "observed"


def test_a_paraphrased_restatement_is_dropped(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """Re-wording a fact is not new information.

    Live symptom: run 5 grew from 22 to 44 facts because ``task_complete``
    findings and the lead agent's notes restated earlier facts in different
    words. Verbatim comparison missed every one. Each duplicate is re-injected
    into every later worker prompt, so the waste compounds.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "forensics", "trace the beacon", "")
    first = run.record_fact(
        task.id,
        "observation",
        "journal.txt 给出三条硬规则: beacon 序号在捕获里(不是 hostinfo.seq); gdb 现跑的落地记录是错的, 要用 staged.rpt; pcap 里只能信 wire 数据",
        basis="observed",
    )

    same = run.record_fact(
        None,
        "insight",
        "journal.txt 三条规则: ①beacon 序号在捕获里, 不在 hostinfo.seq; ②gdb 现跑的落地记录与本次捕获不是同一次, 必须用 staged.rpt; ③pcap 里只能信 wire 数据",
    )
    assert same == first
    assert len(store.list_facts(run.id)) == 1

    # A restatement that carries a value the original did not is new work.
    detail = run.record_fact(
        task.id,
        "observation",
        "staged.rpt 的 296 字节密文在 0x4b80e0 处按 HZR1 头解析, 尾部 8 字节是明文 txid",
    )
    assert detail != first
    assert len(store.list_facts(run.id)) == 2


def test_a_restatement_that_upgrades_the_basis_is_kept(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Grounding is information: "inferred" later confirmed is a new record.

    Live symptom: workers mark guesses ``inferred`` and only confirm them after
    running the binary. Merging the confirmed copy into the guess would leave
    the board — and every later prompt — saying the claim was never verified.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "reverse", "find the key", "")
    run.record_fact(
        task.id, "insight", "the beacon uses a 48-byte RC4 key at 0x4b80e0", basis="inferred"
    )
    confirmed = run.record_fact(
        task.id, "insight", "the beacon uses a 48-byte RC4 key at 0x4b80e0",
        {"command": "./beacon --dump-key", "output": "rc4 key len=48 @0x4b80e0"},
        basis="observed",
    )
    facts = store.list_facts(run.id)
    assert len(facts) == 2
    assert facts[-1].id == confirmed
    assert facts[-1].basis == "observed"


def test_closing_an_unknown_is_never_merged_away(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """An answer that repeats an earlier fact still has to close its unknown.

    ``resolves`` is what closes the question on the digest; merging the answer
    into the fact it restates would leave the unknown open forever.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "crypto", "recover the key", "")
    run.record_fact(task.id, "observation", "AES key is 48 bytes at 0x4b80e0", basis="observed")
    unknown = run.record_fact(task.id, "unknown", "what is the AES key length at 0x4b80e0?", basis="inferred")

    answer = run.record_fact(
        task.id,
        "insight",
        "The AES key at 0x4b80e0 is 48 bytes long",
        basis="derived",
        resolves=unknown,
    )
    assert answer != unknown
    assert not run.open_unknowns()


def test_observed_without_evidence_is_filed_as_a_hypothesis(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """An `observed` claim with no command and no output is downgraded.

    Live symptom: a worker wrote "已确认 legacy-edge 只拦带 query 的请求" as
    ``observed`` from a handful of block/allow responses, without quoting any of
    them. The next worker read it as checked and built a payload on top of a
    rule that had never been tested that way. The prompt asks for the evidence;
    this makes the label honest when it does not arrive.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "web", "probe the edge", "")

    tools = WorkerTools(run, task.id)
    result = tools.record_fact(
        "已确认 legacy-edge 只拦带 query 的请求，不带 query 的路径直接放行",
        kind="insight",
        basis="observed",
        evidence={"note": "从几十次探测里看出来的"},
    )
    assert "warning" in result
    fact = next(f for f in store.list_facts(run.id) if f.id == result["fact_id"])
    assert fact.basis == "inferred"
    assert fact.evidence["declared_basis"] == "observed"

    # Command alone is enough for derived; observed also needs what it printed.
    derived = run.record_fact(
        task.id, "insight", "flag 可取 cat /flag", {"command": "cat /flag"}, basis="derived"
    )
    assert next(f for f in store.list_facts(run.id) if f.id == derived).basis == "derived"

    # Re-recording the same claim with the raw response is an upgrade, not a
    # restatement: the board must stop calling it a hypothesis.
    upgraded = tools.record_fact(
        "已确认 legacy-edge 只拦带 query 的请求，不带 query 的路径直接放行",
        kind="insight",
        basis="observed",
        evidence={
            "request": "GET /x/../shell.php?data=1 HTTP/1.1",
            "response": "HTTP/1.1 403 Forbidden",
        },
    )
    assert "warning" not in upgraded
    assert upgraded["fact_id"] != result["fact_id"]
    assert next(f for f in store.list_facts(run.id) if f.id == upgraded["fact_id"]).basis == "observed"


def test_blackboard_digest_carries_the_evidence(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A grounded fact shows the command and the raw result on the board.

    The digest is the only thing the next worker reads. Without the quoted
    request it can neither re-run the step nor tell a real 403 from a guess, so
    it either repeats the work or trusts it blindly.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    run.record_fact(
        None,
        "insight",
        "detail= 是列名位置注入，布尔盲注可拖库",
        {"request": "GET /api/internal/user?uid=1&detail=(id>0)", "response": "HTTP/1.1 200 OK"},
        basis="observed",
    )
    run.record_fact(
        None, "insight", "需要镜像运行时版本才能复现解析差异", basis="inferred"
    )
    digest = run.blackboard_digest()
    assert "证据·命令: GET /api/internal/user?uid=1&detail=(id>0)" in digest
    assert "证据·输出: HTTP/1.1 200 OK" in digest
    assert digest.count("证据·") == 2  # one command, one output; inferred gets none


def test_artifacts_exclude_the_vendored_skill_tree(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A task's artifact list is its own output, not the skill index.

    Live symptom: the list was the first 200 paths alphabetically, which were
    all ``.claude/skills/...`` — identical in every run. No worker's real files
    were ever listed.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    skills = run.workspace.work / ".claude" / "skills" / "reverse"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / "SKILL.md").write_text("x", encoding="utf-8")
    (run.workspace.work / "solve.py").write_text("print(1)", encoding="utf-8")

    listed = run._list_artifacts()
    assert "solve.py" in listed
    assert not any(name.startswith(".claude") for name in listed)


def test_a_decoy_mentioned_in_the_report_is_not_a_flag(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A value the report calls bait is not a candidate."""
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    )
    task = store.create_task(run.id, "crypto", "recover", "")
    report = (
        "The .decoy section contains flag{decoy_static_vm_path}, which is bait.\n"
        + ("filler " * 60)
        + "\nThe real answer is flag{easy_rsa}."
    )
    run._scan_for_flags(task.id, report)

    assert {f.value for f in store.list_flags(run.id)} == {FLAG}


class _QuietBackend:
    """One turn: a tool call, silence, then the tool returns."""

    def __init__(self, silence_s: float) -> None:
        self.silence_s = silence_s
        self.calls: list[Any] = []

    async def run(self, spec) -> AsyncIterator[AgentEvent]:
        self.calls.append(spec)
        yield AgentEvent("init", {"subtype": "task_started", "session_id": "sess-quiet"})
        yield AgentEvent("tool_use", {"tool": "PowerShell", "input": {}, "id": "call-1"})
        await asyncio.sleep(self.silence_s)
        yield AgentEvent("tool_result", {"id": "call-1", "is_error": False, "content": "ok"})
        yield AgentEvent(
            "result",
            {"subtype": "success", "is_error": False, "result": "", "session_id": "sess-quiet"},
        )

    def open_client(self, spec):  # pragma: no cover - not used by these tests
        raise AssertionError("no main session in these tests")


class _TalkThenHangBackend:
    """Says one line, then goes quiet forever: a task cancelled mid-work."""

    def __init__(self) -> None:
        self.spoke = asyncio.Event()

    async def run(self, spec) -> AsyncIterator[AgentEvent]:
        yield AgentEvent("init", {"subtype": "task_started", "session_id": "sess-talk"})
        yield AgentEvent("text", {"text": "我已用独立的 Python 实现复现出明文，接下来核对程序路径。"})
        self.spoke.set()
        await asyncio.sleep(60)
        yield AgentEvent("result", {"subtype": "success", "is_error": False, "result": ""})

    def open_client(self, spec):  # pragma: no cover - not used by these tests
        raise AssertionError("no main session in these tests")


async def test_silence_while_a_tool_runs_is_not_a_hang(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The stall clock must not fire while a tool is still executing.

    Live symptom: a verifier was killed at 300 s while its ``run_hook.sh``
    (timeout 420 s) was still running under WSL. The session was waiting, not
    hung, and the flag it was confirming stayed unverified until a second
    verifier was paid for.
    """
    cfg.limits.stall_timeout_s = 0.1
    cfg.limits.tool_wait_timeout_s = 2.0
    store, bus = store_and_bus
    backend = _QuietBackend(silence_s=0.4)
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    task = await run.dispatch("reverse", "run the hook", "")
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)

    assert store.get_task(task.id).status == TaskStatus.DONE


async def test_a_tool_that_really_hangs_is_still_cut_off(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The longer tool clock is a ceiling, not an exemption."""
    cfg.limits.stall_timeout_s = 0.1
    cfg.limits.tool_wait_timeout_s = 0.3
    store, bus = store_and_bus
    backend = _QuietBackend(silence_s=2.0)
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    task = await run.dispatch("reverse", "run the hook", "")
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.EXHAUSTED
    assert "工具调用" in finished.error
    # The session id arrived before the hang, so the work is still resumable.
    assert finished.session_id == "sess-quiet"


class _BackgroundBackend:
    """A tool the CLI moved to the background, then a long silent wait."""

    def __init__(self, silence_s: float) -> None:
        self.silence_s = silence_s

    async def run(self, spec) -> AsyncIterator[AgentEvent]:
        yield AgentEvent(
            "init",
            {"subtype": "task_started", "task_id": "bg-1", "is_backgrounded": True,
             "session_id": "sess-bg"},
        )
        yield AgentEvent("tool_use", {"tool": "PowerShell", "input": {}, "id": "call-1"})
        # The tool call returned *immediately* with "running in background".
        # From here the session emits nothing until the job finishes.
        yield AgentEvent(
            "tool_result",
            {"id": "call-1", "is_error": False,
             "content": "Command running in background with ID: bg-1"},
        )
        await asyncio.sleep(self.silence_s)
        yield AgentEvent(
            "init", {"subtype": "task_notification", "task_id": "bg-1", "status": "completed"}
        )
        yield AgentEvent(
            "result",
            {"subtype": "success", "is_error": False, "result": "", "session_id": "sess-bg"},
        )

    def open_client(self, spec):  # pragma: no cover - not used by these tests
        raise AssertionError("no main session in these tests")


async def test_silence_while_a_command_runs_in_the_background_is_not_a_hang(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A backgrounded command's silence is work, not a stuck model.

    Live symptom (run 15): four workers were cut off at exactly 305 s of
    silence while their own background cracks were still printing progress.
    The CLI had already backgrounded the command and returned its tool_result,
    so ``inflight`` was empty and the orchestrator used the 300 s model clock.
    Each kill forced a resume that re-paid for the analysis already done.
    """
    cfg.limits.stall_timeout_s = 0.1
    cfg.limits.tool_wait_timeout_s = 2.0
    store, bus = store_and_bus
    backend = _BackgroundBackend(silence_s=0.4)
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    task = await run.dispatch("reverse", "crack the archive", "")
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.DONE, finished.error


async def test_a_background_task_that_really_hangs_is_still_cut_off(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Tracking background work widens the clock; it does not remove it.

    While the job runs, ``live`` says so — otherwise the lead sees the same
    ``silent_s`` for a 40-minute extraction and a dead session and hedges with
    a second worker, which is the one response that is always wasted.
    """
    cfg.limits.stall_timeout_s = 0.1
    cfg.limits.tool_wait_timeout_s = 0.3
    store, bus = store_and_bus
    backend = _BackgroundBackend(silence_s=2.0)
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    task = await run.dispatch("reverse", "crack the archive", "")
    seen: dict[str, Any] = {}
    for _ in range(200):
        if task.id in run._live:
            seen = run.live_progress(task.id)
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)

    assert seen.get("background") == 1, seen

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.EXHAUSTED
    # The message names what was being waited on, so the operator can tell a
    # dead background job from a dead model.
    assert "后台任务" in finished.error
    assert "bg-1" in finished.error


def test_background_task_tracking_follows_the_cli_signals(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The tracked set mirrors the CLI's task lifecycle exactly.

    ``background_tasks_changed`` is a level signal with REPLACE semantics: a
    merge would keep a finished task in the set forever and hold every later
    silence on the long clock.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    live: set[str] = set()
    track = run._track_background

    track({"subtype": "task_started", "task_id": "a"}, live)
    assert live == {"a"}
    # A foreground task is already covered by ``inflight``.
    track({"subtype": "task_started", "task_id": "fg", "is_backgrounded": False}, live)
    assert live == {"a"}

    # Level signal: swap, do not merge.
    track({"subtype": "background_tasks_changed",
           "tasks": [{"task_id": "b"}, {"task_id": "c"}]}, live)
    assert live == {"b", "c"}
    track({"subtype": "background_tasks_changed", "tasks": []}, live)
    assert live == set()

    # Terminal statuses remove a task; non-terminal ones do not.
    track({"subtype": "task_started", "task_id": "d"}, live)
    track({"subtype": "task_updated", "task_id": "d", "status": "running"}, live)
    assert live == {"d"}
    track({"subtype": "task_updated", "task_id": "d", "status": "killed"}, live)
    assert live == set()
    track({"subtype": "task_started", "task_id": "e"}, live)
    track({"subtype": "task_notification", "task_id": "e", "status": "completed"}, live)
    assert live == set()

    # A foreground task moved off the foreground mid-run joins the set.
    track({"subtype": "task_updated", "task_id": "g", "is_backgrounded": True}, live)
    assert live == {"g"}


async def test_a_running_verifier_blocks_a_second_one(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """One value, one verifier — including one dispatched by hand.

    Live symptom: run 3 spawned three verifier sessions for one flag. The first
    was killed as stalled, the lead dispatched one by hand, and the auto path
    then dispatched a third because the hand-dispatched one was not bound to a
    flag and the guard only looked at its own bindings.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    )
    hand = store.create_task(run.id, "verifier", f"verify {FLAG} by hand", "")
    store.mark_task_started(hand.id)

    result = await run.propose_flag(FLAG, method="Fermat", source="worker")

    assert result["verifier_task_id"] == hand.id
    verifiers = [t for t in store.list_tasks(run.id) if t.role == "verifier"]
    assert len(verifiers) == 1
    assert store.list_flags(run.id)[0].status == FlagStatus.CANDIDATE


async def test_a_verifier_cut_off_by_the_run_clock_keeps_what_it_said(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A cancelled task is STOPPED with its own last words, not FAILED and empty.

    Live symptom: run 5 hit its wall clock while verifier #26 was mid-check. It
    had already written "我已用独立的 Python 实现复现出明文", but the cancellation
    path stored ``{}`` as its result and the run reported "验证任务没有给出结论".
    The verifier's whole purpose was discarded by a bookkeeping shortcut.
    """
    store, bus = store_and_bus
    backend = _TalkThenHangBackend()
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    task = await run.dispatch("verifier", "verify the flag", "")
    for _ in range(200):
        if backend.spoke.is_set():
            break
        await asyncio.sleep(0.01)
    await run.shutdown(RunStatus.STOPPED, "clock", "test")
    # Cancellation is delivered at the task's next await point; shutdown returns
    # before that. Let it unwind before reading the row.
    for _ in range(200):
        if store.get_task(task.id).result:
            break
        await asyncio.sleep(0.01)

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.STOPPED
    assert "独立" in str(finished.result.get("summary", "")), finished.result


async def test_wait_drops_findings_the_board_already_has(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """``wait_tasks`` must not re-send what ``record_fact`` already put on the board.

    Live symptom: task #23's result was 11115 bytes, 8237 of them findings that
    were verbatim blackboard entries; task #24 carried 200 artifact names.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "forensics", "trace", "")
    store.finish_task(
        task.id,
        TaskStatus.DONE,
        {
            "summary": "done",
            "findings": ["f" * 500] * 20,
            "artifacts": [f"a{i}.txt" for i in range(200)],
        },
    )

    payload = (await run.wait([task.id], timeout_s=0))[0]
    result = payload["result"]
    assert "findings" not in result
    assert "20 条 findings" in result["findings_note"]
    assert len(result["artifacts"]) == 30
    assert "另有 170 个文件" in result["artifacts_note"]
    # The full record is still reachable when the lead actually needs it.
    assert len(run.task_result(task.id)["result"]["findings"]) == 20


async def test_a_running_task_reports_live_progress(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A running task's cost books at the end, so ``live`` must show it is alive.

    Live symptom: every running task read ``cost_usd 0.0``; the lead could not
    tell a working reverse worker from a dead one and paid for a hedge worker in
    the same direction.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "reverse", "unpack", "")
    store.mark_task_started(task.id)
    for i in range(3):
        run._record_event(
            AgentEvent("tool_use", {"tool": "PowerShell", "input": {}, "id": f"c{i}"}),
            task.id,
            "reverse",
        )

    payload = (await run.wait([task.id], timeout_s=0))[0]
    assert payload["live"]["tools"] == 3
    assert payload["live"]["last_tool"] == "PowerShell"
    assert payload["live"]["silent_s"] >= 0


async def test_the_clock_closing_the_run_still_writes_a_terminal_status(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A run closed by its own watchdog must end in a terminal state.

    Live symptom: run 5's 90-minute clock fired, the lead answered with
    ``finish_run``, and ``shutdown`` cancelled the watchdog from inside the
    watchdog's own task. The CancelledError landed on the first ``await`` in
    ``shutdown``, so ``set_run_status`` and ``run.finished`` never ran: the
    database kept ``needs_input`` with an empty ``finished_at`` and no close
    event, and the operator had no way to tell the run had hit its clock.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    # Stand in for the watchdog task: shutdown is called from inside it.
    async def close_from_the_watchdog() -> None:
        await run.shutdown(RunStatus.FAILED, "运行超过时限", "时间用尽")

    run._watchdog = asyncio.create_task(close_from_the_watchdog())
    for _ in range(200):
        if store.get_run(run.id).status == RunStatus.FAILED:
            break
        await asyncio.sleep(0.01)

    closed = store.get_run(run.id)
    assert closed.status == RunStatus.FAILED
    assert closed.finished_at, "the run closed without a terminal timestamp"
    assert closed.stop_reason == "时间用尽"
    kinds = [e.kind for e in store.events_since(run.id)]
    assert "run.finished" in kinds, kinds


async def test_every_tool_that_wakes_the_lead_carries_the_clock(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The lead has no clock of its own, so the tools must carry one.

    Live symptom: at 09:52 the lead wrote "we've used maybe ~1200s" (2100s had
    passed) and at 10:12 it read "$3.40/$6 used" — its own session's budget —
    as the run's. It then waited 46 more minutes and dispatched the verifier 8
    minutes before the run clock fired.
    """
    store, bus = store_and_bus
    cfg.limits.run_timeout_s = 600
    cfg.limits.run_budget_usd = 12.0
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    run.cost_usd = 3.5
    task = store.create_task(run.id, "reverse", "unpack", "")

    waited = await run.tools.wait_tasks([task.id], timeout_s=1)
    clock = waited["clock"]
    assert 0 <= clock["elapsed_s"] < 60
    assert 540 < clock["remaining_s"] <= 600
    assert clock["run_cost_usd"] == 3.5
    assert clock["budget_left_usd"] == 8.5
    assert clock["workers_allowed"] == cfg.limits.workers_per_run

    listed = run.tools.list_tasks()
    assert listed["clock"]["run_cost_usd"] == 3.5
    assert listed["clock"]["remaining_s"] > 0


async def test_a_worker_that_hit_its_turn_ceiling_gets_a_wrap_up_turn(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Turns are cheap; losing the conclusion is not.

    Live symptom: reverse #22 and #24 in run 5 both stopped at exactly 151 turns.
    #24 had already found the HZBC VM and the MAC check, but the session ended
    mid-sentence, so neither the run's card nor the next worker inherited it.
    A turn ceiling says nothing about money, so the transcript can afford a few
    more turns to write down what it established.
    """
    store, bus = store_and_bus
    cfg.limits.task_wrapup_turns = 4
    cfg.limits.run_budget_usd = 10.0
    backend = ScriptedBackend()
    backend.script["reverse"] = [
        [AgentEvent(
            "result",
            {"subtype": "error_max_turns", "is_error": True, "result": "",
             "session_id": "sess-turns"},
            cost_usd=1.0, turns=150,
        )],
        [AgentEvent("text", {"text": "结论：VM 在 0x4b80e0，MAC 校验 16 字节"})],
    ]
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    task = await run.dispatch("reverse", "unpack", "")
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)

    calls = [c for c in backend.calls if c.role == "reverse"]
    assert len(calls) == 2, "the ceiling did not get a wrap-up turn"
    assert calls[-1].resume_session_id == "sess-turns"
    assert calls[-1].max_turns == 4
    # The resumed invocation is charged from the transcript's running total.
    assert calls[-1].max_budget_usd > 1.0

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.EXHAUSTED
    assert "VM 在 0x4b80e0" in finished.result.get("summary", "")
    handoff = [f for f in store.list_facts(run.id, task_id=task.id) if "撞上" in f.content]
    assert handoff, "the hand-off must still be recorded"


async def test_a_worker_out_of_money_gets_no_wrap_up_turn(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A budget ceiling means there is nothing left to spend on a courtesy turn."""
    store, bus = store_and_bus
    backend = ScriptedBackend()
    backend.script["reverse"] = [[
        AgentEvent(
            "result",
            {"subtype": "error_max_budget_usd", "is_error": True, "result": "",
             "session_id": "sess-broke"},
            cost_usd=1.0,
        ),
    ]]
    run = _manager(store, bus, cfg, backend).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )

    task = await run.dispatch("reverse", "unpack", "")
    for _ in range(200):
        if store.get_task(task.id).terminal:
            break
        await asyncio.sleep(0.02)

    assert len([c for c in backend.calls if c.role == "reverse"]) == 1
    assert store.get_task(task.id).status == TaskStatus.EXHAUSTED


async def test_a_second_worker_in_a_live_direction_is_refused(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Dispatching into a live direction is refused, not merely warned about.

    Live symptom: run 5 dispatched reverse #24 at 10:07:55 while reverse #22 was
    still running. Both burned the 150-turn ceiling; together they were $10.43,
    58% of the run. Neither used ``resume_task_id``. Run 15 repeated it four
    times in one run: the warning came back and the lead dispatched anyway, and
    each of the four transcripts re-read the same 1312-layer archive. A warning
    is advisory; a refusal is the only version the model cannot read past.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    live = store.create_task(run.id, "reverse", "unpack", "")
    store.mark_task_started(live.id)
    spent = store.create_task(run.id, "reverse", "earlier attempt", "")
    store.finish_task(spent.id, TaskStatus.EXHAUSTED, {}, "", session_id="sess-old")

    refused = await run.tools.dispatch_task("reverse", "unpack the payload", "")
    assert "error" in refused
    assert refused["live_tasks"] == [live.id]
    assert f"#{live.id}" in refused["error"]
    # The way out is named: wait, resume, or force.
    assert any("wait_tasks" in o for o in refused["options"])
    assert any("force=true" in o for o in refused["options"])
    # Nothing was spent on the refused call.
    assert len(store.list_tasks(run.id)) == 2

    # A different direction is unaffected; the guard is per role.
    other = await run.tools.dispatch_task("crypto", "attack the keystream", "")
    assert "task_id" in other

    # Resuming is exactly what the refusal asks for, so it goes through and is
    # not nagged about: the exhausted transcript is already being reused.
    resumed = await run.tools.dispatch_task(
        "reverse", "continue", "", resume_task_id=spent.id
    )
    assert "task_id" in resumed
    assert not resumed.get("warnings")

    # A genuinely different angle is still possible, but only by saying so.
    forced = await run.tools.dispatch_task("reverse", "try the other key order", "", force=True)
    assert "task_id" in forced


def test_the_cli_background_payload_reaches_the_orchestrator() -> None:
    """``_normalise`` must carry the task identity the stall clock needs.

    The SDK hands ``background_tasks_changed`` through as a generic
    ``SystemMessage``; before this, ``_normalise`` kept only subtype/session and
    dropped ``tasks``, ``task_id`` and ``patch.status``. Every run-15 stall was
    therefore classified as model silence.
    """
    from claude_agent_sdk import SystemMessage

    from ctfagent.backend import ClaudeSDKBackend

    def only(subtype: str, data: dict) -> dict:
        events = ClaudeSDKBackend._normalise(SystemMessage(subtype=subtype, data=data))
        assert len(events) == 1 and events[0].kind == "init"
        return events[0].payload

    started = only(
        "task_started",
        {"subtype": "task_started", "task_id": "bg-1", "task_type": "local_bash",
         "description": "unzip 1312 layers", "is_backgrounded": True,
         "session_id": "s"},
    )
    assert started["task_id"] == "bg-1" and started["is_backgrounded"] is True

    # A foreground task says so explicitly; ``False`` must survive, not be
    # dropped as falsy.
    foreground = only(
        "task_started",
        {"subtype": "task_started", "task_id": "fg-1", "is_backgrounded": False,
         "session_id": "s"},
    )
    assert foreground["is_backgrounded"] is False

    # The status lives in the patch, not at the top level.
    updated = only(
        "task_updated",
        {"subtype": "task_updated", "task_id": "bg-1", "patch": {"status": "killed"},
         "session_id": "s"},
    )
    assert updated["status"] == "killed"

    changed = only(
        "background_tasks_changed",
        {"subtype": "background_tasks_changed", "session_id": "s",
         "tasks": [
             {"task_id": "bg-1", "task_type": "local_bash", "description": "crack",
              "awaited": False},
             {"task_id": "bg-2", "task_type": "local_bash", "description": "dump",
              "awaited": True},
         ]},
    )
    assert [t["task_id"] for t in changed["tasks"]] == ["bg-1", "bg-2"]
    assert changed["tasks"][1]["awaited"] is True


def test_a_negative_without_scope_and_control_is_marked_unverified(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A dead end is only as wide as the space it actually searched.

    Live symptom (run 18, "Guanju"): a worker swept "the seed's low 24 bits"
    and recorded ``observed`` "2^24: no solution" with a real command and its
    real output. Both checks in ``grounding_gap`` passed, so the board and the
    case library showed it as a settled dead end — but the key was not the
    seed, it was eight PCG draws concatenated, and the very next run of the
    same challenge was one sweep away from the answer that fact said did not
    exist. Evidence was not missing; its scope was unstated.
    """
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "crypto", "sweep the seed", "")
    tools = WorkerTools(run, task.id)

    vague = tools.record_fact(
        "seed 低 24 位自由区间扫完无解，C1 不是这条路径产生的",
        kind="dead_end",
        basis="observed",
        evidence={"command": "python bsgs.py guanju.exe", "output": "HITS: []"},
    )
    assert "warning" in vague
    fact = next(f for f in store.list_facts(run.id) if f.id == vague["fact_id"])
    # Still recorded — where someone looked is worth keeping.
    assert fact.basis == "observed"
    assert fact.evidence["negative_gap"]

    # And the board says so, instead of presenting it as a closed route.
    digest = run.blackboard_digest()
    assert "未验证的否定" in digest

    # Scoped + controlled is a real dead end: no warning, no marker.
    scoped = run.record_fact(
        task.id,
        "dead_end",
        "对 k ∈ [0,2^24) 全枚举，k·G≠C1（k 取 seed 本身这一假设已排除）",
        {
            "command": "python bsgs.py guanju.exe",
            "output": "HITS: []",
            "scope": "low 24 bits of the seed, k = seed; 2^24 exhaustive",
            "control": "planted k=0x123, recovered",
        },
        basis="observed",
    )
    good = next(f for f in store.list_facts(run.id) if f.id == scoped)
    assert not good.evidence.get("negative_gap")
    assert "未验证" not in run.blackboard_digest().split(f"#{scoped}")[1][:40]


# ------------------------------------------------------------------- lead speed
# Measured on the live database: the lead called wait_tasks 43–65 times per
# run, each call a full model turn on a growing transcript, and the wait
# itself slept to its timeout even when a task had already finished (gather
# semantics). Both costs are pure overhead — no capability trades.


async def test_wait_wakes_the_moment_one_task_lands(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """One finished task must end the wait immediately, not at the timeout.

    The old gather-all semantics slept the whole timeout while an already
    finished task waited to be noticed — reaction latency was locked to the
    polling boundary, which is what made the lead poll at all.
    """
    import time

    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    first = store.create_task(run.id, "reverse", "a", "")
    second = store.create_task(run.id, "crypto", "b", "")
    run._done[first.id] = asyncio.Event()
    run._done[second.id] = asyncio.Event()

    async def land_first() -> None:
        await asyncio.sleep(0.1)
        store.finish_task(first.id, "done", {"summary": "landed"}, "")
        run._done[first.id].set()

    started = time.monotonic()
    landing = asyncio.create_task(land_first())
    out = await asyncio.wait_for(run.wait([first.id, second.id], timeout_s=30), timeout=10)
    await landing
    assert time.monotonic() - started < 5  # woke at ~0.1s, not at 30s
    by_id = {t["task_id"]: t for t in out}
    assert by_id[first.id]["terminal"] is True
    assert by_id[second.id]["terminal"] is False


async def test_wait_tasks_blocks_long_and_clamps_under_the_cli_ceiling(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """One call should span a real stretch of work, never exceed the CLI kill."""
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    captured: dict[str, Any] = {}

    async def fake_wait(ids, timeout_s):
        captured["timeout_s"] = timeout_s
        return []

    run.wait = fake_wait  # type: ignore[method-assign]
    await run.tools.wait_tasks([1], timeout_s=9999)
    assert captured["timeout_s"] == 480
    await run.tools.wait_tasks([1])
    assert captured["timeout_s"] == 240

    spec = next(s for s in tool_specs() if s["name"] == "wait_tasks")
    timeout = spec["input_schema"]["properties"]["timeout_s"]
    assert timeout["default"] == 240 and timeout["maximum"] == 480


def test_session_env_raises_the_mcp_tool_ceiling(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """wait_tasks may only block for minutes if the CLI lets the call live."""
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    assert run._session_env()["MCP_TOOL_TIMEOUT"] == "600000"


def test_the_lead_is_told_to_block_rather_than_poll(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The mechanism is only as cheap as the lead's use of it."""
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    prompt = run._main_spec().system_prompt
    assert "wakes the instant any awaited task finishes" in prompt


# ------------------------------------------------------------- orphan reaping
# Live evidence (run 82): a server restart killed worker sessions without
# finishing their rows; the revived lead read the ghost "running" row, assumed
# it was stuck, force-dispatched a duplicate over it, and the two workers
# collided. Reopen now reaps every non-terminal row the process does not own.


async def test_reopen_reaps_restart_orphans_and_tells_the_lead(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    store, bus = store_and_bus
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    ghost = store.create_task(run.id, "reverse", "unpack", "")
    store.mark_task_started(ghost.id)  # running row from a dead process
    live = store.create_task(run.id, "crypto", "sweep", "")
    store.mark_task_started(live.id)
    run._tasks[live.id] = asyncio.create_task(asyncio.sleep(3600))  # owned by this process

    await run.reopen()

    assert store.get_task(ghost.id).status == TaskStatus.STOPPED
    assert "遗留" in (store.get_task(ghost.id).error or "")
    assert store.get_task(live.id).status == "running"  # a live task is not a ghost
    kinds = [e.kind for e in store.events_since(run.id)]
    warnings = [e for e in store.events_since(run.id) if e.kind == "run.warning"]
    assert warnings and "不要为同样的目标重新派工" in warnings[0].payload["message"]
    run._tasks[live.id].cancel()
