"""The one HTTP client every attack script shares.

Three decisions live here and nowhere else, because each of them was paid for
separately in a real run:

* **Proxy off by default.** The operator's proxy (``127.0.0.1:7897``) answers
  502 for a target on a non-80/443 port — the same shape as "the target is
  down". A script that forgets ``ProxyHandler({})`` therefore reports a dead
  target and the worker goes looking for the wrong bug. Off is the safe
  default; ``proxy=True`` opts back in.
* **Retries on transport only.** One dropped connection used to abort a
  400-request brute force. A 4xx is an answer, not a failure, and is never
  retried — retrying it just multiplies the noise a WAF sees.
* **``elapsed`` is measured per request.** A time-based oracle needs the wall
  clock of the request it just made, so the client records it rather than
  making the caller wrap everything in ``time.time()``.

Standard library only: the kit has to work in the host venv, inside WSL, and in
a bare container, and each of those has a different set of third-party HTTP
packages installed.
"""

from __future__ import annotations

import json as _json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = ["Reply", "Session", "TransportError"]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class TransportError(RuntimeError):
    """Every attempt failed. The oracle turns this into ``None``, not ``False``.

    The distinction is the whole point: an unreachable target and a condition
    that evaluated to FALSE produce the same *absence* of an answer, and a
    blind extractor that conflates them silently emits wrong characters for
    every retry-exhausted position.
    """


class Reply:
    """One HTTP response, already decoded."""

    __slots__ = ("status", "raw", "headers", "elapsed", "url", "error")

    def __init__(
        self,
        status: int,
        raw: bytes,
        headers: dict[str, str],
        elapsed: float,
        url: str,
        error: str = "",
    ) -> None:
        self.status = status
        self.raw = raw
        self.headers = headers
        self.elapsed = elapsed
        self.url = url
        self.error = error

    @property
    def text(self) -> str:
        """Body as text. Charset from the header, then utf-8, gbk, latin-1.

        The GBK fallback is not decoration: a Chinese-hosted CTF platform
        sends ``Content-Type: text/html`` with no charset and a GBK body, and
        utf-8 with ``errors="replace"`` turns the flag into replacement
        characters — a failure that looks like a wrong extraction.
        """
        ctype = self.headers.get("content-type", "")
        charset = "utf-8"
        for part in ctype.split(";")[1:]:
            if "charset=" in part:
                charset = part.split("charset=")[1].strip().strip('"')
        for enc in dict.fromkeys([charset, "utf-8", "gbk", "latin-1"]):
            try:
                return self.raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return self.raw.decode("latin-1", "replace")

    @property
    def json(self):
        return _json.loads(self.text)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Reply {self.status} {len(self.raw)}B {self.elapsed:.3f}s>"


class Session:
    """A retrying, proxy-isolated HTTP session.

    ``base`` is joined with relative paths. A path that already starts with a
    scheme is used verbatim, so a spec can point one request at another host.
    """

    def __init__(
        self,
        base: str = "",
        *,
        timeout: float = 15.0,
        retries: int = 3,
        headers: dict[str, str] | None = None,
        proxy: bool = False,
        backoff: float = 0.25,
    ) -> None:
        self.base = base
        self.timeout = timeout
        self.retries = max(0, retries)
        self.backoff = backoff
        self.headers = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
        if headers:
            self.headers.update(headers)
        # An empty ProxyHandler disables *both* proxy environment variables and
        # the platform proxy settings; that is what makes "off" mean off.
        handlers = [] if proxy else [urllib.request.ProxyHandler({})]
        self._opener = urllib.request.build_opener(*handlers)
        self.requests = 0

    # -- internals ---------------------------------------------------------
    def _url(self, path: str, params: dict | None) -> str:
        if path.startswith(("http://", "https://")):
            url = path
        elif self.base:
            url = urllib.parse.urljoin(self.base.rstrip("/") + "/", path.lstrip("/"))
        else:
            raise ValueError("Session has no base URL and the path is not absolute")
        if params:
            sep = "&" if urllib.parse.urlsplit(url).query else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params)}"
        return url

    def _once(self, url: str, data: bytes | None, headers: dict[str, str]) -> Reply:
        req = urllib.request.Request(url, data=data, headers=headers)
        start = time.monotonic()
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read()
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return Reply(resp.status, body, hdrs, time.monotonic() - start, url)
        except urllib.error.HTTPError as exc:
            # A 500 with a SQL error in the body is the *answer* for an
            # error-based oracle, so it is returned rather than raised.
            body = exc.read() if hasattr(exc, "read") else b""
            hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
            return Reply(exc.code, body, hdrs, time.monotonic() - start, url)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            return Reply(
                0, b"", {}, time.monotonic() - start, url, error=f"{type(exc).__name__}: {exc}"
            )

    def _send(
        self, url: str, data: bytes | None, headers: dict[str, str] | None
    ) -> Reply:
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        last: Reply | None = None
        for attempt in range(self.retries + 1):
            self.requests += 1
            last = self._once(url, data, merged)
            # Retry only what is not an answer: no response at all, or a
            # server-side failure. 4xx means the request is wrong and will
            # stay wrong.
            if last.status and last.status < 500:
                return last
            if attempt < self.retries:
                time.sleep(self.backoff * (2**attempt))
        assert last is not None
        return last

    # -- public ------------------------------------------------------------
    def get(
        self,
        path: str = "",
        params: dict | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        return self._send(self._url(path, params), None, headers)

    def post(
        self,
        path: str = "",
        params: dict | None = None,
        *,
        data: dict | str | None = None,
        json_body=None,
        headers: dict[str, str] | None = None,
    ) -> Reply:
        url = self._url(path, params)
        body: bytes | None = None
        hdrs = dict(headers or {})
        if json_body is not None:
            body = _json.dumps(json_body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, str):
            body = data.encode()
        return self._send(url, body or b"", hdrs)

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        return None
