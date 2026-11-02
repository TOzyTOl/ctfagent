"""Server surface: pages, JSON API, and the SSE replay contract."""

from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.models import Challenge
from ctfagent import server
from ctfagent.server import create_app


@pytest.fixture()
def client(cfg, monkeypatch):
    app = create_app(cfg, scripted=True)
    with TestClient(app) as test_client:
        yield test_client


def test_index_renders(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "CTF" in response.text


def test_import_challenge_and_start_run(client, cfg, fixtures_dir: Path) -> None:
    response = client.post(
        "/api/challenges", data={"directory": str(fixtures_dir / "easy_crypto")}, follow_redirects=False
    )
    assert response.status_code == 303
    challenges = client.get("/api/challenges").json()["challenges"]
    assert len(challenges) == 1
    assert challenges[0]["name"] == "easy-crypto"

    started = client.post(
        "/api/runs", data={"challenge_id": challenges[0]["id"], "objective": ""}, follow_redirects=False
    )
    assert started.status_code == 303
    run_id = int(started.headers["location"].rstrip("/").split("/")[-1])
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["id"] == run_id
    assert detail["challenge"]["name"] == "easy-crypto"
    assert client.get(f"/runs/{run_id}").status_code == 200


def test_import_rejects_bad_directory(client, tmp_path: Path) -> None:
    response = client.post("/api/challenges", data={"directory": str(tmp_path / "nope")})
    assert response.status_code == 400


def test_events_endpoint_and_sse_replay(client, fixtures_dir: Path) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    store.add_event(run.id, "alpha", {"n": 1})
    store.add_event(run.id, "beta", {"n": 2})

    events = client.get(f"/api/runs/{run.id}/events").json()["events"]
    assert [e["kind"] for e in events] == ["alpha", "beta"]

    # A replay from the first id must contain exactly the second event.
    first_id = events[0]["id"]
    response = client.get(
        f"/api/runs/{run.id}/events/stream?once=1", headers={"Last-Event-ID": str(first_id)}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert f"id: {events[1]['id']}" in response.text
    assert "beta" in response.text
    assert "alpha" not in response.text


def test_stop_endpoint(client, cfg, fixtures_dir: Path) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    run = store.create_run(challenge_id, "solve")
    store.set_run_status(run.id, "running")
    response = client.post(f"/api/runs/{run.id}/stop", follow_redirects=False)
    assert response.status_code == 303
    assert store.get_run(run.id).status == "stopped"


def test_confirm_endpoint_unknown_flag(client) -> None:
    assert client.post("/api/flags/9999/confirm").status_code == 404


# --------------------------------------------------------------- chat-first

def _run_id(response) -> int:
    return int(response.headers["location"].rstrip("/").split("/")[-1])


def test_paste_only_task_with_attachments(client) -> None:
    """The compose page's path: no directory, no manifest, files included."""
    response = client.post(
        "/api/tasks",
        data={
            "description": "silentrotor (crypto)\n我们截获了一段密文，rotor 每次转动 3 格。\nflag 格式 flag{...}",
            "category": "crypto",
            "remote": "nc 10.0.0.5 31337",
        },
        files=[("files", ("cipher.txt", b"QVJHTw==")), ("files", ("rotor.bin", b"\x00\x01"))],
        follow_redirects=False,
    )
    assert response.status_code == 303
    run_id = _run_id(response)

    store = client.app.state.store
    row = store.get_run(run_id)
    assert row is not None
    challenge = store.get_challenge(row.challenge_id)
    # The name is the first line of the paste, not a filesystem path.
    assert challenge.name == "silentrotor (crypto)"
    assert challenge.category == "crypto"
    assert challenge.remote == "nc 10.0.0.5 31337"
    assert sorted(challenge.files) == ["cipher.txt", "rotor.bin"]

    attachments = Path(row.workspace) / "attachments"
    assert (attachments / "cipher.txt").read_bytes() == b"QVJHTw=="
    assert (attachments / "rotor.bin").exists()

    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert "silentrotor (crypto)" in page.text

    listing = client.get(f"/api/runs/{run_id}/attachments").json()["attachments"]
    assert {item["name"] for item in listing} == {"cipher.txt", "rotor.bin"}
    assert client.get(f"/api/runs/{run_id}/attachments/cipher.txt").content == b"QVJHTw=="


def test_paste_only_task_rejects_empty_description(client) -> None:
    assert client.post("/api/tasks", data={"description": "   "}).status_code == 400


def test_attachment_download_refuses_traversal(client) -> None:
    response = client.post(
        "/api/tasks",
        data={"description": "misc"},
        files=[("files", ("ok.txt", b"fine"))],
        follow_redirects=False,
    )
    run_id = _run_id(response)
    assert client.get(f"/api/runs/{run_id}/attachments/..%2F..%2Ftest.db").status_code == 404


def test_chat_accepts_an_attachment_mid_run(client) -> None:
    response = client.post(
        "/api/tasks", data={"description": "web\nstart here"}, follow_redirects=False
    )
    run_id = _run_id(response)

    sent = client.post(
        f"/api/runs/{run_id}/chat",
        data={"text": "这是新线索"},
        files={"files": ("hint.png", b"\x89PNG")},
        follow_redirects=False,
    )
    assert sent.status_code == 303

    store = client.app.state.store
    row = store.get_run(run_id)
    challenge = store.get_challenge(row.challenge_id)
    assert "hint.png" in challenge.files
    assert (Path(row.workspace) / "attachments" / "hint.png").read_bytes() == b"\x89PNG"

    messages = store.list_messages(run_id)
    # The bubble shows what the operator typed; the file is a card on that
    # message, and the agent's copy of the turn names it in the text.
    assert any("hint.png" in m["attachments"] for m in messages)
    assert any(m["role"] == "user" and m["content"] == "这是新线索" for m in messages)


def test_rail_lists_every_run_and_marks_the_active_one(client, fixtures_dir: Path) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    first = store.create_run(challenge_id, "solve one")
    second = store.create_run(challenge_id, "solve two")

    fragment = client.get("/api/rail", params={"active": second.id})
    assert fragment.status_code == 200
    assert f"/runs/{first.id}" in fragment.text
    assert f"/runs/{second.id}" in fragment.text
    # Exactly the selected row carries the `on` class.
    rows = fragment.text.split('class="rail-item')
    assert sum(1 for row in rows if " on " in row[:80]) == 1

    # `/` opens the newest run, and `/runs/<id>` still addresses an older one.
    assert client.get("/").status_code == 200
    assert client.get(f"/runs/{first.id}").status_code == 200


def test_live_endpoint_shows_what_a_running_worker_is_doing(
    client, cfg, fixtures_dir: Path
) -> None:
    """The chat pane's activity strip: the lead can be silent, the workers are not.

    Live symptom: run 6's lead repeated ``wait_tasks`` for 20 minutes without a
    single message, and the page looked stuck while two reverse workers were
    making tool calls every few seconds.
    """
    store = client.app.state.store
    manager = client.app.state.manager
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_rev")))
    task = store.create_task(run.id, "reverse", "unpack", "")
    store.mark_task_started(task.id)
    for index in range(2):
        run._record_event(
            AgentEvent("tool_use", {"tool": "PowerShell", "input": {}, "id": f"c{index}"}),
            task.id,
            "reverse",
        )

    tasks = client.get(f"/api/runs/{run.id}/live").json()["tasks"]
    assert [t["task_id"] for t in tasks] == [task.id]
    assert tasks[0]["role"] == "reverse"
    assert tasks[0]["tools"] == 2
    assert tasks[0]["last_tool"] == "PowerShell"
    assert tasks[0]["silent_s"] >= 0

    # A finished task drops out of the strip; a run this process does not own
    # has nothing live to report.
    store.finish_task(task.id, "done", {}, "")
    assert client.get(f"/api/runs/{run.id}/live").json()["tasks"] == []
    assert client.get("/api/runs/999/live").json()["tasks"] == []


def test_index_is_the_compose_page_with_no_runs(client) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="compose-form"' in page.text


def test_index_is_a_chat_surface(client) -> None:
    """No runs yet: the page is a conversation box, not a form to fill in."""
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="compose-form"' in page.text
    assert 'class="composer home-composer"' in page.text
    assert 'id="drop-veil"' in page.text
    # The old "open run" form is gone; a submit button alone is what sends.
    assert 'id="compose-submit"' in page.text


def test_first_message_is_stored_with_its_attachments(client) -> None:
    response = client.post(
        "/api/tasks",
        data={"description": "web\n先看看附件"},
        files={"files": ("app.zip", b"PK\x03\x04")},
        follow_redirects=False,
    )
    run_id = _run_id(response)

    store = client.app.state.store
    first = store.list_messages(run_id)[0]
    assert first["role"] == "user"
    assert first["content"] == "web\n先看看附件"
    assert first["attachments"] == ["app.zip"]

    page = client.get(f"/runs/{run_id}")
    assert "app.zip" in page.text
    assert 'class="file-card"' in page.text


def test_chat_post_answers_json_for_fetch(client) -> None:
    """The page appends the bubble itself; it does not need the whole document."""
    response = client.post(
        "/api/tasks", data={"description": "web"}, follow_redirects=False
    )
    run_id = _run_id(response)

    sent = client.post(
        f"/api/runs/{run_id}/chat",
        data={"text": "再补一条线索"},
        files={"files": ("hint.txt", b"hint")},
        headers={"X-Requested-With": "fetch"},
    )
    assert sent.status_code == 200
    assert sent.json() == {"ok": True, "stored": ["hint.txt"]}

    # A plain form post still redirects, so the page works without JavaScript.
    plain = client.post(
        f"/api/runs/{run_id}/chat", data={"text": "plain"}, follow_redirects=False
    )
    assert plain.status_code == 303


def test_delete_run_clears_rail_rows_and_workspace(client) -> None:
    """The rail is a job list, not an archive: a deleted run must leave it.

    Live symptom: failed runs piled up in the rail with no way to remove them,
    so every restart re-surfaced work the operator had abandoned.
    """
    store = client.app.state.store
    manager = client.app.state.manager
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_rev")))
    run = manager.prepare(challenge_id, "solve")
    store.add_event(run.id, "alpha", {"n": 1})
    store.create_task(run.id, "reverse", "unpack", "")
    workspace = Path(run.workspace.root)
    keep = manager.prepare(challenge_id, "solve another")

    response = client.post(f"/api/runs/{run.id}/delete", follow_redirects=False)
    assert response.status_code == 303

    assert store.get_run(run.id) is None
    assert [r.id for r in store.list_runs()] == [keep.id]
    assert store.list_tasks(run.id) == []
    assert store.recent_events(run.id, limit=50) == []
    assert manager.get(run.id) is None
    assert not workspace.parent.exists()
    assert f"/runs/{run.id}" not in client.get("/api/rail").text
    assert f"/runs/{keep.id}" in client.get("/api/rail").text


def test_delete_challenge_refused_while_it_has_runs(client) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_crypto")))
    run = store.create_run(challenge_id, "solve")

    refused = client.post(f"/api/challenges/{challenge_id}/delete", follow_redirects=False)
    assert refused.status_code == 400
    assert store.get_challenge(challenge_id) is not None

    client.post(f"/api/runs/{run.id}/delete", follow_redirects=False)
    accepted = client.post(f"/api/challenges/{challenge_id}/delete", follow_redirects=False)
    assert accepted.status_code == 303
    assert store.get_challenge(challenge_id) is None


def test_rail_has_a_delete_control_per_run(client) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_crypto")))
    run = store.create_run(challenge_id, "solve")
    fragment = client.get("/api/rail").text
    assert f"/api/runs/{run.id}/delete" in fragment


def fixtures_dir_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "fixtures" / name


def test_composer_stays_usable_on_a_run_whose_process_is_gone(client) -> None:
    """A run started before a restart used to render its composer disabled.

    Live symptom: every run in the rail opened with a greyed-out textarea and a
    ``?notice=detached`` answer on send, even though the lead's transcript was
    still on disk and could be resumed.
    """
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_crypto")))
    run = store.create_run(challenge_id, "solve")
    store.set_run_status(run.id, "failed", summary="时限用尽")

    # No session was ever recorded for it: nothing to resume, so the composer is
    # honestly disabled rather than accepting a message nobody will read.
    page = client.get(f"/runs/{run.id}").text
    assert "disabled" in page
    assert "不能继续对话" in page

    # A resumed transcript is a different story: the same closed run can be
    # chatted with, because the session id was saved.
    store.set_main_session(run.id, "sess-main", 0.4)
    page = client.get(f"/runs/{run.id}").text
    composer = page[page.index('id="composer"'):]
    composer = composer[: composer.index("</form>")]
    assert "disabled" not in composer
    assert "发消息会恢复主 agent 会话" in page


def test_chat_revives_a_closed_run_instead_of_refusing_the_message(client) -> None:
    store = client.app.state.store
    manager = client.app.state.manager
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_crypto")))
    run = store.create_run(challenge_id, "solve")
    store.set_run_status(run.id, "failed", summary="时限用尽")
    store.set_main_session(run.id, "sess-main", 0.4)

    response = client.post(
        f"/api/runs/{run.id}/chat",
        data={"text": "继续，flag 可能在 config 里"},
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert manager.get(run.id) is not None
    assert not manager.get(run.id).closed
    texts = [m["content"] for m in store.list_messages(run.id) if m["role"] == "user"]
    assert texts and "config" in texts[-1]


def test_chat_reports_when_there_is_no_session_to_resume(client) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_crypto")))
    run = store.create_run(challenge_id, "solve")
    store.set_run_status(run.id, "failed", summary="进程退出")

    response = client.post(
        f"/api/runs/{run.id}/chat",
        data={"text": "在吗"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "notice=" in response.headers["location"]
    assert "无法继续对话" in urllib.parse.unquote(response.headers["location"])
    assert store.list_messages(run.id) == []


def test_chat_refuses_a_run_that_is_over_its_dollar_ceiling(client, cfg) -> None:
    """Reopening an insolvent run sends a request and answers nothing.

    Live symptom (run 18, 2026-10-21): a $35.78 run on a $30.00 ceiling was
    reopened by an operator message; the CLI request went out at 06:59:17 and
    ``run.finished`` with the same "超过 $30.00 预算" summary landed at 06:59:23.
    The operator saw their message and no reply. Refusing up front names the
    ceiling and points at the two ways out.
    """
    store = client.app.state.store
    manager = client.app.state.manager
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_crypto")))
    run = store.create_run(challenge_id, "solve")
    store.set_main_session(run.id, "sess-main", 6.0)
    store.set_run_status(run.id, "failed", summary="超过 $1.00 预算")
    store.add_run_cost(run.id, cfg.limits.run_budget_usd + 1.0)

    response = client.post(
        f"/api/runs/{run.id}/chat",
        data={"text": "继续"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    notice = urllib.parse.unquote(response.headers["location"])
    assert "无法继续对话" in notice and "预算" in notice
    assert "新开一次运行" in notice
    assert manager.get(run.id) is None, "the run was reopened despite being insolvent"
    assert store.list_messages(run.id) == []


def test_the_workspace_says_a_run_has_no_budget_before_the_operator_types(
    client, cfg
) -> None:
    """The page must not offer a composer that would silently fail."""
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir_path("easy_crypto")))
    run = store.create_run(challenge_id, "solve")
    store.set_main_session(run.id, "sess-main", 6.0)
    store.set_run_status(run.id, "failed", summary="超过预算")
    store.add_run_cost(run.id, cfg.limits.run_budget_usd + 1.0)

    page = client.get(f"/runs/{run.id}").text
    assert "预算已用尽" in page
    composer = page[page.index('id="composer"'):]
    composer = composer[: composer.index("</form>")]
    assert composer.count("disabled") == 3, composer


def test_a_fresh_process_is_not_reported_as_stale(monkeypatch) -> None:
    """The banner must not cry wolf on every page load.

    ``_PROCESS_STARTED`` is pinned relative to the newest source mtime instead
    of being read from the clock: the old version asserted "this process is
    fresh" against wall time, so editing a module while the suite ran made the
    test fail for a reason that had nothing to do with the banner it guards.
    """
    import ctfagent.server as srv

    monkeypatch.setattr(srv, "_PROCESS_STARTED", srv._newest_source_mtime() + 1)
    assert srv._code_staleness() == ""


def test_edited_code_is_reported_as_stale(monkeypatch) -> None:
    """The confusion this prevents: a new page answers 404 because the server
    predates it, and a 404 reads as a bug rather than as "restart me"."""
    import ctfagent.server as srv

    monkeypatch.setattr(srv, "_newest_source_mtime", lambda: srv._PROCESS_STARTED + 60)
    message = srv._code_staleness()
    assert "代码比这个进程新" in message
    assert "重启" in message


def test_the_stale_banner_reaches_the_page(client, monkeypatch) -> None:
    import ctfagent.server as srv

    monkeypatch.setattr(srv, "_newest_source_mtime", lambda: srv._PROCESS_STARTED + 60)
    page = client.get("/docs").text
    assert "stale-banner" in page
    assert "start.cmd -Restart" in page


# ------------------------------------------------------------------- folders
# Runs accumulated one row per attempt, and the rail is the only list of them.
# A folder is the operator's own grouping (one competition, one practice
# batch); it is bookkeeping, so nothing inside it can be lost by using it.


def test_a_run_can_be_filed_into_a_folder_and_moved_back(client, fixtures_dir: Path) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    folder = store.add_folder("DASCTF 十月赛")
    assert run.folder_id is None  # every run starts unfiled

    moved = client.post(
        f"/api/runs/{run.id}/folder", data={"folder_id": str(folder.id)}, follow_redirects=False
    )
    assert moved.status_code == 303
    assert store.get_run(run.id).folder_id == folder.id

    # The rail groups by folder and the folder carries its run.
    rail = client.get("/api/rail").text
    assert "DASCTF 十月赛" in rail and "rail-group" in rail

    back = client.post(f"/api/runs/{run.id}/folder", data={"folder_id": ""}, follow_redirects=False)
    assert back.status_code == 303
    assert store.get_run(run.id).folder_id is None


def test_deleting_a_folder_keeps_its_runs(client) -> None:
    """A folder is a label. Deleting it must never take work with it."""
    store = client.app.state.store
    challenge_id = store.add_challenge(
        Challenge(id=0, name="c", category="misc", description="d")
    )
    keep = store.create_run(challenge_id, "solve")
    folder = store.add_folder("临时分组")
    store.set_run_folder(keep.id, folder.id)

    deleted = client.post(f"/api/folders/{folder.id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert store.get_folder(folder.id) is None
    assert store.get_run(keep.id) is not None
    assert store.get_run(keep.id).folder_id is None


def test_a_pasted_challenge_can_be_created_inside_a_folder(client) -> None:
    """The composer's folder picker: a new run lands in the folder chosen."""
    store = client.app.state.store
    folder = store.add_folder("自练")
    response = client.post(
        "/api/tasks",
        data={"description": "一道测试题", "folder_id": str(folder.id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    run_id = int(response.headers["location"].rstrip("/").split("/")[-1])
    assert store.get_run(run_id).folder_id == folder.id


def test_the_rail_lists_folders_first_and_unfiled_last(client) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(
        Challenge(id=0, name="c", category="misc", description="d")
    )
    folder = store.add_folder("有名字的")
    filed = store.create_run(challenge_id, "solve", folder_id=folder.id)
    loose = store.create_run(challenge_id, "solve")

    _, groups = server._rail_view(store, client.app.state.manager)
    assert [g["folder"].name if g["folder"] else None for g in groups] == ["有名字的", None]
    assert [r["run"].id for r in groups[0]["rows"]] == [filed.id]
    assert [r["run"].id for r in groups[1]["rows"]] == [loose.id]

    # An empty folder is still listed: it is something the operator made, and a
    # folder that vanishes on creation reads as a failed creation.
    empty = store.add_folder("空的")
    _, groups = server._rail_view(store, client.app.state.manager)
    assert [g["folder"].id for g in groups if g["folder"]] == [folder.id, empty.id]


def test_a_folder_keeps_showing_old_runs_after_many_newer_ones(client) -> None:
    """The per-folder window exists so filing a run does not hide it.

    A plain "newest 120 overall" made a competition from last month fall out of
    the rail, and its folder then rendered as empty — the same picture as never
    having run anything there. The rail is where old work is found.
    """
    store = client.app.state.store
    challenge_id = store.add_challenge(
        Challenge(id=0, name="c", category="misc", description="d")
    )
    old = store.add_folder("上个月的比赛")
    ancient = store.create_run(challenge_id, "solve", folder_id=old.id)
    start = store.create_run(challenge_id, "solve", folder_id=old.id)
    assert start.id > ancient.id

    # A lot of newer work, all unfiled, far beyond the old 120-row window.
    for _ in range(200):
        store.create_run(challenge_id, "solve")

    _, groups = server._rail_view(store, client.app.state.manager)
    by_name = {g["folder"].name if g["folder"] else None: g for g in groups}
    ids = [r["run"].id for r in by_name["上个月的比赛"]["rows"]]
    assert ancient.id in ids and start.id in ids
    # The total is every run in the folder, not just the window's slice.
    assert by_name["上个月的比赛"]["total"] == 2


def test_a_cut_off_folder_says_how_many_it_hides(client) -> None:
    """A heading shows "shown/total" once the window cuts it, and the footer
    counts the whole history rather than the visible slice.

    Without the total, a 41-run competition rendering 25 rows reads as a
    25-run competition — the number the operator would repeat to someone else.
    """
    store = client.app.state.store
    challenge_id = store.add_challenge(
        Challenge(id=0, name="c", category="misc", description="d")
    )
    folder = store.add_folder("大赛")
    for _ in range(41):
        store.create_run(challenge_id, "solve", folder_id=folder.id)
    for _ in range(5):
        store.create_run(challenge_id, "solve")

    _, groups = server._rail_view(store, client.app.state.manager)
    big = next(g for g in groups if g["folder"] is not None)
    assert len(big["rows"]) == 25 and big["total"] == 41

    page = client.get("/new").text
    assert "25/41" in page
    # Footer totals are over every run, not the rail's window.
    totals = store.run_totals()
    assert totals["total"] == 46
    assert f"<b>{totals['total']}</b> 个任务" in page


def test_a_folder_name_cannot_be_reused(client) -> None:
    """Two identical headings in the rail are two folders nobody can tell apart."""
    store = client.app.state.store
    store.add_folder("DASCTF 十月赛")

    again = client.post("/api/folders", data={"name": "DASCTF 十月赛"}, follow_redirects=False)
    assert again.status_code == 303
    # No folder was created, and the page says why.
    assert [f.name for f in store.list_folders()] == ["DASCTF 十月赛"]
    assert "DASCTF" in urllib.parse.unquote(again.headers["location"])
    page = client.get(again.headers["location"]).text
    assert "已经有叫" in page

    # Renaming onto an existing name is refused the same way.
    other = store.add_folder("自练")
    renamed = client.post(
        f"/api/folders/{other.id}/rename", data={"name": "DASCTF 十月赛"}, follow_redirects=False
    )
    assert renamed.status_code == 303
    assert store.get_folder(other.id).name == "自练"
    # Renaming to its own name is not a clash.
    assert client.post(
        f"/api/folders/{other.id}/rename", data={"name": "自练"}, follow_redirects=False
    ).status_code == 303
    assert store.get_folder(other.id).name == "自练"


def test_duplicate_folder_names_in_an_old_database_are_separated(tmp_path: Path) -> None:
    """A database from before the constraint must still open, and names diverge."""
    import sqlite3

    from ctfagent.db import Store

    path = tmp_path / "old.db"
    store = Store(path)
    with store._lock:  # whatever an older build wrote before the check existed
        store._conn.execute("DROP INDEX IF EXISTS folders_name")
        store._conn.execute("INSERT INTO folders (name) VALUES ('同名')")
        store._conn.execute("INSERT INTO folders (name) VALUES ('同名')")
        store._conn.commit()
    store.close()

    reopened = Store(path)
    assert [f.name for f in reopened.list_folders()] == ["同名", "同名 (2)"]
    reopened.close()
    # The unique index is in place afterwards, so the next writer is refused.
    probe = sqlite3.connect(path)
    try:
        probe.execute("INSERT INTO folders (name) VALUES ('同名')")
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("duplicate folder name was accepted")
    finally:
        probe.close()


def test_a_child_that_never_started_says_so_in_words() -> None:
    """Measured failure: exit code 3221225794, zero cost, same second it started.

    The operator saw a ten-digit number and had no way to know the machine was
    out of commit memory. The hint has to name the cause and the fix.
    """
    from ctfagent.backend import spawn_error_message

    message = spawn_error_message(
        "ProcessError: Command failed with exit code 3221225794 (exit code: 3221225794)"
    )
    assert "内存" in message
    assert "0xC0000142" in message

    # Anything else is left to the caller rather than guessed at.
    assert spawn_error_message("ProcessError: exit code 1") == ""
    assert spawn_error_message("") == ""


def test_the_fragments_endpoint_redraws_the_two_regions_a_confirm_changes(
    client, cfg, fixtures_dir: Path
) -> None:
    """Confirming no longer reloads the page, so these two regions must be
    re-renderable on their own.

    The flag list loses its confirm/reject buttons and gains the confirmed
    stamp; the header gains 写报告. Both come from the same templates the page
    uses, which is what keeps a fragment swap identical to a full reload.
    """
    from ctfagent.db import Store

    client.post(
        "/api/challenges",
        data={"directory": str(fixtures_dir / "easy_crypto")},
        follow_redirects=False,
    )
    challenge_id = client.get("/api/challenges").json()["challenges"][0]["id"]
    client.post(
        "/api/runs", data={"challenge_id": challenge_id, "objective": ""},
        follow_redirects=False,
    )
    run_id = client.get("/api/runs").json()[0]["id"]

    store = Store(cfg.db_path)
    try:
        flag = store.add_flag(run_id, None, "flag{probe}", "test", "probe method", "high")
        assert flag is not None
        store.set_flag_status(flag.id, "confirmed", "confirmed by the test")
    finally:
        store.close()

    html = client.get(f"/api/runs/{run_id}/fragments").text
    assert 'id="flags-body"' in html
    assert 'id="run-actions"' in html
    # The buttons the operator just used are gone.
    assert f'/api/flags/{flag.id}/confirm' not in html
    # And the action the confirm was for has appeared.
    assert "写报告" in html

    assert client.get("/api/runs/999999/fragments").status_code == 404


def test_the_workspace_shows_the_hypothesis_board(client, cfg, fixtures_dir: Path) -> None:
    """Board mode moves the decision to the board; the page has to show it.

    Without this pane an operator watching a board-mode run sees a lead that
    stopped dispatching and no reason for it. The status labels are the board's
    own — ``open`` means "waiting for a worker", not the task queue's "排队".
    """
    from ctfagent import swarm
    from ctfagent.db import Store

    client.post(
        "/api/challenges",
        data={"directory": str(fixtures_dir / "easy_crypto")},
        follow_redirects=False,
    )
    challenge_id = client.get("/api/challenges").json()["challenges"][0]["id"]
    client.post(
        "/api/runs", data={"challenge_id": challenge_id, "objective": ""},
        follow_redirects=False,
    )
    run_id = client.get("/api/runs").json()[0]["id"]

    store = Store(cfg.db_path)
    try:
        store.add_hypothesis(
            run_id,
            swarm.Hypothesis(
                family="crypto", statement="模数可分解",
                criterion="枚举 10^6 内无因子", evidence="p、q 相差很小",
            ),
        )
    finally:
        store.close()

    page = client.get(f"/runs/{run_id}").text
    # The tab only exists when the board has something on it.
    assert 'data-stab="board"' in page
    assert "模数可分解" in page
    # The falsifier travels with the entry, not only in the lead's context.
    assert "枚举 10^6 内无因子" in page
    assert "待认领" in page and "排队" not in page.split('data-spane="board"')[1].split("</section>")[0]
