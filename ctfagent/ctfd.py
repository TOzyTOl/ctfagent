"""CTFd platform client: pull challenges, download attachments, submit flags.

Deliberately small and dependency-free (``urllib``): the whole system runs from
one virtualenv with no build step, and adding ``requests`` for four calls is not
worth the supply-chain surface. Every request is blocking; callers run it in a
worker thread so the FastAPI event loop is never held.

Contract notes learned from the CTFd API:

- Auth is ``Authorization: Token <token>`` (an API token from the user's
  settings page). The legacy ``session`` cookie flow is not supported.
- ``GET /api/v1/challenges`` lists challenges but omits ``files`` and
  ``connection_info``; those need one ``GET /api/v1/challenges/<id>`` per
  challenge. Sync therefore does N+1 requests, which is fine for a competition
  (tens of challenges) and is why it is a background action, not a page render.
- ``POST /api/v1/challenges/attempt`` answers with ``data.status``:
  ``correct``, ``incorrect`` or ``already_solved`` (plus ``ratelimited`` when
  the platform is throttling submissions).
- A wrong flag is a *successful* API call. Only transport/HTTP/auth failures
  raise.

The token is never logged, echoed back, or included in exception text.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 20
#: A challenge attachment is untrusted input, not a video: cap it so a hostile
#: or broken platform cannot fill the operator's disk during a sync.
MAX_DOWNLOAD_BYTES = 1 << 30


class PlatformError(RuntimeError):
    """Any failure the operator should see as "the platform did not cooperate"."""


@dataclass
class RemoteChallenge:
    """One challenge as the platform describes it."""

    external_id: str
    name: str
    category: str = "unknown"
    description: str = ""
    value: int = 0
    solved: bool = False
    files: list[str] = field(default_factory=list)
    connection_info: str = ""

    @property
    def remote(self) -> str:
        """``nc host port`` form when the platform supplies a connection string."""
        info = self.connection_info.strip()
        if not info:
            return ""
        # CTFd renders connection_info as free text; a URL is the other common
        # shape. Keep the raw text, it is what the agents should read.
        return info if len(info) <= 300 else info[:300]


@dataclass
class SubmitResult:
    status: str          # correct | incorrect | already_solved | ratelimited | error
    message: str = ""

    @property
    def solved(self) -> bool:
        return self.status in ("correct", "already_solved")


class CTFdClient:
    def __init__(self, base_url: str, token: str, *, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token.strip()
        self.timeout = timeout

    # ------------------------------------------------------------------ plumbing
    def _request(
        self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None
    ) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Token {self._token}",
            "User-Agent": "ctfagent/0.1",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            # The body of an error can carry the platform's explanation, but it
            # may also echo the request. Only the status line is safe to keep.
            raise PlatformError(f"{method} {path} 返回 HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise PlatformError(f"连不上 {method} {path}：{reason}") from None
        except TimeoutError:
            raise PlatformError(f"{method} {path} 超时") from None
        try:
            return json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise PlatformError(f"{method} {path} 返回的不是 JSON（平台地址填错了？）") from None

    # ---------------------------------------------------------------------- api
    def whoami(self) -> dict[str, Any]:
        """Validate the token and return the platform's view of the user."""
        data = self._request("/users/me")
        if not isinstance(data, dict) or "data" not in data:
            raise PlatformError("/users/me 返回格式不对")
        return data["data"]

    def list_challenges(self) -> list[RemoteChallenge]:
        """Every visible challenge, with files and connection info filled in."""
        listing = self._request("/challenges").get("data") or []
        out: list[RemoteChallenge] = []
        for entry in listing:
            if not isinstance(entry, dict):
                continue
            external_id = str(entry.get("id", "")).strip()
            if not external_id:
                continue
            detail = self.challenge(external_id)
            out.append(detail)
        return out

    def challenge(self, external_id: str) -> RemoteChallenge:
        data = self._request(f"/challenges/{urllib.parse.quote(str(external_id))}").get("data") or {}
        files = data.get("files") or []
        if isinstance(files, str):  # some CTFd forks return a bare string
            files = [files]
        return RemoteChallenge(
            external_id=str(data.get("id", external_id)),
            name=str(data.get("name") or f"challenge-{external_id}"),
            category=str(data.get("category") or "unknown").lower(),
            description=str(data.get("description") or ""),
            value=int(data.get("value") or 0),
            solved=bool(data.get("solved_by_me")),
            files=[str(f) for f in files if f],
            connection_info=str(data.get("connection_info") or ""),
        )

    def download(
        self,
        file_url: str,
        dest_dir: Path,
        *,
        name: str = "",
        taken: set[str] | None = None,
    ) -> Path:
        """Stream one challenge attachment into ``dest_dir``.

        ``file_url`` comes from the platform and may be absolute or rooted. The
        saved name is the URL's basename (``name`` overrides), sanitised the
        same way as an operator upload: a hostile platform must not be able to
        write outside ``dest_dir`` with ``../`` in a filename.

        ``taken`` is the set of names already written by this sync. Passing it
        makes the download idempotent: the file is overwritten in place (the
        platform is authoritative and may have replaced it) while a second
        attachment that happens to share a basename still gets its own file.
        Without it, an existing file is kept and the new one gets a suffix.
        """
        url = file_url if file_url.startswith(("http://", "https://")) else (
            self.base_url + ("" if file_url.startswith("/") else "/") + file_url
        )
        raw = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        filename = _safe_name(name or urllib.parse.unquote(raw) or "attachment")
        dest_dir.mkdir(parents=True, exist_ok=True)
        if taken is not None:
            stem, suffix = Path(filename).stem, Path(filename).suffix
            candidate = filename
            index = 2
            while candidate in taken:
                candidate = f"{stem}-{index}{suffix}"
                index += 1
            filename = candidate
            taken.add(filename)
            target = dest_dir / filename
        else:
            target = dest_dir / filename
            if target.exists():
                stem, suffix = target.stem, target.suffix
                for index in range(2, 1000):
                    candidate = dest_dir / f"{stem}-{index}{suffix}"
                    if not candidate.exists():
                        target = candidate
                        break

        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Token {self._token}", "User-Agent": "ctfagent/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response, target.open(
                "wb"
            ) as fh:
                written = 0
                while chunk := response.read(1 << 16):
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise PlatformError(f"附件超过 {MAX_DOWNLOAD_BYTES} 字节上限")
                    fh.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target

    def submit(self, external_id: str, flag: str) -> SubmitResult:
        """Submit one flag. A wrong flag returns ``incorrect``, it does not raise."""
        try:
            challenge_id = int(external_id)
        except ValueError:
            raise PlatformError(f"题目 id {external_id!r} 不是数字") from None
        payload = {"challenge_id": challenge_id, "submission": flag}
        body = self._request("/challenges/attempt", method="POST", payload=payload)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise PlatformError("/challenges/attempt 返回格式不对")
        status = str(data.get("status") or "error").strip().lower()
        message = str(data.get("message") or "").strip()
        return SubmitResult(status=status, message=message)


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name or "attachment"
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip()
    name = re.sub(r"\s+", " ", name)
    if not name or name in (".", ".."):
        name = "attachment"
    return name[:120]
