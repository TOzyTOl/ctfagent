"""The chat renders Markdown identically whether it streams or is re-read.

Live symptom this locks down: a reply arrived formatted, then a reload (or
clicking 停止, or any of the SSE events that re-render the page) printed the
same text as one run-on paragraph with literal ``##`` and ``` markers, because
the stored path used the raw text while the streamed path used the browser's
renderer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ctfagent.challenge import load_challenge
from ctfagent.mdrender import render
from ctfagent.server import create_app


def test_markdown_produces_blocks_not_literal_markers() -> None:
    html = str(render("## 结论\n\n- 甲\n- 乙\n\n```\nflag{x}\n```\n"))
    assert "<h2>结论</h2>" in html
    assert "<ul>" in html and "<li>甲</li>" in html
    assert "<pre><code>flag{x}</code></pre>" in html
    assert "##" not in html


def test_markdown_never_passes_raw_html_through() -> None:
    html = str(render("<script>alert(1)</script> & <b>x</b>"))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


def test_markdown_keeps_inline_formatting_and_links() -> None:
    html = str(render("**粗** 和 `code` 和 [链](https://example.com/a)"))
    assert "<strong>粗</strong>" in html
    assert "<code>code</code>" in html
    assert '<a href="https://example.com/a" rel="noopener">链</a>' in html


def test_markdown_escapes_link_targets_it_would_otherwise_trust() -> None:
    # Only http(s) becomes a link; anything else stays text.
    html = str(render("[x](javascript:alert(1))"))
    assert "javascript:" not in html or "<a" not in html


@pytest.fixture()
def client(cfg) -> TestClient:
    app = create_app(cfg, scripted=True)
    with TestClient(app) as test_client:
        yield test_client


def test_a_stored_reply_is_rendered_as_markdown_on_the_page(
    client: TestClient, fixtures_dir: Path
) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    store.add_message(
        run.id,
        "assistant",
        "## 方向\n\n先看 **RSA**：\n\n```\nn = 12345\n```\n",
    )
    store.add_message(run.id, "user", "**这不是 markdown**，照原样显示")

    page = client.get(f"/runs/{run.id}").text
    # The agent's turn is markup…
    assert "<h2>方向</h2>" in page
    assert "<strong>RSA</strong>" in page
    assert "<pre><code>n = 12345</code></pre>" in page
    # …and the operator's own words are not: they typed asterisks, they see them.
    assert "<strong>这不是 markdown</strong>" not in page
    assert "**这不是 markdown**" in page


def test_the_rail_does_not_force_the_unfiled_group_open(client: TestClient) -> None:
    """The fold state belongs to the operator, not to the folder's id.

    The unfiled group used to render ``open`` unconditionally, so folding it was
    undone by the next click or refresh. It is now only a default — and none,
    for an unfiled group with nothing live in it.
    """
    store = client.app.state.store
    from ctfagent.models import Challenge

    challenge_id = store.add_challenge(
        Challenge(id=0, name="c", category="misc", description="d")
    )
    store.create_run(challenge_id, "solve")

    rail = client.get("/api/rail").text
    assert 'data-folder=""' in rail
    head = rail.split('data-folder=""', 1)[1].split(">", 1)[0]
    assert "open" not in head
