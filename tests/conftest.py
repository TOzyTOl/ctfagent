"""Shared fixtures for the offline test suite."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ctfagent.config import AppConfig  # noqa: E402


@pytest.fixture()
def cfg(tmp_path: Path) -> AppConfig:
    """A config rooted entirely in tmp_path: no writes outside the test."""
    config = AppConfig(
        data_dir=tmp_path / "workspaces",
        db_path=tmp_path / "test.db",
        challenges_dir=tmp_path / "challenges",
        skills_dir=ROOT / "plugins" / "ctf-skills" / "skills",
    )
    config.limits.max_sessions = 4
    config.limits.workers_per_run = 2
    config.limits.max_active_runs = 2
    config.limits.stall_timeout_s = 30
    config.limits.run_timeout_s = 120
    config.limits.run_budget_usd = 1.0
    config.ensure_dirs()
    return config


@pytest.fixture()
def fixtures_dir() -> Path:
    return ROOT / "tests" / "fixtures"


@pytest.fixture()
def store_and_bus(cfg):
    from ctfagent.db import Store
    from ctfagent.events import EventBus

    store = Store(cfg.db_path)
    bus = EventBus(store)
    yield store, bus
    store.close()
