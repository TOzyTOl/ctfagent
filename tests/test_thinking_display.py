"""The lead's thinking is visible while it happens, and folded by default.

Three things are tested here:

- the orchestrator must fan ``thinking_delta`` out live (it is a token stream,
  never a row) and keep the completed ``thinking`` block as an event, because
  the block is what a reload re-renders;
- the page must seed the panel from those stored blocks, and only from the
  lead's — a worker's reasoning belongs to the worker's task, not to the
  conversation the operator is having with the lead;
- the prompt must ask for Chinese reasoning, since the panel puts the lead's
  thinking on screen next to Chinese replies. An English chain of thought here
  is visible to the operator, so it is part of the prompt's contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from ctfagent import prompts
from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.config import AppConfig
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.models import RunStatus
from ctfagent.orchestrator import RunManager
from ctfagent.server import create_app
from ctfagent.workspace import SandboxStatus


def test_the_lead_is_asked_to_think_in_chinese() -> None:
    """The panel shows reasoning verbatim; it has to be readable Chinese.

    The rule is stated twice on purpose: the system prompt is long and
    English, and a trailing language note alone loses to that context (seen
    live — the lead reasoned in English with the note in place). The opening
    section is what the model reads first and weighs most.
    """
    system = prompts.main_system_prompt(7, "challenge: test", ["ctf-reverse"])
    assert "# 输出语言" in system
    assert system.index("# 输出语言") < system.index("# How you work")
    assert "思考过程" in system and "简体中文" in system
    assert "# 语言" in system and "用简体中文思考" in system


def _run_with_thinking(store: Store, fixtures_dir: Path) -> int:
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    store.add_event(
        run.id,
        "agent.event",
        {"role": "main", "task_id": None, "kind": "thinking", "text": "先看附件里的 pdb。"},
    )
    store.add_event(
        run.id,
        "agent.event",
        {
            "role": "reverse",
            "task_id": 7,
            "kind": "thinking",
            "text": "worker 的推理不该出现在主对话里",
        },
        task_id=7,
    )
    return run.id


def test_the_page_seeds_the_panel_from_the_leads_thinking(cfg: AppConfig, fixtures_dir: Path) -> None:
    store = Store(cfg.db_path)
    try:
        run_id = _run_with_thinking(store, fixtures_dir)
    finally:
        store.close()

    app = create_app(cfg, scripted=True)
    with TestClient(app) as client:
        page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    # The rail summarizes a run with its latest line, so the worker's text does
    # appear somewhere on the page; what matters is that it is not in the panel.
    panel = page.text[page.text.index('id="think-panel"') : page.text.index('id="live-strip"')]
    assert "先看附件里的 pdb。" in panel
    assert "worker 的推理不该出现在主对话里" not in panel
    # The body is folded until the operator opens it; the panel itself is not
    # hidden, or a run that has thought would look like one that never did.
    assert 'class="think-panel" id="think-panel" hidden' not in page.text
    assert 'id="think-body" hidden' in page.text


def test_a_run_that_never_thought_keeps_the_panel_hidden(cfg: AppConfig, fixtures_dir: Path) -> None:
    store = Store(cfg.db_path)
    try:
        challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
        run = store.create_run(challenge_id, "solve")
    finally:
        store.close()

    app = create_app(cfg, scripted=True)
    with TestClient(app) as client:
        page = client.get(f"/runs/{run.id}")
    assert page.status_code == 200
    assert 'class="think-panel" id="think-panel" hidden' in page.text


async def test_thinking_streams_live_and_settles_as_a_block(
    cfg: AppConfig, fixtures_dir: Path, store_and_bus
) -> None:
    """Deltas wake the page; the block is the durable copy.

    Same contract as the reply: a reload replays the block and never the
    deltas, so keeping both would double the text on screen.
    """
    store, bus = store_and_bus
    backend = ScriptedBackend()
    backend.script["main"] = [
        [
            AgentEvent("thinking_delta", {"text": "先看"}),
            AgentEvent("thinking_delta", {"text": "附件。"}),
            AgentEvent("thinking", {"text": "先看附件。"}),
            AgentEvent("text", {"text": "我先看附件。"}),
            AgentEvent(
                "result",
                {"subtype": "success", "is_error": False, "result": "我先看附件。", "session_id": ""},
            ),
        ]
    ]
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    queue = bus.subscribe(1)
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    await run.start()
    await asyncio.sleep(0.2)
    await run.shutdown(RunStatus.STOPPED, "test", "test")
    bus.unsubscribe(run.id, queue)

    live = []
    while not queue.empty():
        live.append(queue.get_nowait())
    assert [e.kind for e in live if e.kind == "thinking_delta"] == [
        "thinking_delta",
        "thinking_delta",
    ]
    assert all(e.id == 0 for e in live if e.kind == "thinking_delta")
    assert "".join(
        e.payload["text"] for e in live if e.kind == "thinking_delta"
    ) == "先看附件。"

    stored = store.recent_events(run.id, limit=500)
    kinds = [e.kind for e in stored]
    assert "thinking_delta" not in kinds
    blocks = [
        e
        for e in stored
        if e.kind == "agent.event" and e.payload.get("kind") == "thinking"
    ]
    assert [e.payload["text"] for e in blocks] == ["先看附件。"]
    assert blocks[0].payload["role"] == "main"
