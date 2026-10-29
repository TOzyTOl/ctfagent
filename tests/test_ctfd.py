"""The CTFd client against a stub platform.

No network: a tiny HTTP server stands in for CTFd so the contract (auth header,
N+1 detail fetch, attachment download, attempt statuses) is exercised exactly
as it is against a real instance.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ctfagent.ctfd import CTFdClient, PlatformError

TOKEN = "test-token-123"


class _Handler(BaseHTTPRequestHandler):
    challenges = {
        "7": {
            "id": 7,
            "name": "Baby Web",
            "category": "Web",
            "description": "Look at the cookie.",
            "value": 100,
            "solved_by_me": False,
            "files": ["/files/7/app.zip"],
            "connection_info": "http://chal.example:8080",
        },
        "9": {
            "id": 9,
            "name": "Old One",
            "category": "Crypto",
            "description": "already done",
            "value": 50,
            "solved_by_me": True,
            "files": [],
            "connection_info": "",
        },
    }

    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode())

    def _authed(self) -> bool:
        if self.headers.get("Authorization") != f"Token {TOKEN}":
            self._json({"success": False}, 403)
            return False
        return True

    def do_GET(self):  # noqa: N802 - http.server API
        if not self._authed():
            return
        if self.path == "/api/v1/users/me":
            return self._json({"success": True, "data": {"id": 1, "name": "player"}})
        if self.path == "/api/v1/challenges":
            listing = [
                {"id": int(k), "name": v["name"], "category": v["category"],
                 "value": v["value"], "solved_by_me": v["solved_by_me"]}
                for k, v in self.challenges.items()
            ]
            return self._json({"success": True, "data": listing})
        if self.path.startswith("/api/v1/challenges/"):
            cid = self.path.rsplit("/", 1)[-1]
            if cid in self.challenges:
                return self._json({"success": True, "data": self.challenges[cid]})
            return self._json({"success": False}, 404)
        if self.path == "/files/7/app.zip":
            return self._send(200, b"PK\x03\x04fake zip", "application/zip")
        self._json({"success": False}, 404)

    def do_POST(self):  # noqa: N802
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/v1/challenges/attempt":
            if payload.get("submission") == "flag{right}":
                return self._json(
                    {"success": True, "data": {"status": "correct", "message": "Nice"}}
                )
            return self._json(
                {"success": True, "data": {"status": "incorrect", "message": "Nope"}}
            )
        self._json({"success": False}, 404)


@pytest.fixture()
def ctfd_platform():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_whoami_rejects_a_bad_token(ctfd_platform: str) -> None:
    with pytest.raises(PlatformError, match="HTTP 403"):
        CTFdClient(ctfd_platform, "wrong").whoami()


def test_list_challenges_fetches_details_and_normalises(ctfd_platform: str) -> None:
    challenges = CTFdClient(ctfd_platform, TOKEN).list_challenges()
    by_id = {c.external_id: c for c in challenges}
    assert set(by_id) == {"7", "9"}
    baby = by_id["7"]
    assert baby.name == "Baby Web"
    assert baby.category == "web"          # normalised to the local vocabulary
    assert baby.solved is False
    assert baby.files == ["/files/7/app.zip"]
    assert baby.remote == "http://chal.example:8080"
    assert by_id["9"].solved is True


def test_download_writes_into_the_given_directory(ctfd_platform: str, tmp_path: Path) -> None:
    target = CTFdClient(ctfd_platform, TOKEN).download("/files/7/app.zip", tmp_path)
    assert target.parent == tmp_path
    assert target.name == "app.zip"
    assert target.read_bytes().startswith(b"PK")


def test_download_never_escapes_the_directory(ctfd_platform: str, tmp_path: Path) -> None:
    """A hostile file URL cannot choose where the bytes land."""
    client = CTFdClient(ctfd_platform, TOKEN)
    target = client.download("/files/7/app.zip", tmp_path / "inner", name="../../evil.zip")
    assert target.parent == tmp_path / "inner"
    assert target.name == "evil.zip"


def test_submit_maps_platform_statuses(ctfd_platform: str) -> None:
    client = CTFdClient(ctfd_platform, TOKEN)
    right = client.submit("7", "flag{right}")
    assert right.status == "correct" and right.solved
    wrong = client.submit("7", "flag{nope}")
    assert wrong.status == "incorrect" and not wrong.solved


def test_submit_refuses_a_non_numeric_challenge_id(ctfd_platform: str) -> None:
    with pytest.raises(PlatformError, match="不是数字"):
        CTFdClient(ctfd_platform, TOKEN).submit("not-an-id", "flag{x}")
