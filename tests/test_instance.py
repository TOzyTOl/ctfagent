"""The single-instance guard: a second server must not disturb a live run."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ctfagent import instance
from ctfagent.config import AppConfig


@pytest.fixture()
def cfg(tmp_path: Path) -> AppConfig:
    config = AppConfig(
        data_dir=tmp_path / "workspaces",
        db_path=tmp_path / "test.db",
        challenges_dir=tmp_path / "challenges",
        skills_dir=tmp_path / "skills",
    )
    config.ensure_dirs()
    return config


def test_no_instance_initially(cfg: AppConfig) -> None:
    assert instance.running_instance(cfg) is None


def test_acquire_then_visible(cfg: AppConfig) -> None:
    assert instance.acquire(cfg, "127.0.0.1", 8788) is None
    # The holder is this test process, so ask for the lock without the
    # self-exclusion that stops a server from seeing itself as a conflict.
    running = instance.running_instance(cfg, exclude_self=False)
    assert running is not None
    assert running["pid"] == os.getpid()
    assert running["port"] == 8788
    assert instance.running_instance(cfg) is None  # another *instance*: none
    instance.release(cfg)
    assert instance.running_instance(cfg, exclude_self=False) is None


def test_second_acquire_in_same_process_is_not_a_conflict(cfg: AppConfig) -> None:
    instance.acquire(cfg, "127.0.0.1", 8788)
    assert instance.acquire(cfg, "127.0.0.1", 9001) is None
    instance.release(cfg)


def test_stale_lock_is_ignored(cfg: AppConfig) -> None:
    path = instance.lock_path(cfg)
    # A pid that cannot be alive: beyond the platform's pid range.
    path.write_text(json.dumps({"pid": 999999999, "port": 8788}), encoding="utf-8")
    assert instance.running_instance(cfg) is None
    assert instance.acquire(cfg, "127.0.0.1", 8788) is None


def test_plain_pid_format_is_accepted(cfg: AppConfig) -> None:
    path = instance.lock_path(cfg)
    path.write_text(str(999999999), encoding="utf-8")
    assert instance.running_instance(cfg) is None  # parsed, but not alive


def test_garbage_lock_is_ignored(cfg: AppConfig) -> None:
    instance.lock_path(cfg).write_text("not a pid\n", encoding="utf-8")
    assert instance.running_instance(cfg) is None


def test_release_does_not_remove_foreign_lock(cfg: AppConfig) -> None:
    path = instance.lock_path(cfg)
    path.write_text(json.dumps({"pid": 999999999, "port": 8788}), encoding="utf-8")
    instance.release(cfg)
    assert path.exists(), "a lock owned by another pid must be left alone"


def test_a_finished_process_is_not_alive(cfg: AppConfig) -> None:
    """Windows keeps a pid's handle open briefly after exit; liveness must not.

    A stale lock left by a crashed server is the common case, and on Windows
    ``OpenProcess`` succeeds against a just-exited process, so the guard has to
    look at the exit code rather than the handle alone.
    """
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert instance._pid_alive(child.pid) is False


def test_message_names_the_url() -> None:
    text = instance.already_running_message(1234, "0.0.0.0", 8788)
    assert "1234" in text
    assert "127.0.0.1:8788" in text
