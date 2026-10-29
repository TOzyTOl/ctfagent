"""Store: schema, event ordering, task lifecycle, flags."""

from __future__ import annotations

from pathlib import Path

from ctfagent.db import Store
from ctfagent.models import Challenge, FlagStatus, TaskStatus


def _challenge() -> Challenge:
    return Challenge(id=0, name="c", category="misc", description="d")


def test_schema_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    Store(path).close()
    store = Store(path)  # re-open must not fail on existing tables
    assert store.list_challenges() == []
    store.close()


def test_event_ids_are_monotonic(tmp_path: Path) -> None:
    store = Store(tmp_path / "x.db")
    challenge_id = store.add_challenge(_challenge())
    run = store.create_run(challenge_id, "solve")
    ids = [store.add_event(run.id, "test", {"i": i}).id for i in range(50)]
    assert ids == sorted(ids)
    assert len(set(ids)) == 50
    since = store.events_since(run.id, after_id=ids[10])
    assert [e.id for e in since] == ids[11:]
    store.close()


def test_task_lifecycle(tmp_path: Path) -> None:
    store = Store(tmp_path / "x.db")
    run = store.create_run(store.add_challenge(_challenge()), "solve")
    task = store.create_task(run.id, "web", "find the admin panel", "context")
    assert task.status == TaskStatus.QUEUED
    store.mark_task_started(task.id)
    assert store.get_task(task.id).status == TaskStatus.RUNNING
    store.add_task_usage(task.id, cost=0.25, turns=3)
    store.finish_task(task.id, TaskStatus.DONE, {"summary": "done"})
    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.DONE
    assert finished.cost_usd == 0.25
    assert finished.turns == 3
    assert finished.result["summary"] == "done"
    store.close()


def test_fail_running_tasks(tmp_path: Path) -> None:
    store = Store(tmp_path / "x.db")
    run = store.create_run(store.add_challenge(_challenge()), "solve")
    a = store.create_task(run.id, "web", "a", "")
    b = store.create_task(run.id, "crypto", "b", "")
    store.mark_task_started(a.id)
    assert store.fail_running_tasks(run.id, TaskStatus.STOPPED, "shutdown") == 2
    assert store.get_task(a.id).status == TaskStatus.STOPPED
    assert store.get_task(b.id).error == "shutdown"
    store.close()


def test_flag_dedupe_and_status(tmp_path: Path) -> None:
    store = Store(tmp_path / "x.db")
    run = store.create_run(store.add_challenge(_challenge()), "solve")
    flag = store.add_flag(run.id, None, "flag{a}", "src", "method", "high")
    assert flag is not None
    assert store.add_flag(run.id, None, "flag{a}", "src2", "method2", "low") is None
    store.set_flag_status(flag.id, FlagStatus.CONFIRMED, note="ok")
    stored = store.get_flag(flag.id)
    assert stored.status == FlagStatus.CONFIRMED
    assert stored.note == "ok"
    store.close()


def test_fail_unfinished_runs(tmp_path: Path) -> None:
    store = Store(tmp_path / "x.db")
    challenge_id = store.add_challenge(_challenge())
    a = store.create_run(challenge_id, "a")
    b = store.create_run(challenge_id, "b")
    store.set_run_status(a.id, "running")
    store.set_run_status(b.id, "solved", summary="done")
    assert store.fail_unfinished_runs("server restarted") == 1
    # A restart is not a failure: the run is interrupted, and the UI says why.
    assert store.get_run(a.id).status == "interrupted"
    assert store.get_run(a.id).stop_reason == "server restarted"
    assert store.get_run(b.id).status == "solved"
    store.close()


def test_messages_roundtrip(tmp_path: Path) -> None:
    store = Store(tmp_path / "x.db")
    run = store.create_run(store.add_challenge(_challenge()), "solve")
    store.add_message(run.id, "user", "hello")
    store.add_message(run.id, "assistant", "hi")
    messages = store.list_messages(run.id)
    assert [m["content"] for m in messages] == ["hello", "hi"]
    store.close()
