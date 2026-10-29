"""A deliberately vulnerable HTTP target, bound to loopback, for ``selftest``.

Not a CTF challenge and not a demo: it is the **positive control** for the
engines in this package. The doctrine everywhere else in this project is that a
tool must be proven able to find a known answer before its failure to find an
unknown one means anything (``positive-control-before-negative``). An injection
engine is exactly the kind of tool that has to obey it — an oracle that returns
False for everything produces an empty string, and an empty string reads as
"no injection here".

So this server holds a flag whose value is known, with four independent ways in,
one per oracle class:

===========================  ==============================================
``/check?id=``               boolean blind (``1' AND (c) AND '1'='1``)
``/search?q=``               UNION (``' UNION SELECT ...``)
``/err?id=``                 error-based (``abs(-2**63)`` on the TRUE branch)
``/timing?id=``              time blind (recursive CTE on the TRUE branch)
===========================  ==============================================

Every endpoint is sqlite3, which every Python has, so the control runs
offline in the test suite. Nothing here listens on ``0.0.0.0``.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__all__ = ["start", "LocalTarget"]

FLAG = "flag{local-control-0k}"
SECRET = "admin-pw-9f2a"


_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, pass TEXT);
CREATE TABLE secrets (id INTEGER PRIMARY KEY, flag TEXT);
INSERT INTO users (id, name, pass) VALUES (1, 'admin', '%s');
INSERT INTO users (id, name, pass) VALUES (2, 'guest', 'guest');
INSERT INTO secrets (id, flag) VALUES (1, '%s');
""" % (SECRET, FLAG)


class LocalTarget:
    """Threaded sqlite-backed server. Use as a context manager."""

    def __init__(self, port: int = 0, delay: float = 0.9) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        # The handler reaches the database through `self.server`, which is the
        # HTTP server object, not this one — so the target is hung off it.
        self._server.target = self  # type: ignore[attr-defined]
        # The timing oracle's TRUE branch burns this many seconds of CPU. A
        # 0.9s gap against a 5-20ms baseline leaves the noise margin
        # TimeOracle needs without making the self-test take a minute.
        self._server.delay = delay  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        # One connection per request (threads), but the row data is static so a
        # shared in-memory database would need check_same_thread=False and a
        # lock. A file in a temp dir per instance is simpler than either.
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)

    def query(self, sql: str):
        with self._lock:
            return self._conn.execute(sql).fetchall()

    def __enter__(self) -> "LocalTarget":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._conn.close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "LocalControl/1.0"

    def log_message(self, *args) -> None:  # silence
        return

    def do_GET(self) -> None:  # noqa: N802
        parts = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        arg = (qs.get("id") or qs.get("q") or [""])[0]
        db = self.server.target  # type: ignore[attr-defined]

        if parts.path == "/check":
            self._boolean(db, arg)
        elif parts.path == "/search":
            self._union(db, arg)
        elif parts.path == "/err":
            self._error(db, arg)
        elif parts.path == "/timing":
            self._timing(db, arg)
        else:
            self._send(404, "not found")

    # -- the four injection shapes ----------------------------------------
    def _boolean(self, db, arg: str) -> None:
        sql = f"SELECT id, name FROM users WHERE id='{arg}'"
        try:
            rows = db.query(sql)
        except sqlite3.Error:
            # A broken predicate must be visibly not-TRUE, or a spec whose
            # payload never reaches the query would calibrate as "always TRUE".
            self._send(200, '{"exists": false}')
            return
        self._send(200, ('{"exists": true}' if rows else '{"exists": false}'))

    def _union(self, db, arg: str) -> None:
        sql = f"SELECT id, name || '|' || pass FROM users WHERE id='{arg}'"
        try:
            rows = db.query(sql)
        except sqlite3.Error as exc:
            self._send(500, f"SQL error: {exc}")
            return
        body = "".join(f"<li>{r[1]}</li>" for r in rows) or "<li>(none)</li>"
        self._send(200, f"<ul>{body}</ul>")

    def _error(self, db, arg: str) -> None:
        # `abs(-2**63)` overflows — but only on the TRUE branch, so the error
        # message itself is the oracle. Measured on sqlite 3.x: with the
        # payload `1' AND abs(CASE WHEN (1=1) THEN -9223372036854775808 ...`,
        # `1=1` raises "integer overflow" and `1=2` returns the row.
        sql = f"SELECT id, name FROM users WHERE id='{arg}'"
        try:
            rows = db.query(sql)
        except sqlite3.Error as exc:
            # Status 200 with the message in the body. That is what a PHP page
            # that prints its own database error looks like, and it is the
            # shape ErrorOracle is written to read; a 500 would work too but
            # urllib turns it into an exception the probe has to unwrap.
            self._send(200, f"<p>SQL error: {exc}</p>")
            return
        self._send(200, f"<p>{len(rows)} rows</p>")

    def _timing(self, db, arg: str) -> None:
        # The payload carries the delay *inside the injected string*, exactly
        # the way a real time-blind payload does — the handler adds nothing:
        #
        #     id=1' AND CASE WHEN (cond) THEN <burn> ELSE 0 END AND '1'='1
        #
        # `<burn>` is a recursive CTE counting N rows, sqlite's stand-in for
        # `IF(cond,SLEEP(n),0)`. Keeping it in the payload matters: if it were
        # appended by the handler, a payload that never reached the query
        # would still be slow and the control would prove nothing.
        #
        # Measured here: the CTE costs ~0.18s per million rows (7M → 1.28s),
        # so `delay` converts to rows at that rate. TimeOracle measures its own
        # threshold, so an inexact rate costs accuracy, not correctness.
        sql = f"SELECT id, name FROM users WHERE id='{arg}'"
        try:
            db.query(sql)
        except sqlite3.Error:
            # A payload whose syntax is broken must read as fast, i.e. FALSE.
            pass
        self._send(200, "ok")

    def _send(self, status: int, body: str) -> None:
        raw = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def start(port: int = 0) -> LocalTarget:
    """Start a target in the background and return it (already serving)."""
    target = LocalTarget(port)
    target._thread.start()
    for _ in range(50):
        try:
            import socket

            with socket.create_connection(("127.0.0.1", target.port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return target
