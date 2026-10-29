"""Supersession: a conclusion that overturns another names it.

Run 18 lost its budget to this. Four reverse workers, one question, and a board
that showed both answers with equal standing:

    #457 程序的 ecAdd/ecMul 与标准 SM2 群运算一致
    #465 程序的 ecAdd/ecMul 与标准 SM2 群运算不一致
    #467 其实一致（#465 是我 gdb 打错地址）
    #476 裁决 #90 vs #92：不一致

Each worker that read the board had to re-run the experiment to find out which
line was current, and paid a full session for it. The fix is not a smarter
model — it is letting a record say which record it overturns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.backend import ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus

CLAIM = "程序的点运算与标准 SM2 群运算一致，可以按标准算法求解"


@pytest.fixture()
def run(cfg, fixtures_dir: Path, store_and_bus):
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    return manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))


def test_a_superseded_fact_is_marked_on_the_board(run) -> None:
    old = run.record_fact(
        None, "insight", CLAIM, {"command": "gdb -ex ...", "output": "2G == 2G"}, "observed"
    )
    new = run.record_fact(
        None,
        "insight",
        "程序的点运算与标准 SM2 群运算**不一致**：mulPoint 在 k=2 时输出不在曲线上",
        {"command": "unicorn harness", "output": "mismatch at k=2"},
        "observed",
        supersedes=old,
    )

    board = run.blackboard_digest()
    assert f"【已被 #{new} 推翻，不要据此行动】" in board
    assert f"取代 #{old}" in board
    # The old record is kept: what was believed and why it fell is how the next
    # worker avoids repeating it.
    assert CLAIM in board


def test_a_superseding_fact_is_not_swallowed_as_a_restatement(run) -> None:
    """Dropping it as a duplicate would leave the board showing what it corrects."""
    old = run.record_fact(
        None, "insight", CLAIM, {"command": "a", "output": "b"}, "observed"
    )
    new = run.record_fact(
        None, "insight", CLAIM, {"command": "a", "output": "b"}, "observed", supersedes=old
    )
    assert new != old
    assert f"已被 #{new} 推翻" in run.blackboard_digest()


def test_a_plain_restatement_is_still_merged(run) -> None:
    first = run.record_fact(
        None, "insight", CLAIM, {"command": "a", "output": "b"}, "observed"
    )
    again = run.record_fact(
        None, "insight", CLAIM, {"command": "a", "output": "b"}, "observed"
    )
    assert again == first
    assert "已被 #" not in run.blackboard_digest()


def test_the_column_is_added_to_an_existing_database(tmp_path: Path) -> None:
    """Runs predate the column; the history is not regenerable, so migrate."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            task_id INTEGER,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '{}',
            basis TEXT NOT NULL DEFAULT '',
            resolves INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()

    store = Store(path)
    try:
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(facts)")}
    finally:
        store.close()
    assert "supersedes" in columns
