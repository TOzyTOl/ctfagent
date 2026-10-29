"""Single-instance guard for the web server.

Two CTFAGENT servers against one database is not a configuration error the user
can see: the second one's startup sweep marks the first one's live runs as
interrupted, and each process only knows about the runs it started. So the
launcher and ``ctfagent serve`` both refuse to start a second instance, and
they can point at the one already running.

The lock is a small JSON file next to the database holding the pid and the port.
It is advisory: a crash leaves a stale file behind, which is detected by
checking whether the pid is still alive.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path

from .config import AppConfig


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # PROCESS_QUERY_LIMITED_INFORMATION: enough to test liveness for a
        # process owned by the same user, without full access rights.
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            # OpenProcess also succeeds for a process that has exited but whose
            # handles have not all been closed yet, so the handle alone is not
            # proof of life. STILL_ACTIVE (259) is the exit code Windows reports
            # for a running process.
            code = ctypes.c_ulong(0)
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else — still a live process.
        return True
    return True


def lock_path(cfg: AppConfig) -> Path:
    return cfg.db_path.with_suffix(".pid")


def _read(cfg: AppConfig) -> dict[str, object] | None:
    try:
        raw = lock_path(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Older/plain format: just the pid.
    try:
        return {"pid": int(raw)}
    except ValueError:
        return None


def running_instance(cfg: AppConfig, *, exclude_self: bool = True) -> dict[str, object] | None:
    """Return ``{"pid": int, "port": int}`` for a live server, else None.

    ``exclude_self`` is what makes :func:`acquire` idempotent within one
    process: the server that holds the lock must not see itself as a conflict.
    Callers asking "who is serving right now?" (the launcher) leave it on, since
    they are a different process from the server.
    """
    data = _read(cfg)
    if not data:
        return None
    try:
        pid = int(data.get("pid", 0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not pid:
        return None
    if exclude_self and pid == os.getpid():
        return None
    if not _pid_alive(pid):
        return None
    try:
        port = int(data.get("port", cfg.port))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        port = cfg.port
    return {"pid": pid, "port": port}


def acquire(cfg: AppConfig, host: str, port: int) -> dict[str, object] | None:
    """Take the lock. Returns the running instance if one already holds it."""
    existing = running_instance(cfg)
    if existing:
        return existing
    try:
        path = lock_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": os.getpid(), "port": port, "host": host}), encoding="utf-8"
        )
    except OSError:
        # A read-only data directory should not stop the server: the guard is a
        # convenience, not a correctness requirement.
        pass
    return None


def release(cfg: AppConfig) -> None:
    path = lock_path(cfg)
    try:
        data = _read(cfg)
        if data and int(data.get("pid", 0)) == os.getpid():  # type: ignore[arg-type]
            path.unlink()
    except (OSError, TypeError, ValueError):
        pass


def already_running_message(pid: int, host: str, port: int) -> str:
    shown = host if host not in ("0.0.0.0", "::", "") else "127.0.0.1"
    return (
        f"CTFAGENT 已经在运行（pid {pid}）：http://{shown}:{port}/\n"
        f"  要重启，先停掉它；要另开一个实例，换一个数据库：CTFAGENT_DB_PATH=...\n"
    )
