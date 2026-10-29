"""The competition surface end to end: connect, sync, start, confirm, submit.

The platform is the same stub CTFd used by ``test_ctfd`` — one unsolved
challenge with an attachment and one already-solved challenge — so the whole
path is exercised without a network.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ctfagent.challenge import load_challenge
from ctfagent.server import create_app
from test_ctfd import TOKEN, _Handler


@pytest.fixture()
def platform_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def client(cfg, platform_url):
    app = create_app(cfg, scripted=True)
    with TestClient(app) as test_client:
        test_client.platform_url = platform_url
        yield test_client


def _connect(client, name: str = "十月赛") -> int:
    response = client.post(
        "/api/competitions",
        data={"name": name, "base_url": client.platform_url, "token": TOKEN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error" not in response.headers["location"]
    comps = client.app.state.store.list_competitions()
    assert comps and comps[0].name == name
    return comps[0].id


def _sync(client, competition_id: int) -> None:
    response = client.post(
        f"/api/competitions/{competition_id}/sync", follow_redirects=False
    )
    assert response.status_code == 303
    assert "error" not in response.headers["location"]


def test_bad_token_is_not_stored(client) -> None:
    response = client.post(
        "/api/competitions",
        data={"name": "x", "base_url": client.platform_url, "token": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error" in response.headers["location"]
    assert client.app.state.store.list_competitions() == []


def test_sync_pulls_challenges_and_attachments(client, cfg) -> None:
    competition_id = _connect(client)
    _sync(client, competition_id)

    store = client.app.state.store
    challenges = [c for c in store.list_challenges() if c.competition_id == competition_id]
    by_name = {c.name: c for c in challenges}
    assert set(by_name) == {"Baby Web", "Old One"}

    baby = by_name["Baby Web"]
    assert baby.external_id == "7"
    assert baby.category == "web"
    assert baby.remote == "http://chal.example:8080"
    assert baby.files == ["app.zip"]
    assert baby.source_dir
    attachment = Path(baby.source_dir) / "app.zip"
    assert attachment.read_bytes().startswith(b"PK")

    # The already-solved challenge is recorded but its (nonexistent) files are
    # not fetched, and the operator's local state is untouched.
    assert by_name["Old One"].external_id == "9"
    assert by_name["Old One"].files == []


def test_sync_is_idempotent(client) -> None:
    competition_id = _connect(client)
    _sync(client, competition_id)
    store = client.app.state.store
    assert "新增 2" in store.get_competition(competition_id).last_sync_note

    _sync(client, competition_id)
    challenges = [c for c in store.list_challenges() if c.competition_id == competition_id]
    assert len(challenges) == 2
    assert "更新 2" in store.get_competition(competition_id).last_sync_note


def test_page_lists_the_synced_challenges(client) -> None:
    competition_id = _connect(client)
    _sync(client, competition_id)
    page = client.get("/competitions")
    assert page.status_code == 200
    assert "Baby Web" in page.text
    assert "开跑" in page.text


def test_resync_updates_the_renamed_challenge_in_place(client) -> None:
    """A platform rename reaches the local row without touching its files."""
    competition_id = _connect(client)
    _sync(client, competition_id)
    store = client.app.state.store
    baby = next(c for c in store.list_challenges() if c.name == "Baby Web")
    # An operator-supplied attachment must survive the re-sync.
    store.set_challenge_files(baby.id, [*baby.files, "notes.txt"])

    _Handler.challenges["7"]["name"] = "Baby Web (renamed)"
    try:
        _sync(client, competition_id)
    finally:
        _Handler.challenges["7"]["name"] = "Baby Web"

    refreshed = store.get_challenge(baby.id)
    assert refreshed is not None
    assert refreshed.name == "Baby Web (renamed)"
    assert refreshed.files == ["app.zip", "notes.txt"]


def test_start_creates_one_run_per_challenge(client) -> None:
    competition_id = _connect(client)
    _sync(client, competition_id)
    store = client.app.state.store
    baby = next(c for c in store.list_challenges() if c.name == "Baby Web")

    response = client.post(
        f"/api/competitions/{competition_id}/start",
        data={"challenge_id": baby.id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    runs = store.list_runs_for_challenge(baby.id)
    assert len(runs) == 1
    # The attachment downloaded during sync is what the run's workspace is
    # built from — that link is the whole point of recording `source_dir`.
    attachment = Path(runs[0].workspace) / "attachments" / "app.zip"
    assert attachment.read_bytes().startswith(b"PK")

    # Pressing it again starts nothing: the run already exists.
    again = client.post(
        f"/api/competitions/{competition_id}/start",
        data={"challenge_id": baby.id},
        follow_redirects=False,
    )
    assert again.status_code == 303
    assert len(store.list_runs_for_challenge(baby.id)) == 1


def _platform_flag(client) -> tuple[int, int]:
    """A confirmed flag on a synced challenge; returns (run_id, flag_id)."""
    competition_id = _connect(client)
    _sync(client, competition_id)
    store = client.app.state.store
    challenge = next(c for c in store.list_challenges() if c.name == "Baby Web")
    run = store.create_run(challenge.id, "solve it")
    flag = store.add_flag(run.id, None, "flag{right}", "agent", "sql injection", "high")
    assert flag is not None
    store.set_flag_status(flag.id, "confirmed")
    return run.id, flag.id


def test_submit_is_refused_before_confirmation(client) -> None:
    competition_id = _connect(client)
    _sync(client, competition_id)
    store = client.app.state.store
    challenge = next(c for c in store.list_challenges() if c.name == "Baby Web")
    run = store.create_run(challenge.id, "solve it")
    flag = store.add_flag(run.id, None, "flag{right}", "agent", "guess", "low")
    assert flag is not None

    response = client.post(f"/api/flags/{flag.id}/submit", follow_redirects=False)
    assert response.status_code == 409
    assert store.latest_submission(flag.id) is None


def test_submit_records_the_platform_receipt(client) -> None:
    run_id, flag_id = _platform_flag(client)
    response = client.post(f"/api/flags/{flag_id}/submit", follow_redirects=False)
    assert response.status_code == 303
    assert f"/runs/{run_id}" in response.headers["location"]

    store = client.app.state.store
    receipt = store.latest_submission(flag_id)
    assert receipt is not None
    assert receipt.status == "correct"
    assert receipt.external_id == "7"

    kinds = [e["kind"] for e in client.get(f"/api/runs/{run_id}/events").json()["events"]]
    assert "flag.submitted" in kinds


def test_a_wrong_flag_is_recorded_not_raised(client) -> None:
    competition_id = _connect(client)
    _sync(client, competition_id)
    store = client.app.state.store
    challenge = next(c for c in store.list_challenges() if c.name == "Baby Web")
    run = store.create_run(challenge.id, "solve it")
    flag = store.add_flag(run.id, None, "flag{nope}", "agent", "guess", "low")
    assert flag is not None
    store.set_flag_status(flag.id, "confirmed")

    response = client.post(f"/api/flags/{flag.id}/submit", follow_redirects=False)
    assert response.status_code == 303
    receipt = store.latest_submission(flag.id)
    assert receipt is not None and receipt.status == "incorrect"


def test_a_non_platform_challenge_cannot_be_submitted(client, fixtures_dir: Path) -> None:
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    flag = store.add_flag(run.id, None, "flag{x}", "agent", "guess", "low")
    assert flag is not None
    store.set_flag_status(flag.id, "confirmed")

    response = client.post(f"/api/flags/{flag.id}/submit", follow_redirects=False)
    assert response.status_code == 409
    assert store.latest_submission(flag.id) is None
