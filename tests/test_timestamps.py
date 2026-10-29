"""Every run and every work order shows when it started and how long it took.

Two clocks, because they answer different questions: a start time ("was this
the one I kicked off after lunch?") and an elapsed span ("has this worker been
stuck for two minutes or two hours?"). Both live in the rail and in the task
panel, and both have to keep counting while the page sits open — a value
rendered once and never updated reads as a stopped clock.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ctfagent.challenge import load_challenge
from ctfagent.server import _fmt_duration, _fmt_span, create_app


@pytest.fixture()
def client(cfg):
    app = create_app(cfg, scripted=True)
    with TestClient(app) as test_client:
        yield test_client


def _today_utc(hour: int, minute: int, second: int = 0) -> str:
    """A local wall time today, stored the way the DB stores it (UTC).

    The rail spells out the date for a run that did not start today, so a
    hardcoded calendar date renders ``10-20 10:10`` and the ``HH:MM`` shape
    these tests pin would never appear. Anchoring on *local* today keeps the
    render stable whatever the machine's zone or the current hour.
    """
    from datetime import datetime, timezone

    local = datetime.now().astimezone().replace(
        hour=hour, minute=minute, second=second, microsecond=0
    )
    return local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _run_with_clock(client, fixtures_dir: Path, *, status: str) -> int:
    """A run with fixed timestamps, so the rendered duration is assertable."""
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    store._exec(
        "UPDATE runs SET status=?, started_at=?, finished_at=? WHERE id=?",
        (status, _today_utc(2, 10), _today_utc(2, 47, 31), run.id),
    )
    return run.id


def test_a_finished_run_shows_its_start_time_and_its_elapsed_span(
    client, fixtures_dir: Path
) -> None:
    run_id = _run_with_clock(client, fixtures_dir, status="solved")
    rail = client.get("/api/rail").text

    assert f'data-run-id="{run_id}"' in rail
    # The clock is the local wall time of the stored UTC start; the exact hour
    # depends on the machine's zone, so assert the shape, not the value.
    assert re.search(r'class="rail-clock"[^>]*>\d\d:\d\d<', rail)
    # The span is derived, and this one is fixed: 37m31s.
    assert 'class="rail-elapsed"' in rail
    assert "37分31秒" in rail
    assert f'data-elapsed-from="{_today_utc(2, 10)}"' in rail
    assert f'data-elapsed-to="{_today_utc(2, 47, 31)}"' in rail


def test_a_queued_run_has_a_start_time_but_no_elapsed_span(
    client, fixtures_dir: Path
) -> None:
    """Nothing has run yet, so there is nothing to count.

    Showing ``0秒`` here would be a lie about a run that has been queued for
    ten minutes; the creation time is the honest "when did this arrive".
    """
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    rail = client.get("/api/rail").text

    assert 'class="rail-clock"' in rail
    # The tooltip says why there is no span, instead of leaving the row silent.
    assert "还没开始" in rail
    assert 'class="rail-elapsed"' not in rail


def test_a_work_order_shows_when_it_started_and_how_long_it_ran(
    client, fixtures_dir: Path
) -> None:
    store = client.app.state.store
    run_id = _run_with_clock(client, fixtures_dir, status="running")
    task = store.create_task(run_id, "reverse", "look at the binary", "")
    store._exec(
        "UPDATE tasks SET status='done', started_at=?, finished_at=? WHERE id=?",
        (_today_utc(2, 12), _today_utc(2, 30, 5), task.id),
    )

    page = client.get(f"/runs/{run_id}").text
    # Slice to the task panel: the rail above also carries a clock and a span,
    # and a page-wide `in` check would pass on those alone.
    panel = page[page.index('<div class="tasks">') : page.index('data-spane="flags"')]
    assert 'class="task-clock"' in panel
    assert "18分05秒" in panel
    assert f'data-elapsed-from="{_today_utc(2, 12)}"' in panel
    assert f'data-elapsed-to="{_today_utc(2, 30, 5)}"' in panel


def test_a_still_running_span_has_an_open_end(client, fixtures_dir: Path) -> None:
    """An empty ``to`` is what tells the browser to keep counting up."""
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")
    store._exec(
        "UPDATE runs SET status='running', started_at=datetime('now','-90 seconds') WHERE id=?",
        (run.id,),
    )
    rail = client.get("/api/rail").text

    assert 'data-elapsed-to=""' in rail
    # The server's first render is already a real number, not a placeholder.
    assert re.search(r'class="rail-elapsed"[^>]*>\d+分\d\d秒<', rail)
    assert "仍在进行" in rail


def test_a_reopened_run_clears_its_finish_stamp(client, fixtures_dir: Path) -> None:
    """An operator message resumes a closed run; the clock must restart.

    Live symptom this guards: the rail kept the finish stamp from the moment
    the run was stopped, so a run that had been working for another hour read
    as finished in four minutes.
    """
    store = client.app.state.store
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    run = store.create_run(challenge_id, "solve")

    store.set_run_status(run.id, "running")
    store.set_run_status(run.id, "solved")
    closed = store.get_run(run.id)
    assert closed.finished_at, "a terminal status must stamp the finish time"

    store.set_run_status(run.id, "running")
    reopened = store.get_run(run.id)
    assert reopened.finished_at == ""
    assert reopened.started_at == closed.started_at, "reopening keeps the original start"


def test_the_span_tooltip_names_both_ends() -> None:
    assert _fmt_span("2026-10-20 02:10:00", "2026-10-20 02:47:31").endswith("→ 10:47")
    assert "仍在进行" in _fmt_span("2026-10-20 02:10:00", "")
    assert _fmt_span("", "") == ""


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0秒"), (42, "42秒"), (59, "59秒"), (60, "1分00秒"), (725, "12分05秒"), (4320, "1小时12分")],
)
def test_duration_format(seconds: float, expected: str) -> None:
    assert _fmt_duration(seconds) == expected
