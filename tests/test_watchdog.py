"""The external watchdog's reap and revive decisions, against a real Store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.models import TaskStatus
from ctfagent.watchdog import run_once


def _running_run(store, fixtures_dir: Path, *, started_minutes_ago: float) -> int:
    from datetime import datetime, timedelta, timezone

    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run_id = store.create_run(challenge_id, "solve").id
    store.set_run_status(run_id, "running")
    past = (datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    # A backdated event sets the silence clock the same way the live stream would.
    store.add_event(run_id, "run.started", {"run_id": run_id})
    store._conn.execute(
        "UPDATE events SET created_at=? WHERE run_id=? AND kind='run.started'",
        (past, run_id),
    )
    store._conn.commit()
    return run_id


def test_a_silent_running_run_is_reaped_and_interrupted(
    cfg, fixtures_dir: Path, store_and_bus, tmp_path: Path
) -> None:
    store, _ = store_and_bus
    run_id = _running_run(store, fixtures_dir, started_minutes_ago=60)
    ghost = store.create_task(run_id, "crypto", "sweep", "")
    store.mark_task_started(ghost.id)

    report = run_once(
        cfg.db_path, "http://127.0.0.1:1", silence_s=2700,
        state_path=tmp_path / "wd.json", revive=False,
    )

    assert report["reaped"] and report["reaped"][0]["run_id"] == run_id
    assert store.get_task(ghost.id).status == TaskStatus.STOPPED
    row = store.get_run(run_id)
    assert row.status == "interrupted"
    assert "看门狗(外部)" in (row.stop_reason or "")


def test_a_fresh_run_and_a_needs_input_run_are_left_alone(
    cfg, fixtures_dir: Path, store_and_bus, tmp_path: Path
) -> None:
    store, _ = store_and_bus
    fresh = _running_run(store, fixtures_dir, started_minutes_ago=1)
    waiting = store.create_run(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")), "solve").id
    store.set_run_status(waiting, "needs_input")

    report = run_once(cfg.db_path, "http://127.0.0.1:1", silence_s=2700,
                      state_path=tmp_path / "wd.json", revive=False)

    assert report["reaped"] == []
    assert store.get_run(fresh).status == "running"
    assert store.get_run(waiting).status == "needs_input"


def _silence_all(store: Store, run_id: int, minutes: float) -> None:
    """Backdate every event of the run — the next silence window starts now."""
    from datetime import datetime, timedelta, timezone

    past = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    store._conn.execute("UPDATE events SET created_at=? WHERE run_id=?", (past, run_id))
    store._conn.commit()


def test_revive_posts_one_nudge_and_honors_the_cap(
    cfg, fixtures_dir: Path, store_and_bus, tmp_path: Path, monkeypatch
) -> None:
    store, _ = store_and_bus
    run_id = _running_run(store, fixtures_dir, started_minutes_ago=60)
    store.set_main_session(run_id, "sess-main", 0.5)

    posted: list[tuple[int, str]] = []
    monkeypatch.setattr("ctfagent.watchdog.post_chat", fake_post := (
        lambda api_base, run_id_, text, timeout=30.0: posted.append((run_id_, text)) or True
    ))

    state = tmp_path / "wd.json"
    report = run_once(cfg.db_path, "http://127.0.0.1:8788", silence_s=2700, state_path=state)
    assert len(posted) == 1 and report["revived"]
    assert "看门狗自动消息" in posted[0][1]

    # The run goes silent again (the reap's own events backdated too — they
    # legitimately reset the silence clock, which is why the helper exists).
    # The state file's cap (2 per run) has room, so a second nudge goes out —
    # and then the cap closes.
    store.set_run_status(run_id, "running")
    _silence_all(store, run_id, 60)
    report = run_once(cfg.db_path, "http://127.0.0.1:8788", silence_s=2700, state_path=state)
    assert len(posted) == 2

    store.set_run_status(run_id, "running")
    _silence_all(store, run_id, 60)
    report = run_once(cfg.db_path, "http://127.0.0.1:8788", silence_s=2700, state_path=state)
    assert len(posted) == 2
    assert report["skipped"][0]["why"] == "复活次数用尽"
