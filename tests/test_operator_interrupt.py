"""An operator message cuts the lead's in-flight turn.

A lead turn can be open for minutes — it dispatches and then sits inside
``wait_tasks``. Waiting for it to end means the operator's question is answered
only after the work it asked about is already over. The message now interrupts
the lead so it answers immediately.

The risky half is that the CLI reports an interrupt exactly like a dead session
(``error_during_execution``). Misreading that would close the run the first
time the operator typed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

from ctfagent.backend import AgentEvent
from ctfagent.challenge import load_challenge
from ctfagent.models import RunStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


class _InterruptBackend:
    """One long kickoff turn, then ordinary turns answering what it was asked."""

    def __init__(self, abort_subtype: str = "error_during_execution") -> None:
        self.abort_subtype = abort_subtype
        self.client_texts: list[str] = []
        self.interrupts = 0
        self._client: Any = None

    def run(self, spec) -> AsyncIterator[AgentEvent]:  # pragma: no cover - workers unused
        raise AssertionError("no workers expected")

    def open_client(self, spec) -> "_InterruptClient":
        self._client = _InterruptClient(self, spec)
        return self._client


class _InterruptClient:
    def __init__(self, backend: _InterruptBackend, spec: Any) -> None:
        self.backend = backend
        self.spec = spec
        self.interrupted = asyncio.Event()

    async def __aenter__(self) -> "_InterruptClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def interrupt(self) -> None:
        self.backend.interrupts += 1
        self.interrupted.set()

    def send(self, text: str) -> AsyncIterator[AgentEvent]:
        return self._send(text)

    async def _send(self, text: str) -> AsyncIterator[AgentEvent]:
        first = not self.backend.client_texts
        self.backend.client_texts.append(text)
        if first:
            # A partial reply, as the stream delivers it: deltas are live-only,
            # so nothing durable should survive a turn that was cut.
            yield AgentEvent("text_delta", {"text": "先说一半"})
            await self.interrupted.wait()
            yield AgentEvent(
                "result",
                {
                    "subtype": self.backend.abort_subtype,
                    "is_error": True,
                    "result": "Request interrupted by user",
                    "session_id": "sess-main",
                    "terminal_reason": "aborted_tools",
                },
            )
            return
        yield AgentEvent("text", {"text": f"收到：{text}"})
        yield AgentEvent(
            "result",
            {"subtype": "success", "is_error": False, "result": "", "session_id": "sess-main"},
        )


class _LateInterruptBackend(_InterruptBackend):
    """The interrupt arrives after its own turn ended, so the CLI cuts the next.

    Observed shape of the race: the operator's POST lands in the same tick the
    turn's result does. The message must not be swallowed by a turn that never
    read it.
    """

    def open_client(self, spec) -> "_LateInterruptClient":
        self._client = _LateInterruptClient(self, spec)
        return self._client


class _LateInterruptClient(_InterruptClient):
    async def _send(self, text: str) -> AsyncIterator[AgentEvent]:
        calls = len(self.backend.client_texts)
        self.backend.client_texts.append(text)
        if calls == 0:
            # Kickoff: finishes on its own, but only after the interrupt was
            # already sent — which is what makes the CLI apply it late.
            await self.interrupted.wait()
            yield AgentEvent(
                "result",
                {"subtype": "success", "is_error": False, "result": "", "session_id": "sess-main"},
            )
            return
        if calls == 1:
            # The operator's message: cut before the lead read a single token.
            yield AgentEvent(
                "result",
                {
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "session_id": "sess-main",
                    "terminal_reason": "aborted_streaming",
                },
            )
            return
        yield AgentEvent("text", {"text": f"收到：{text}"})
        yield AgentEvent(
            "result",
            {"subtype": "success", "is_error": False, "result": "", "session_id": "sess-main"},
        )


class _NoReasonBackend(_InterruptBackend):
    """An interrupt result shaped the way a CLI that omits the reason sends it."""

    def open_client(self, spec) -> "_NoReasonClient":
        self._client = _NoReasonClient(self, spec)
        return self._client


class _NoReasonClient(_InterruptClient):
    async def _send(self, text: str) -> AsyncIterator[AgentEvent]:
        if not self.backend.client_texts:
            self.backend.client_texts.append(text)
            await self.interrupted.wait()
            yield AgentEvent(
                "result",
                {
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "session_id": "sess-main",
                },
            )
            return
        self.backend.client_texts.append(text)
        yield AgentEvent("text", {"text": f"收到：{text}"})
        yield AgentEvent(
            "result",
            {"subtype": "success", "is_error": False, "result": "", "session_id": "sess-main"},
        )


def _manager(store, bus, cfg, backend) -> RunManager:
    return RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )


async def _wait(predicate, attempts: int = 400) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_an_operator_message_answers_before_the_turn_ends(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    store, bus = store_and_bus
    backend = _InterruptBackend()
    manager = _manager(store, bus, cfg, backend)
    run = await manager.start(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    assert await run.wait_until_listening(timeout=5)
    # The kickoff turn is open and will only end when it is interrupted.
    assert await _wait(lambda: run._lead_turn_active)

    await run.chat("先别派任务，说说你判断的方向")

    assert await _wait(lambda: "先别派任务，说说你判断的方向" in backend.client_texts)
    assert backend.interrupts == 1
    assert await _wait(
        lambda: any(
            m["role"] == "assistant" and "收到：" in m["content"]
            for m in store.list_messages(run.id)
        )
    )
    # An interrupted turn is not a dead session: the run keeps serving turns.
    assert not run.closed
    assert store.get_run(run.id).status == RunStatus.RUNNING
    # The CLI's interrupt notice is not something the lead said.
    assert not any(
        "Request interrupted" in m["content"] for m in store.list_messages(run.id)
    )
    # Whatever had been written when the turn was cut is not a message either.
    assert not any(
        m["role"] == "assistant" and "先说一半" in m["content"]
        for m in store.list_messages(run.id)
    )
    assert any(e.kind == "main.interrupted" for e in store.recent_events(run.id))
    await run.shutdown(RunStatus.STOPPED, "test", "test")


async def test_the_cli_without_terminal_reason_is_still_read_as_an_interrupt(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Older CLIs omit ``terminal_reason``; the interrupt must still be ours.

    The probe against the real CLI reported ``error_during_execution`` with
    ``terminal_reason: aborted_streaming``; the orchestrator also has to work
    where that field never arrives.
    """
    store, bus = store_and_bus
    backend = _NoReasonBackend()
    manager = _manager(store, bus, cfg, backend)
    run = await manager.start(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    assert await run.wait_until_listening(timeout=5)
    assert await _wait(lambda: run._lead_turn_active)

    await run.chat("现在什么情况？")

    assert await _wait(lambda: backend.interrupts == 1)
    assert await _wait(
        lambda: any(
            m["role"] == "assistant" and "收到：" in m["content"]
            for m in store.list_messages(run.id)
        )
    )
    assert not run.closed
    await run.shutdown(RunStatus.STOPPED, "test", "test")


async def test_a_spontaneous_execution_error_still_ends_the_session(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """No interrupt was sent, so the same subtype really is a dead session."""

    class _Dying(_InterruptBackend):
        def open_client(self, spec):
            return _DyingClient(self, spec)

    class _DyingClient(_InterruptClient):
        async def _send(self, text: str) -> AsyncIterator[AgentEvent]:
            self.backend.client_texts.append(text)
            yield AgentEvent(
                "result",
                {
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "CLI crashed",
                    "session_id": "sess-main",
                },
            )

    store, bus = store_and_bus
    backend = _Dying()
    manager = _manager(store, bus, cfg, backend)
    run = await manager.start(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    assert await _wait(lambda: run.closed)
    assert backend.interrupts == 0
    assert store.get_run(run.id).status == RunStatus.FAILED


async def test_a_message_cut_before_it_was_read_is_not_swallowed(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The interrupt lands on the wrong turn; the operator's words still arrive."""
    store, bus = store_and_bus
    backend = _LateInterruptBackend()
    manager = _manager(store, bus, cfg, backend)
    run = await manager.start(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    assert await run.wait_until_listening(timeout=5)
    assert await _wait(lambda: run._lead_turn_active)

    await run.chat("附件里那个 pdb 要不要看？")

    # Sent twice: cut, then re-queued and answered.
    assert await _wait(
        lambda: backend.client_texts.count("附件里那个 pdb 要不要看？") == 2
    )
    assert await _wait(
        lambda: any(
            m["role"] == "assistant" and "收到：" in m["content"]
            for m in store.list_messages(run.id)
        )
    )
    assert not run.closed
    await run.shutdown(RunStatus.STOPPED, "test", "test")
