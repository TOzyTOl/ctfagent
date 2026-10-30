"""SQLite persistence: challenges, runs, tasks, facts, flags, events, messages.

One file, WAL mode, a single connection guarded by a lock. The orchestrator runs
on one asyncio loop and SQLite calls are sub-millisecond, so a lock is simpler
and safer than a thread pool.

The ``events`` table is the single observability stream (ARTEX's ``activity``
table): every state change appends a row with a monotonic id, and SSE clients
resume from their last id. The in-process pub/sub only wakes subscribers; the
table is the source of truth, so a dropped notification is never lost data.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Case,
    Challenge,
    Competition,
    Event,
    Fact,
    Flag,
    Folder,
    Provider,
    Run,
    Submission,
    Task,
    dumps,
    loads,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS challenges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT 'unknown',
    description  TEXT NOT NULL DEFAULT '',
    files        TEXT NOT NULL DEFAULT '[]',
    remote       TEXT NOT NULL DEFAULT '',
    flag_format  TEXT NOT NULL DEFAULT '',
    hints        TEXT NOT NULL DEFAULT '[]',
    time_limit_s INTEGER NOT NULL DEFAULT 3600,
    source_dir   TEXT NOT NULL DEFAULT '',
    -- set when the challenge was pulled from a competition platform; the pair
    -- (competition_id, external_id) is what a flag submission addresses.
    competition_id INTEGER,
    external_id    TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL REFERENCES challenges(id),
    objective    TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'queued',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT NOT NULL DEFAULT '',
    finished_at  TEXT NOT NULL DEFAULT '',
    cost_usd     REAL NOT NULL DEFAULT 0,
    summary      TEXT NOT NULL DEFAULT '',
    workspace    TEXT NOT NULL DEFAULT '',
    stop_reason  TEXT NOT NULL DEFAULT '',
    -- The main agent's CLI session, so an operator message can resume the
    -- conversation after a restart or after the run closed. Worker sessions
    -- already live on ``tasks.session_id``; the lead had nowhere to record its
    -- own, which made a finished run permanently deaf.
    main_session_id TEXT NOT NULL DEFAULT '',
    -- The cumulative cost the CLI reported for that session. A resumed session
    -- keeps counting from it, so the first result after a resume must not be
    -- booked as if the whole transcript were new spend.
    main_cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status);

-- A folder groups runs: one competition, one practice batch. Deliberately not
-- the ``competitions`` table — that one is a remote platform account with a
-- URL and a token, and most runs are local challenges with neither.
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    objective   TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL DEFAULT '',
    cost_usd    REAL NOT NULL DEFAULT 0,
    turns       INTEGER NOT NULL DEFAULT 0,
    result      TEXT NOT NULL DEFAULT '{}',
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tasks_run ON tasks(run_id, status);

CREATE TABLE IF NOT EXISTS facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id    INTEGER,
    kind       TEXT NOT NULL,
    content    TEXT NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '{}',
    basis      TEXT NOT NULL DEFAULT '',
    resolves   INTEGER,
    supersedes INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS facts_run ON facts(run_id, id);

CREATE TABLE IF NOT EXISTS flags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id    INTEGER,
    value      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    method     TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    status     TEXT NOT NULL DEFAULT 'candidate',
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS flags_run ON flags(run_id, status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL,
    task_id    INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
-- The rail asks "which runs have a distilled note" every few seconds, and the
-- event log is the only place that answer lives. Without this the poll scans
-- every event row, which is the whole history.
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, run_id);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,          -- 'user' | 'assistant'
    content     TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS messages_run ON messages(run_id, id);

-- A competition is one remote CTF platform account. Challenges synced from it
-- carry ``competition_id`` + ``external_id``; that pair is what a submission
-- needs, so a flag can be sent back without re-fetching the challenge list.
CREATE TABLE IF NOT EXISTS competitions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    platform       TEXT NOT NULL DEFAULT 'ctfd',
    base_url       TEXT NOT NULL DEFAULT '',
    token          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_synced_at TEXT NOT NULL DEFAULT '',
    last_sync_note TEXT NOT NULL DEFAULT ''
);

-- Every attempt to submit a confirmed flag. Kept as a log rather than a column
-- on ``flags`` because a platform can answer "already solved", be unreachable,
-- and then succeed — the history is what explains the current state.
CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id        INTEGER NOT NULL REFERENCES flags(id) ON DELETE CASCADE,
    competition_id INTEGER NOT NULL,
    external_id    TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',   -- correct | incorrect | already_solved | error
    message        TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS submissions_flag ON submissions(flag_id, id);

-- Distilled experience: one row per finished run that has something to teach.
-- Kept out of ``facts`` because it outlives the run: facts are the blackboard of
-- one challenge, a case is what the next challenge of the same shape should try
-- first. The flag literal is never stored here — only how it was derived.
CREATE TABLE IF NOT EXISTS cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    challenge_id  INTEGER NOT NULL REFERENCES challenges(id) ON DELETE CASCADE,
    name          TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT 'unknown',
    outcome       TEXT NOT NULL DEFAULT 'failed',   -- solved | failed
    route         TEXT NOT NULL DEFAULT '[]',
    derivation    TEXT NOT NULL DEFAULT '',
    dead_ends     TEXT NOT NULL DEFAULT '[]',
    open_unknowns TEXT NOT NULL DEFAULT '[]',
    techniques    TEXT NOT NULL DEFAULT '[]',
    cost_usd      REAL NOT NULL DEFAULT 0,
    elapsed_s     INTEGER NOT NULL DEFAULT 0,
    used_count    INTEGER NOT NULL DEFAULT 0,
    help_count    INTEGER NOT NULL DEFAULT 0,
    harm_count    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS cases_category ON cases(category, outcome);

-- Model endpoints the operator has tried. One row is active at a time
-- (``app_settings.active_provider``); switching rewrites that pointer, and
-- ``settings.apply_provider`` pushes it into the running config so the next
-- dispatch uses it without a restart.
--
-- The token is stored in this file, exactly as it would be in config.toml.
-- This table exists because the old arrangement — one endpoint in a file plus an
-- environment variable that silently overrides it — made "which gateway am I
-- actually on" a question you answered by running ``ctfagent doctor``.
CREATE TABLE IF NOT EXISTS providers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL DEFAULT '',
    base_url        TEXT NOT NULL DEFAULT '',
    auth_token      TEXT NOT NULL DEFAULT '',
    api_key         TEXT NOT NULL DEFAULT '',
    api_format      TEXT NOT NULL DEFAULT 'anthropic',
    models          TEXT NOT NULL DEFAULT '[]',
    last_probe      TEXT NOT NULL DEFAULT '',
    last_probe_note TEXT NOT NULL DEFAULT '',
    last_probed_at  TEXT NOT NULL DEFAULT '',
    imported_from   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Small key/value store for UI state that is not worth a column: which provider
-- is active, and the per-role model assignment made on the credentials page
-- (``models.roles`` JSON) so it survives a restart through the same path the
-- page writes.
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

TERMINAL_RUN_STATES = ("solved", "failed", "stopped")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database already existed.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing file, so a new
        column has to be added explicitly. Only additive changes belong here —
        nothing in this table is ever dropped, because the operator's run
        history is the one thing that cannot be regenerated.
        """
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(challenges)")}
        for column, ddl in (
            ("competition_id", "ALTER TABLE challenges ADD COLUMN competition_id INTEGER"),
            ("external_id", "ALTER TABLE challenges ADD COLUMN external_id TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in have:
                self._conn.execute(ddl)

        # A message carries the files the operator sent with it, so the chat can
        # render them as cards instead of a sentence naming them.
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(messages)")}
        if "attachments" not in have:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
            )

        # Facts gained a grounding level and a "this closes unknown #N" pointer.
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(facts)")}
        if "basis" not in have:
            self._conn.execute("ALTER TABLE facts ADD COLUMN basis TEXT NOT NULL DEFAULT ''")
        if "resolves" not in have:
            self._conn.execute("ALTER TABLE facts ADD COLUMN resolves INTEGER")
        # A conclusion that overturns an earlier one names it, so the board can
        # show which of two conflicting records is the current one.
        if "supersedes" not in have:
            self._conn.execute("ALTER TABLE facts ADD COLUMN supersedes INTEGER")

        # A card can now carry the questions its run left open, and record how
        # often it was present on a winning versus a losing run.
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(cases)")}
        for column, ddl in (
            ("open_unknowns", "ALTER TABLE cases ADD COLUMN open_unknowns TEXT NOT NULL DEFAULT '[]'"),
            ("help_count", "ALTER TABLE cases ADD COLUMN help_count INTEGER NOT NULL DEFAULT 0"),
            ("harm_count", "ALTER TABLE cases ADD COLUMN harm_count INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in have:
                self._conn.execute(ddl)

        # The lead's session id and its cumulative CLI spend, so chat can resume
        # a run whose process is gone (see ``runs.main_session_id`` above).
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)")}
        if "main_session_id" not in have:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN main_session_id TEXT NOT NULL DEFAULT ''"
            )
        if "main_cost_usd" not in have:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN main_cost_usd REAL NOT NULL DEFAULT 0"
            )
        # Which case cards this run was shown. Without it a card's ``used_count``
        # counts loads that nobody can attribute to an outcome, and the library
        # has no way to learn that one of its cards keeps riding along on losing
        # runs.
        if "case_ids" not in have:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN case_ids TEXT NOT NULL DEFAULT '[]'"
            )
        # A run can be closed more than once (a reopened run, a late flag
        # confirmation). The outcome it charges to its cards must be booked once.
        if "cases_credited" not in have:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN cases_credited INTEGER NOT NULL DEFAULT 0"
            )
        # Technique notes are files, not rows, so their help/harm counters and
        # the record of which were listed live in their own places: a JSON map
        # of counters inside the note, and one column here for the set the run
        # was shown. Charged once per run for the same reason as the cards.
        if "note_paths" not in have:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN note_paths TEXT NOT NULL DEFAULT '[]'"
            )
        if "notes_credited" not in have:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN notes_credited INTEGER NOT NULL DEFAULT 0"
            )
        # Which folder the operator filed this run under (see ``folders`` above).
        # Existing runs stay unfiled: an operator's history cannot be guessed
        # into groups, and "未分类" is an honest place for it to land.
        if "folder_id" not in have:
            self._conn.execute("ALTER TABLE runs ADD COLUMN folder_id INTEGER")

        # Every rail query filters or sorts runs by folder, and the rail is
        # rebuilt on a 5-second poll. Without this index each poll scans the
        # runs table once per folder once runs are filed.
        self._conn.execute("CREATE INDEX IF NOT EXISTS runs_folder ON runs(folder_id, id)")
        # Two folders with the same name are two identical headings in the rail,
        # the only place they are listed, so the name has to be unique. The
        # index is created here rather than in SCHEMA because a database that
        # already holds duplicates would refuse to open, and the operator wants
        # the app up more than it wants the constraint.
        self._ensure_unique_folder_names()

        # Runs started before the column existed still have a resumable
        # transcript: the session id was recorded in every ``init`` event. Once
        # found, an estimate of that session's spend is the run total minus what
        # its tasks were billed, so a resume does not book the whole transcript
        # as fresh spend.
        self._conn.execute(
            """UPDATE runs SET main_session_id = COALESCE((
                   SELECT json_extract(e.payload, '$.session_id') FROM events e
                    WHERE e.run_id = runs.id AND e.kind = 'agent.event'
                      AND json_extract(e.payload, '$.role') = 'main'
                      AND json_extract(e.payload, '$.session_id') <> ''
                    ORDER BY e.id DESC LIMIT 1
               ), '')
               WHERE main_session_id = ''"""
        )
        self._conn.execute(
            """UPDATE runs
                  SET main_cost_usd = MAX(0, cost_usd - COALESCE((
                      SELECT SUM(cost_usd) FROM tasks WHERE tasks.run_id = runs.id
                  ), 0))
                WHERE main_session_id <> '' AND main_cost_usd = 0"""
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _ensure_unique_folder_names(self) -> None:
        """Make folder names unique, for databases that already have clones.

        Duplicates can only be resolved by a human: the runs under the two
        headings are different work, and merging them would file runs under a
        folder they were never in. So each duplicate after the first is renamed
        apart (「A (2)」) — nothing is deleted and no run moves.

        Candidate suffixes are checked against *every* name that existed
        before any renaming, so a folder the operator deliberately called
        「A (2)」 keeps that name instead of being pushed aside by the repair of
        its neighbour.
        """
        rows = self._conn.execute("SELECT id, name FROM folders ORDER BY id").fetchall()
        taken = {row["name"] for row in rows}
        seen: set[str] = set()
        for row in rows:
            name = row["name"]
            if name not in seen:
                seen.add(name)
                continue
            n = 2
            while f"{name} ({n})" in taken:
                n += 1
            candidate = f"{name} ({n})"
            taken.add(candidate)
            seen.add(candidate)
            self._conn.execute("UPDATE folders SET name=? WHERE id=?", (candidate, row["id"]))
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS folders_name ON folders(name)")
    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def _row(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    # --------------------------------------------------------------- challenges
    def add_challenge(self, ch: Challenge) -> int:
        cur = self._exec(
            """INSERT INTO challenges
               (name, category, description, files, remote, flag_format, hints,
                time_limit_s, source_dir, competition_id, external_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ch.name,
                ch.category,
                ch.description,
                dumps(ch.files),
                ch.remote,
                ch.flag_format,
                dumps(ch.hints),
                ch.time_limit_s,
                ch.source_dir,
                ch.competition_id,
                ch.external_id,
            ),
        )
        return int(cur.lastrowid)

    def find_platform_challenge(self, competition_id: int, external_id: str) -> Challenge | None:
        """The local challenge already synced from this platform entry, if any.

        Sync is re-runnable: a second sync must update the challenge the first
        one created rather than pile up duplicates with the same name.
        """
        row = self._row(
            "SELECT * FROM challenges WHERE competition_id=? AND external_id=?",
            (competition_id, str(external_id)),
        )
        return self._challenge(row) if row else None

    def update_challenge_from_platform(
        self,
        challenge_id: int,
        *,
        description: str,
        remote: str,
        hints: list[str],
        name: str = "",
        category: str = "",
    ) -> None:
        """Refresh the fields a platform can change; keep locally added files.

        ``files`` is deliberately untouched: the operator may have uploaded
        extra attachments, and a re-sync must not erase them. A blank
        ``name``/``category`` keeps the stored value — a platform omitting a
        field is not an instruction to erase it.
        """
        self._exec(
            """UPDATE challenges
               SET description=?, remote=?, hints=?,
                   name=CASE WHEN ?<>'' THEN ? ELSE name END,
                   category=CASE WHEN ?<>'' THEN ? ELSE category END
               WHERE id=?""",
            (description, remote, dumps(hints), name, name, category, category, challenge_id),
        )

    def get_challenge(self, challenge_id: int) -> Challenge | None:
        row = self._row("SELECT * FROM challenges WHERE id=?", (challenge_id,))
        return self._challenge(row) if row else None

    def list_challenges(self) -> list[Challenge]:
        return [self._challenge(r) for r in self._rows("SELECT * FROM challenges ORDER BY id DESC")]

    def set_challenge_files(self, challenge_id: int, files: list[str]) -> None:
        """Keep ``challenge.files`` in sync when attachments are added later.

        The file list is injected into every agent prompt, so it must reflect
        what is actually in ``attachments/`` — an upload the model is not told
        about is an upload it will not look at.
        """
        self._exec("UPDATE challenges SET files=? WHERE id=?", (dumps(files), challenge_id))

    def set_challenge_source_dir(self, challenge_id: int, source_dir: str) -> None:
        """Remember where a synced challenge's attachments live on disk.

        ``prepare`` copies this directory into the run's ``attachments/``; without
        it a challenge synced from a platform would start a run with no files.
        """
        self._exec("UPDATE challenges SET source_dir=? WHERE id=?", (source_dir, challenge_id))

    def set_challenge_category(self, challenge_id: int, category: str) -> bool:
        """Record the direction triage concluded for this challenge.

        Guarded to rows still marked ``unknown``/empty: a category that came
        from the platform (CTFd sync) or the operator outranks a model's
        guess, and a later run must not flip a direction somebody already
        decided. Returns whether the row changed, so the caller can skip the
        in-memory update and the event when it did not.
        """
        cur = self._exec(
            "UPDATE challenges SET category=? WHERE id=? AND (category='' OR category='unknown')",
            (category, challenge_id),
        )
        return cur.rowcount > 0

    def rename_challenge(self, challenge_id: int, name: str) -> bool:
        """Replace a pasted-in blob title with the normalized one.

        Guarded to the same "blob" shape the caller decided on (a URL or a
        whole pasted paragraph): a challenge that already has a short, human
        name — from ``challenge.yaml`` or the operator — is never touched,
        and the SQL guard means a stale caller cannot rename a challenge
        somebody has since renamed by hand. Returns whether the row changed.
        """
        cur = self._exec(
            "UPDATE challenges SET name=? WHERE id=? AND (name LIKE '%://%' OR length(name) > 40)",
            (name, challenge_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def _challenge(row: sqlite3.Row) -> Challenge:
        return Challenge(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            description=row["description"],
            files=loads(row["files"], []),
            remote=row["remote"],
            flag_format=row["flag_format"],
            hints=loads(row["hints"], []),
            time_limit_s=row["time_limit_s"],
            source_dir=row["source_dir"],
            created_at=row["created_at"],
            competition_id=row["competition_id"],
            external_id=row["external_id"],
        )

    # --------------------------------------------------------------------- runs
    def create_run(
        self, challenge_id: int, objective: str, workspace: str = "", folder_id: int | None = None
    ) -> Run:
        cur = self._exec(
            "INSERT INTO runs (challenge_id, objective, workspace, folder_id) VALUES (?,?,?,?)",
            (challenge_id, objective, workspace, folder_id),
        )
        run = self.get_run(int(cur.lastrowid))
        assert run is not None
        return run

    def get_run(self, run_id: int) -> Run | None:
        row = self._row("SELECT * FROM runs WHERE id=?", (run_id,))
        return self._run(row) if row else None

    def list_runs(self, limit: int = 100) -> list[Run]:
        rows = self._rows("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [self._run(r) for r in rows]

    def list_runs_grouped(self, per_folder: int = 25, ceiling: int = 400) -> list[Run]:
        """Runs for the rail, taken per folder rather than off the newest N overall.

        A plain ``LIMIT 120`` is wrong once runs are filed: a competition from
        last month falls outside the window and its folder renders as empty,
        which is indistinguishable from never having run anything there. The
        rail exists to find old work, so every folder keeps its own newest slice.

        Unfiled runs get their own slice rather than the leftover: filing is
        optional, and an operator who never makes a folder must still see enough
        of their history to want to.

        One ``LIMIT`` per folder, not a window function over the whole table:
        measured at 6000 runs, the per-folder queries take 1.4 ms against 13 ms
        for a ``ROW_NUMBER()`` scan, and neither grows with the number of runs
        the rail does not show.

        ``ceiling`` caps the whole rail so a page of folders cannot turn one
        5-second poll into a table scan. The per-folder minimum of 3 wins over
        it in the pathological case, because a folder that looks empty when it
        is not is a worse bug than a slow poll.
        """
        folder_ids = [f.id for f in self.list_folders()]
        per = max(3, min(per_folder, ceiling // max(1, len(folder_ids) + 1)))
        # A run can name a folder that no longer exists only if the process died
        # between the two statements of ``delete_folder``. It is still queried
        # here so the rail can show it under 未分类 rather than count it in the
        # totals and never list it.
        known = set(folder_ids)
        orphans = [
            fid for fid in self.count_runs_per_folder() if fid is not None and fid not in known
        ]
        rows: list[sqlite3.Row] = []
        for folder_id in folder_ids + orphans:
            rows.extend(
                self._rows(
                    "SELECT * FROM runs WHERE folder_id=? ORDER BY id DESC LIMIT ?",
                    (folder_id, per),
                )
            )
        rows.extend(
            self._rows(
                "SELECT * FROM runs WHERE folder_id IS NULL ORDER BY id DESC LIMIT ?", (per,)
            )
        )
        # Newest first across the whole thing; the rail re-sorts by liveness
        # afterwards.
        seen: set[int] = set()
        out: list[Run] = []
        for row in sorted(rows, key=lambda r: r["id"], reverse=True):
            if int(row["id"]) in seen:
                continue
            seen.add(int(row["id"]))
            out.append(self._run(row))
        return out

    def run_totals(self) -> dict[str, Any]:
        """Whole-history totals for the rail's footer.

        Computed in SQL over every run, not over the rail's window: the footer
        is the one place that answers "how much have I done and spent", and a
        per-folder window would quietly shrink those numbers as folders fill.
        """
        row = self._row(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(cost_usd), 0) AS cost,
                      COALESCE(SUM(status IN ('running', 'needs_input', 'queued')), 0) AS active
                 FROM runs"""
        )
        captured = self._row(
            """SELECT COUNT(DISTINCT run_id) AS n FROM flags WHERE status='confirmed'"""
        )
        return {
            "total": int(row["total"]) if row else 0,
            "cost": float(row["cost"]) if row else 0.0,
            "active": int(row["active"]) if row else 0,
            "captured": int(captured["n"]) if captured else 0,
        }

    def count_runs_per_folder(self) -> dict[int | None, int]:
        """Run totals by folder, so a group header can say "25 条中的 3 条"."""
        out: dict[int | None, int] = {}
        for row in self._rows("SELECT folder_id, COUNT(*) AS n FROM runs GROUP BY folder_id"):
            out[row["folder_id"]] = int(row["n"])
        return out

    def list_runs_for_challenge(self, challenge_id: int) -> list[Run]:
        rows = self._rows(
            "SELECT * FROM runs WHERE challenge_id=? ORDER BY id DESC", (challenge_id,)
        )
        return [self._run(r) for r in rows]

    @staticmethod
    def _run(row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            challenge_id=row["challenge_id"],
            objective=row["objective"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            cost_usd=row["cost_usd"],
            summary=row["summary"],
            workspace=row["workspace"],
            stop_reason=row["stop_reason"],
            main_session_id=row["main_session_id"],
            main_cost_usd=row["main_cost_usd"],
            cases_credited=row["cases_credited"],
            notes_credited=row["notes_credited"],
            folder_id=row["folder_id"],
        )

    def set_run_status(
        self, run_id: int, status: str, *, summary: str = "", reason: str = ""
    ) -> None:
        terminal = 1 if status in TERMINAL_RUN_STATES else 0
        # A run can leave a terminal state: an operator message on a closed run
        # resumes its session (``RunManager.revive``). The old finish stamp has
        # to go with it, or the elapsed clock stays frozen at the moment it was
        # stopped and the rail reports a run that has been going for hours as
        # finished in minutes.
        reopened = 1 if status == "running" else 0
        self._exec(
            """UPDATE runs SET status=?,
                 started_at=CASE WHEN started_at='' AND ?='running' THEN datetime('now') ELSE started_at END,
                 finished_at=CASE WHEN ? THEN datetime('now')
                                  WHEN ? THEN ''
                                  ELSE finished_at END,
                 summary=CASE WHEN ?<>'' THEN ? ELSE summary END,
                 stop_reason=CASE WHEN ?<>'' THEN ? ELSE stop_reason END
               WHERE id=?""",
            (status, status, terminal, reopened, summary, summary, reason, reason, run_id),
        )

    def set_run_workspace(self, run_id: int, workspace: str) -> None:
        self._exec("UPDATE runs SET workspace=? WHERE id=?", (workspace, run_id))

    def add_run_cost(self, run_id: int, cost: float) -> None:
        self._exec("UPDATE runs SET cost_usd=cost_usd+? WHERE id=?", (cost, run_id))

    def set_main_session(self, run_id: int, session_id: str, cost_usd: float) -> None:
        """Record the lead's session and the cumulative spend it now reports."""
        self._exec(
            "UPDATE runs SET main_session_id=?, main_cost_usd=? WHERE id=?",
            (session_id, cost_usd, run_id),
        )

    def fail_unfinished_runs(self, reason: str = "服务重启") -> int:
        cur = self._exec(
            """UPDATE runs SET status='interrupted', finished_at=datetime('now'), stop_reason=?
               WHERE status IN ('queued','running','needs_input')""",
            (reason,),
        )
        return cur.rowcount

    def delete_run(self, run_id: int) -> None:
        """Remove a run and everything hanging off it.

        ``events`` has no foreign key (it is an append-only log that outlives
        task rows), so it is deleted explicitly; tasks, facts, flags, messages
        and cases cascade from ``runs``.
        """
        with self._lock:
            self._conn.execute("DELETE FROM events WHERE run_id=?", (run_id,))
            self._conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._conn.commit()

    def delete_challenge(self, challenge_id: int) -> None:
        """Remove a challenge that has no runs left.

        Runs are the operator's history; deleting one silently would be a data
        loss, so the caller must have deleted (or never started) them.
        """
        self._exec("DELETE FROM challenges WHERE id=?", (challenge_id,))

    # ------------------------------------------------------------------ folders
    def add_folder(self, name: str) -> Folder:
        """Create a folder. Raises ``ValueError`` when the name is taken.

        The rail lists folders by name only, so two with the same name are two
        identical headings the operator cannot tell apart. The message is the
        one the page shows verbatim.
        """
        clean = name.strip() or "未命名文件夹"
        with self._lock:
            if self._row("SELECT id FROM folders WHERE name=?", (clean,)):
                raise ValueError(f"已经有叫「{clean}」的文件夹了")
            cur = self._exec("INSERT INTO folders (name) VALUES (?)", (clean,))
        folder = self.get_folder(int(cur.lastrowid))
        assert folder is not None
        return folder

    def get_folder(self, folder_id: int) -> Folder | None:
        row = self._row("SELECT * FROM folders WHERE id=?", (folder_id,))
        return self._folder(row) if row else None

    def list_folders(self) -> list[Folder]:
        return [self._folder(r) for r in self._rows("SELECT * FROM folders ORDER BY id")]

    def rename_folder(self, folder_id: int, name: str) -> None:
        """Rename a folder. Raises ``ValueError`` when the name is taken."""
        clean = name.strip() or "未命名文件夹"
        with self._lock:
            clash = self._row("SELECT id FROM folders WHERE name=? AND id<>?", (clean, folder_id))
            if clash:
                raise ValueError(f"已经有叫「{clean}」的文件夹了")
            self._exec("UPDATE folders SET name=? WHERE id=?", (clean, folder_id))

    def delete_folder(self, folder_id: int) -> None:
        """Remove the group, not the work in it: its runs become unfiled.

        Deleting a folder is a bookkeeping act. The runs inside it are the
        operator's history and the notes distilled from them are files, so
        nothing but the grouping goes away.
        """
        with self._lock:
            self._conn.execute("UPDATE runs SET folder_id=NULL WHERE folder_id=?", (folder_id,))
            self._conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
            self._conn.commit()

    def set_run_folder(self, run_id: int, folder_id: int | None) -> None:
        self._exec("UPDATE runs SET folder_id=? WHERE id=?", (folder_id, run_id))

    @staticmethod
    def _folder(row: sqlite3.Row) -> Folder:
        return Folder(id=row["id"], name=row["name"], created_at=row["created_at"])

    # ------------------------------------------------------------------- layout
    def rail_groups(
        self, per_folder: int = 25
    ) -> list[tuple[Folder | None, int, list[Run]]]:
        """Rail layout as ``(folder, total_runs, shown_runs)``, unfiled last.

        One place decides which runs the rail shows and how many a group hides:
        the rail, the folder page and the empty-folder label would otherwise
        disagree about whether a folder has work in it. The total comes from a
        real count, not from ``len(shown)`` — a group cut off by the per-folder
        window must not claim to be complete.

        Folders with no runs are kept. Deleting one is a click; looking like it
        vanished on creation reads as a failed create.
        """
        folders = self.list_folders()
        counted = self.count_runs_per_folder()
        shown: dict[int | None, list[Run]] = {}
        for run in self.list_runs_grouped(per_folder=per_folder):
            shown.setdefault(run.folder_id, []).append(run)

        groups: list[tuple[Folder | None, int, list[Run]]] = [
            (folder, counted.get(folder.id, 0), shown.get(folder.id, []))
            for folder in folders
        ]
        # Runs filed under a folder id that no longer exists are counted by
        # ``count_runs_per_folder`` but attached to no group; fold them into
        # unfiled so the totals still add up. The route validates folder ids, so
        # this only happens after a crash between the two statements of
        # ``delete_folder``.
        known = {folder.id for folder in folders}
        loose = sum(n for fid, n in counted.items() if fid is not None and fid not in known)
        unfiled = counted.get(None, 0) + loose
        unfiled_runs = list(shown.get(None, []))
        for fid, rows in shown.items():
            if fid is not None and fid not in known:
                unfiled_runs.extend(rows)
        if unfiled or unfiled_runs:
            unfiled_runs.sort(key=lambda r: r.id, reverse=True)
            groups.append((None, unfiled, unfiled_runs))
        return groups

    # -------------------------------------------------------------------- tasks
    def create_task(self, run_id: int, role: str, objective: str, context: str) -> Task:
        cur = self._exec(
            "INSERT INTO tasks (run_id, role, objective, context) VALUES (?,?,?,?)",
            (run_id, role, objective, context),
        )
        task = self.get_task(int(cur.lastrowid))
        assert task is not None
        return task

    def get_task(self, task_id: int) -> Task | None:
        row = self._row("SELECT * FROM tasks WHERE id=?", (task_id,))
        return self._task(row) if row else None

    def list_tasks(self, run_id: int) -> list[Task]:
        return [
            self._task(r) for r in self._rows("SELECT * FROM tasks WHERE run_id=? ORDER BY id", (run_id,))
        ]

    def mark_task_started(self, task_id: int) -> None:
        self._exec(
            "UPDATE tasks SET status='running', started_at=datetime('now') WHERE id=?", (task_id,)
        )

    def set_task_status(self, task_id: int, status: str) -> None:
        self._exec("UPDATE tasks SET status=? WHERE id=?", (status, task_id))

    def get_task_cost(self, task_id: int) -> float:
        row = self._row("SELECT cost_usd FROM tasks WHERE id=?", (task_id,))
        return float(row["cost_usd"]) if row else 0.0

    def add_task_usage(self, task_id: int, cost: float = 0.0, turns: int = 0) -> None:
        self._exec(
            "UPDATE tasks SET cost_usd=cost_usd+?, turns=turns+? WHERE id=?",
            (cost, turns, task_id),
        )

    def finish_task(
        self,
        task_id: int,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
        *,
        session_id: str = "",
    ) -> None:
        self._exec(
            """UPDATE tasks SET status=?, finished_at=datetime('now'), result=?, error=?,
                 session_id=CASE WHEN ?<>'' THEN ? ELSE session_id END
               WHERE id=?""",
            (status, dumps(result or {}), error, session_id, session_id, task_id),
        )

    def fail_running_tasks(self, run_id: int, status: str = "stopped", reason: str = "") -> int:
        cur = self._exec(
            """UPDATE tasks SET status=?, finished_at=datetime('now'),
                 error=CASE WHEN ?<>'' THEN ? ELSE error END
               WHERE run_id=? AND status IN ('queued','running')""",
            (status, reason, reason, run_id),
        )
        return cur.rowcount

    @staticmethod
    def _task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            run_id=row["run_id"],
            role=row["role"],
            objective=row["objective"],
            context=row["context"],
            status=row["status"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            session_id=row["session_id"],
            cost_usd=row["cost_usd"],
            turns=row["turns"],
            result=loads(row["result"], {}),
            error=row["error"],
        )

    # -------------------------------------------------------------------- facts
    def add_fact(
        self,
        run_id: int,
        task_id: int | None,
        kind: str,
        content: str,
        evidence: dict[str, Any],
        basis: str = "",
        resolves: int | None = None,
        supersedes: int | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO facts (run_id, task_id, kind, content, evidence, basis, resolves, supersedes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, task_id, kind, content, dumps(evidence), basis, resolves, supersedes),
        )
        return int(cur.lastrowid)

    def list_facts(
        self, run_id: int, limit: int = 200, task_id: int | None = None
    ) -> list[Fact]:
        if task_id is None:
            rows = self._rows(
                "SELECT * FROM facts WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)
            )
        else:
            rows = self._rows(
                "SELECT * FROM facts WHERE run_id=? AND task_id=? ORDER BY id DESC LIMIT ?",
                (run_id, task_id, limit),
            )
        return [
            Fact(
                id=r["id"],
                run_id=r["run_id"],
                task_id=r["task_id"],
                kind=r["kind"],
                content=r["content"],
                evidence=loads(r["evidence"], {}),
                created_at=r["created_at"],
                basis=r["basis"],
                resolves=r["resolves"],
                supersedes=r["supersedes"],
            )
            for r in reversed(rows)
        ]

    # -------------------------------------------------------------------- flags
    def add_flag(
        self, run_id: int, task_id: int | None, value: str, source: str, method: str, confidence: str
    ) -> Flag | None:
        """Insert a candidate; returns None if the same value already exists for the run."""
        if self._row("SELECT id FROM flags WHERE run_id=? AND value=?", (run_id, value)):
            return None
        cur = self._exec(
            "INSERT INTO flags (run_id, task_id, value, source, method, confidence) VALUES (?,?,?,?,?,?)",
            (run_id, task_id, value, source, method, confidence),
        )
        return self.get_flag(int(cur.lastrowid))

    def set_flag_status(self, flag_id: int, status: str, note: str = "") -> None:
        self._exec(
            "UPDATE flags SET status=?, note=CASE WHEN ?<>'' THEN ? ELSE note END WHERE id=?",
            (status, note, note, flag_id),
        )

    def usage_by_task(self, run_id: int | None = None) -> list[dict[str, Any]]:
        """Peak token usage per session, with the role and run it belonged to.

        A result event carries the session's *cumulative* usage, so the maximum
        reported value is that session's total. Summing every event would count
        the same tokens once per turn — a 90-turn worker would look 90x more
        expensive than it was.

        ``task_id`` is NULL for the lead's own session; it is reported under the
        ``main`` role, which is what the price lookup needs.
        """
        sql = """
            SELECT e.task_id                AS task_id,
                   t.run_id                 AS run_id,
                   t.role                   AS role,
                   MAX(json_extract(e.payload, '$.usage.input_tokens')) AS input_tokens,
                   MAX(json_extract(e.payload, '$.usage.cache_read_input_tokens')) AS cache_read,
                   MAX(json_extract(e.payload, '$.usage.output_tokens')) AS output_tokens
              FROM events e
              LEFT JOIN tasks t ON t.id = e.task_id
             WHERE e.kind = 'agent.event'
               AND json_extract(e.payload, '$.kind') = 'result'
        """
        params: tuple[Any, ...] = ()
        if run_id is not None:
            sql += " AND t.run_id = ?"
            params = (run_id,)
        sql += " GROUP BY e.task_id"
        return [
            {
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "role": row["role"] or "main",
                "input_tokens": int(row["input_tokens"] or 0),
                "cache_read": int(row["cache_read"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
            }
            for row in self._rows(sql, params)
        ]

    def list_flags(self, run_id: int) -> list[Flag]:
        return [self._flag(r) for r in self._rows("SELECT * FROM flags WHERE run_id=? ORDER BY id", (run_id,))]

    def get_flag(self, flag_id: int) -> Flag | None:
        row = self._row("SELECT * FROM flags WHERE id=?", (flag_id,))
        return self._flag(row) if row else None

    @staticmethod
    def _flag(row: sqlite3.Row) -> Flag:
        return Flag(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            value=row["value"],
            source=row["source"],
            method=row["method"],
            confidence=row["confidence"],
            status=row["status"],
            created_at=row["created_at"],
            note=row["note"],
        )

    # -------------------------------------------------------------------- cases
    def upsert_case(self, case: Case) -> int:
        """Write the case for a run, replacing any earlier distillation of it.

        A run can be closed more than once (the operator confirms a flag after
        the lead already gave up), and the second close is the one that knows
        the flag. ``run_id`` is unique, so the later, better-informed card wins.
        """
        self._exec(
            """INSERT INTO cases
                 (run_id, challenge_id, name, category, outcome, route, derivation,
                  dead_ends, open_unknowns, techniques, cost_usd, elapsed_s,
                  help_count, harm_count)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(run_id) DO UPDATE SET
                 name=excluded.name, category=excluded.category, outcome=excluded.outcome,
                 route=excluded.route, derivation=excluded.derivation,
                 dead_ends=excluded.dead_ends, open_unknowns=excluded.open_unknowns,
                 techniques=excluded.techniques,
                 cost_usd=excluded.cost_usd, elapsed_s=excluded.elapsed_s""",
            (
                case.run_id, case.challenge_id, case.name, case.category, case.outcome,
                dumps(case.route), case.derivation, dumps(case.dead_ends),
                dumps(case.open_unknowns), dumps(case.techniques),
                case.cost_usd, case.elapsed_s, case.help_count, case.harm_count,
            ),
        )
        row = self._row("SELECT id FROM cases WHERE run_id=?", (case.run_id,))
        return int(row["id"]) if row else 0

    def list_cases(self, limit: int = 200) -> list[Case]:
        return [self._case(r) for r in self._rows("SELECT * FROM cases ORDER BY id DESC LIMIT ?", (limit,))]

    def get_case(self, case_id: int) -> Case | None:
        row = self._row("SELECT * FROM cases WHERE id=?", (case_id,))
        return self._case(row) if row else None

    def touch_case(self, case_id: int) -> None:
        """Record that a case was injected into a prompt."""
        self._exec(
            "UPDATE cases SET used_count=used_count+1, last_used_at=datetime('now') WHERE id=?",
            (case_id,),
        )

    def credit_case(self, case_id: int, helped: bool) -> None:
        """Record what the run that loaded a card ended up doing.

        A load is an exposure, not a result. Without the outcome the only thing
        the library knows about a card is that it exists, and a card that
        misleads every run it appears on is indistinguishable from a good one.
        """
        column = "help_count" if helped else "harm_count"
        self._exec(f"UPDATE cases SET {column}={column}+1 WHERE id=?", (case_id,))

    def reset_case_counts(self) -> int:
        """Zero every card's help/harm, for a recount from the run records.

        The counters written before use-based attribution were exposure counts
        (a card in five winning prompts read 5-0 whether or not anything in it
        was applied). They cannot be corrected by adjusting them — only by
        discarding them and recounting from what the runs actually wrote. The
        runs themselves are the evidence and they are still there.
        """
        cur = self._exec(
            "UPDATE cases SET help_count=0, harm_count=0 WHERE help_count<>0 OR harm_count<>0"
        )
        return cur.rowcount

    def set_run_cases(self, run_id: int, case_ids: list[int]) -> None:
        self._exec("UPDATE runs SET case_ids=? WHERE id=?", (dumps(case_ids), run_id))

    def get_run_cases(self, run_id: int) -> list[int]:
        row = self._row("SELECT case_ids FROM runs WHERE id=?", (run_id,))
        return [int(c) for c in loads(row["case_ids"], [])] if row else []

    def claim_case_credit(self, run_id: int) -> bool:
        """Atomically take the right to credit this run's cards, once."""
        cur = self._exec(
            "UPDATE runs SET cases_credited=1 WHERE id=? AND cases_credited=0", (run_id,)
        )
        return cur.rowcount > 0

    def clear_case_credit(self, run_id: int) -> bool:
        """Give a run back its one attribution, so it can be recharged.

        Used only by an explicit recount (``ctfagent recount``): the run's cards
        were charged under the exposure rule that counted being shown as being
        used, and the charge has to be undone before the same run is judged
        again under the use rule.
        """
        cur = self._exec("UPDATE runs SET cases_credited=0 WHERE id=?", (run_id,))
        return cur.rowcount > 0

    def delete_case(self, case_id: int) -> None:
        self._exec("DELETE FROM cases WHERE id=?", (case_id,))

    def set_run_notes(self, run_id: int, paths: list[str]) -> None:
        """Record which technique notes this run's prompts listed."""
        self._exec("UPDATE runs SET note_paths=? WHERE id=?", (dumps(paths), run_id))

    def get_run_notes(self, run_id: int) -> list[str]:
        row = self._row("SELECT note_paths FROM runs WHERE id=?", (run_id,))
        return [str(p) for p in loads(row["note_paths"], [])] if row else []

    def claim_notes_credit(self, run_id: int) -> bool:
        """Atomically take the right to charge this run's notes, once."""
        cur = self._exec(
            "UPDATE runs SET notes_credited=1 WHERE id=? AND notes_credited=0", (run_id,)
        )
        return cur.rowcount > 0

    def distilled_notes(self) -> dict[int, list[dict[str, str]]]:
        """Which runs installed a technique note, and where.

        Read from ``skill.note_added`` rather than a column on ``runs``: the
        installer emits it at the one moment a note lands, so the log already
        knows about runs distilled before such a column would have existed. A
        re-distill (or a second click on the button) emits again for the same
        file, and the path is deduplicated — the question this answers is "has
        this run been distilled", not "how many times was the button pressed".
        """
        out: dict[int, list[dict[str, str]]] = {}
        for row in self._rows(
            "SELECT run_id, payload FROM events WHERE kind='skill.note_added' ORDER BY id"
        ):
            payload = loads(row["payload"], {})
            path = str(payload.get("path") or "")
            if not path:
                continue
            notes = out.setdefault(int(row["run_id"]), [])
            if any(n["path"] == path for n in notes):
                continue
            notes.append({"path": path, "skill": str(payload.get("skill") or "")})
        return out

    @staticmethod
    def _case(row: sqlite3.Row) -> Case:
        return Case(
            id=row["id"],
            run_id=row["run_id"],
            challenge_id=row["challenge_id"],
            name=row["name"],
            category=row["category"],
            outcome=row["outcome"],
            route=loads(row["route"], []),
            derivation=row["derivation"],
            dead_ends=loads(row["dead_ends"], []),
            open_unknowns=loads(row["open_unknowns"], []),
            techniques=loads(row["techniques"], []),
            cost_usd=row["cost_usd"],
            elapsed_s=row["elapsed_s"],
            used_count=row["used_count"],
            help_count=row["help_count"],
            harm_count=row["harm_count"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
        )

    # ------------------------------------------------------------------- events
    def add_event(self, run_id: int, kind: str, payload: dict[str, Any], task_id: int | None = None) -> Event:
        cur = self._exec(
            "INSERT INTO events (run_id, task_id, kind, payload) VALUES (?,?,?,?)",
            (run_id, task_id, kind, dumps(payload)),
        )
        event_id = int(cur.lastrowid)
        row = self._row("SELECT created_at FROM events WHERE id=?", (event_id,))
        return Event(
            id=event_id,
            run_id=run_id,
            task_id=task_id,
            kind=kind,
            payload=payload,
            created_at=row["created_at"] if row else "",
        )

    def events_since(self, run_id: int, after_id: int = 0, limit: int = 500) -> list[Event]:
        rows = self._rows(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
            (run_id, after_id, limit),
        )
        return [self._event(r) for r in rows]

    def recent_events(self, run_id: int, limit: int = 200) -> list[Event]:
        rows = self._rows(
            "SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)
        )
        return [self._event(r) for r in reversed(rows)]

    @staticmethod
    def _event(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            kind=row["kind"],
            payload=loads(row["payload"], {}),
            created_at=row["created_at"],
        )

    # ----------------------------------------------------------------- messages
    def add_message(
        self, run_id: int, role: str, content: str, attachments: list[str] | None = None
    ) -> int:
        cur = self._exec(
            "INSERT INTO messages (run_id, role, content, attachments) VALUES (?,?,?,?)",
            (run_id, role, content, dumps(attachments or [])),
        )
        return int(cur.lastrowid or 0)

    def list_messages(self, run_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM messages WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)
        )
        out = []
        for row in reversed(rows):
            item = dict(row)
            item["attachments"] = loads(item.get("attachments"), [])
            out.append(item)
        return out

    # ------------------------------------------------------------- competitions
    def add_competition(self, name: str, platform: str, base_url: str, token: str) -> Competition:
        cur = self._exec(
            "INSERT INTO competitions (name, platform, base_url, token) VALUES (?,?,?,?)",
            (name, platform, base_url.rstrip("/"), token),
        )
        comp = self.get_competition(int(cur.lastrowid))
        assert comp is not None
        return comp

    def get_competition(self, competition_id: int) -> Competition | None:
        row = self._row("SELECT * FROM competitions WHERE id=?", (competition_id,))
        return self._competition(row) if row else None

    def list_competitions(self) -> list[Competition]:
        rows = self._rows("SELECT * FROM competitions ORDER BY id DESC")
        return [self._competition(r) for r in rows]

    def update_competition(
        self, competition_id: int, *, name: str, base_url: str, token: str
    ) -> None:
        """Change the connection settings.

        An empty ``token`` means "keep the stored one": the page echoes the
        token back as a mask, and a form submitted without editing it must not
        clear the credential.
        """
        current = self.get_competition(competition_id)
        if current is None:
            return
        self._exec(
            "UPDATE competitions SET name=?, base_url=?, token=? WHERE id=?",
            (name, base_url.rstrip("/"), token or current.token, competition_id),
        )

    def note_sync(self, competition_id: int, note: str) -> None:
        self._exec(
            "UPDATE competitions SET last_synced_at=datetime('now'), last_sync_note=? WHERE id=?",
            (note, competition_id),
        )

    def delete_competition(self, competition_id: int) -> None:
        self._exec("DELETE FROM competitions WHERE id=?", (competition_id,))

    @staticmethod
    def _competition(row: sqlite3.Row) -> Competition:
        return Competition(
            id=row["id"],
            name=row["name"],
            platform=row["platform"],
            base_url=row["base_url"],
            token=row["token"],
            created_at=row["created_at"],
            last_synced_at=row["last_synced_at"],
            last_sync_note=row["last_sync_note"],
        )

    # -------------------------------------------------------------- providers
    def add_provider(self, provider: Provider) -> int:
        cur = self._exec(
            """INSERT INTO providers
               (name, base_url, auth_token, api_key, api_format, models,
                last_probe, last_probe_note, last_probed_at, imported_from)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                provider.name.strip(),
                provider.base_url.strip().rstrip("/"),
                provider.auth_token.strip(),
                provider.api_key.strip(),
                provider.api_format or "anthropic",
                dumps(provider.models),
                provider.last_probe,
                provider.last_probe_note,
                provider.last_probed_at,
                provider.imported_from,
            ),
        )
        return int(cur.lastrowid)

    def find_provider(self, base_url: str) -> Provider | None:
        """The row for this endpoint, so a re-import updates instead of duplicates."""
        row = self._row(
            "SELECT * FROM providers WHERE base_url=? ORDER BY id LIMIT 1",
            (base_url.strip().rstrip("/"),),
        )
        return self._provider(row) if row else None

    def get_provider(self, provider_id: int) -> Provider | None:
        row = self._row("SELECT * FROM providers WHERE id=?", (provider_id,))
        return self._provider(row) if row else None

    def list_providers(self) -> list[Provider]:
        return [self._provider(r) for r in self._rows("SELECT * FROM providers ORDER BY id")]

    def count_providers(self) -> int:
        row = self._row("SELECT COUNT(*) AS n FROM providers")
        return int(row["n"]) if row else 0

    def update_provider(
        self,
        provider_id: int,
        *,
        name: str,
        base_url: str,
        auth_token: str | None = None,
        api_key: str | None = None,
        api_format: str = "anthropic",
    ) -> None:
        """Change a provider. ``None`` for a secret means "keep the stored one".

        The page echoes secrets back as a mask, so a form submitted without
        retyping the token must not wipe it — the same rule the competition
        accounts follow.
        """
        current = self.get_provider(provider_id)
        if current is None:
            return
        self._exec(
            """UPDATE providers
               SET name=?, base_url=?, auth_token=?, api_key=?, api_format=?
               WHERE id=?""",
            (
                name.strip() or current.name,
                base_url.strip().rstrip("/"),
                current.auth_token if auth_token is None else auth_token.strip(),
                current.api_key if api_key is None else api_key.strip(),
                api_format or current.api_format,
                provider_id,
            ),
        )

    def note_provider_probe(
        self, provider_id: int, *, ok: bool, note: str, models: list[str] | None = None
    ) -> None:
        """Record the last probe, and the model list when one came back.

        The model list is what the per-role dropdown is built from; an endpoint
        that answers 404 for ``/v1/models`` leaves it empty rather than clearing
        a list a previous probe filled in (a gateway can stop serving the route
        without changing what it serves).
        """
        current = self.get_provider(provider_id)
        if current is None:
            return
        models = models if models is not None else current.models
        self._exec(
            """UPDATE providers
               SET last_probe=?, last_probe_note=?, last_probed_at=datetime('now'), models=?
               WHERE id=?""",
            ("ok" if ok else "failed", note[:400], dumps(models), provider_id),
        )

    def delete_provider(self, provider_id: int) -> None:
        self._exec("DELETE FROM providers WHERE id=?", (provider_id,))
        if self.get_setting("active_provider") == str(provider_id):
            self.set_setting("active_provider", "")

    @staticmethod
    def _provider(row: sqlite3.Row) -> Provider:
        return Provider(
            id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            auth_token=row["auth_token"],
            api_key=row["api_key"],
            api_format=row["api_format"],
            models=loads(row["models"], []),
            last_probe=row["last_probe"],
            last_probe_note=row["last_probe_note"],
            last_probed_at=row["last_probed_at"],
            imported_from=row["imported_from"],
            created_at=row["created_at"],
        )

    # ----------------------------------------------------------- app settings
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._row("SELECT value FROM app_settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO app_settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -------------------------------------------------------------- submissions
    def add_submission(
        self,
        flag_id: int,
        competition_id: int,
        external_id: str,
        status: str,
        message: str = "",
    ) -> Submission:
        cur = self._exec(
            """INSERT INTO submissions (flag_id, competition_id, external_id, status, message)
               VALUES (?,?,?,?,?)""",
            (flag_id, competition_id, str(external_id), status, message),
        )
        sub = self.get_submission(int(cur.lastrowid))
        assert sub is not None
        return sub

    def get_submission(self, submission_id: int) -> Submission | None:
        row = self._row("SELECT * FROM submissions WHERE id=?", (submission_id,))
        return self._submission(row) if row else None

    def latest_submission(self, flag_id: int) -> Submission | None:
        row = self._row(
            "SELECT * FROM submissions WHERE flag_id=? ORDER BY id DESC LIMIT 1", (flag_id,)
        )
        return self._submission(row) if row else None

    @staticmethod
    def _submission(row: sqlite3.Row) -> Submission:
        return Submission(
            id=row["id"],
            flag_id=row["flag_id"],
            competition_id=row["competition_id"],
            external_id=row["external_id"],
            status=row["status"],
            message=row["message"],
            created_at=row["created_at"],
        )
