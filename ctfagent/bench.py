"""Benchmarks: the evidence that the platform is getting stronger.

Without a fixed evaluation set, "we improved" is unfalsifiable. Every change to
a prompt, a tool or the case library has a plausible story; only a score on
challenges the platform has not trained on says whether it worked.

Two rules keep the number honest:

* **A benchmark run is never training data.** Each invocation gets its own
  database, so a card distilled from a benchmark run cannot be injected into the
  next benchmark run of the same challenge. That would measure memory, not
  skill, and it would make every later score look better than it is.
* **The answer is checked, not the confidence.** A run counts as solved only
  when a submitted value equals the expected one. A worker that says "the flag
  is almost certainly X" scored zero, and so did one that matched a decoy.
* **A worker cannot read the answer key.** The manifest, the training database
  and any ``--library`` are sealed for the duration of the run; a challenge's
  original material folder can be sealed per item with ``sealed:``. Both were
  needed: a run was observed grepping the host for the challenge name, reading
  ``bench.yaml``'s ``expect``, and submitting it — and separately shipping the
  recovered answer inside the challenge's own attachments.

The library can still be studied: ``--library <db>`` copies only the ``cases``
rows from another database into the fresh one. That is the train/test split —
train on challenges outside the benchmark, then measure with the cards that
training produced.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .challenge import load_challenge
from .config import AppConfig
from .db import Store, TERMINAL_RUN_STATES
from .events import EventBus
from .models import FlagStatus, RunStatus
from .workspace import probe_sandbox

if TYPE_CHECKING:  # pragma: no cover
    from .orchestrator import RunManager

MANIFEST_NAMES = ("bench.yaml", "bench.yml", "bench.json")
#: A run that never reaches one of these is still going; the bench stops it.
_ENDED = set(TERMINAL_RUN_STATES) | {"interrupted", "needs_input"}
#: The wrapper a challenge's flag_format usually declares. It is presentation,
#: not part of the answer: a manifest that lists ``96b3…`` and a worker that
#: submits ``flag{96b3…}`` are talking about the same value, and comparing them
#: literally scored a solved challenge as a miss.
_WRAPPER = re.compile(r"^(?:flag|ctf|key)\{(.*)\}$", re.IGNORECASE | re.DOTALL)


def _bare(value: str) -> str:
    """The answer without its declared wrapper, for comparison only."""
    value = (value or "").strip()
    inner = _WRAPPER.match(value)
    return inner.group(1).strip() if inner else value


@dataclass
class BenchItem:
    name: str
    directory: Path
    expect: str = ""
    expect_regex: str = ""
    #: Free-form label used to group results (category is taken from the
    #: challenge itself when omitted).
    category: str = ""
    note: str = ""
    #: Directories a worker must not read while this item runs. The challenge
    #: material is copied into the workspace, but the *original* folder often
    #: sits next to whatever the solver wrote, and a worker that greps the host
    #: for the challenge name reaches it. Curation alone does not close that:
    #: the answer is still on disk, one directory away.
    sealed: list[Path] = field(default_factory=list)

    def matches(self, value: str) -> bool:
        """Does this submitted value count as the expected answer?

        Both sides are compared with their wrapper removed, so a manifest may
        list either form. The comparison stays exact otherwise: no prefix,
        subsequence or "close enough" matching, because those are how a decoy
        gets scored as a solve.
        """
        value = (value or "").strip()
        if not value:
            return False
        bare = _bare(value)
        if self.expect:
            expected = self.expect.strip()
            return bare.lower() == _bare(expected).lower()
        if self.expect_regex:
            try:
                return any(
                    re.fullmatch(self.expect_regex, candidate) is not None
                    for candidate in (value, bare)
                )
            except re.error:
                return False
        return False


@dataclass
class BenchResult:
    name: str
    category: str
    run_id: int
    status: str
    solved: bool
    hit: bool
    verified_hit: bool
    matched_value: str
    expected: str
    cost_usd: float
    elapsed_s: float
    tasks: int
    flag_statuses: list[tuple[str, str]] = field(default_factory=list)
    error: str = ""


def _parse_items(text: str) -> list[dict[str, str]]:
    """The ``bench.yaml`` shape: ``items:`` followed by ``- key: value`` maps.

    Deliberately narrow. A bench manifest is a list of path/expected-value
    triples; supporting anchors and nesting here would buy nothing and invite
    a misread that silently changes what is being measured.
    """
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#") or line.strip() == "items:":
            continue
        stripped = line.strip()
        starts_item = stripped.startswith("- ")
        body = stripped[2:].strip() if starts_item else stripped
        if ":" not in body:
            raise ValueError(f"bench.yaml 第 {number} 行应该是「键: 值」：{raw!r}")
        key, _, value = body.partition(":")
        key, value = key.strip(), value.strip().strip("\"'")
        if starts_item:
            current = {}
            items.append(current)
        if current is None:
            if key in ("items",):
                continue
            raise ValueError(f"bench.yaml 第 {number} 行不在任何条目里：{raw!r}")
        current[key] = value
    return items


def load_manifest(directory: Path) -> list[BenchItem]:
    """Read ``bench.yaml`` and resolve every item's challenge directory.

    Relative directories resolve against the manifest, so a benchmark folder
    can be moved or shared without rewriting paths.
    """
    manifest = next((directory / n for n in MANIFEST_NAMES if (directory / n).exists()), None)
    if manifest is None:
        raise FileNotFoundError(
            f"{directory} 里没有 bench.yaml。格式：\n"
            "items:\n"
            "  - name: easyrs\n"
            "    directory: D:/path/to/challenge\n"
            "    expect: 96b3...   # 或 expect_regex: \"[0-9a-f]{32}\"\n"
        )
    if manifest.suffix == ".json":
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        entries = raw.get("items")
    else:
        # Not ``challenge._parse_yaml``: that parser is a flat key/value subset
        # and silently drops a list of maps, which is exactly this file's shape.
        entries = _parse_items(manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{manifest} 里没有 items 列表")
    items: list[BenchItem] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        target = Path(str(entry.get("directory") or "")).expanduser()
        if not target.is_absolute():
            target = (manifest.parent / target).resolve()
        sealed: list[Path] = []
        for raw in str(entry.get("sealed") or "").split(";"):
            raw = raw.strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            sealed.append(path if path.is_absolute() else (manifest.parent / path).resolve())
        name = str(entry.get("name") or target.name)
        items.append(
            BenchItem(
                name=name,
                directory=target,
                expect=str(entry.get("expect") or ""),
                expect_regex=str(entry.get("expect_regex") or ""),
                category=str(entry.get("category") or ""),
                note=str(entry.get("note") or ""),
                sealed=sealed,
            )
        )
    if not items:
        raise ValueError(f"{manifest} 里没有可用的条目")
    return items


def _bench_skills_dir(
    live: Path, workdir: Path, notes_dir: Path | None
) -> Path:
    """A skill directory the benchmark may write to, holding no training notes.

    The skills directory is not data the fresh database isolates: a
    ``learned-*.md`` distilled by a training run sits there and is injected into
    the next benchmark run's prompt, and a benchmark run that distils its own
    note writes into the live library. Both directions leak, so the benchmark
    gets its own copy.

    * ``notes_dir`` given: that directory's notes are copied in (an explicit
      opt-in to measuring with training notes).
    * otherwise: the notes are stripped, and any that a benchmark run itself
      produces live and die with the temporary copy.
    """
    dest = workdir / "skills"
    if not live.is_dir():
        return live
    shutil.copytree(live, dest)
    if notes_dir is not None and notes_dir.is_dir():
        for skill_dir in dest.iterdir():
            if not skill_dir.is_dir():
                continue
            source = notes_dir / skill_dir.name
            if not source.is_dir():
                continue
            for note in source.glob("learned-*.md"):
                try:
                    shutil.copy2(note, skill_dir / note.name)
                except OSError:
                    continue
    else:
        for note in dest.rglob("learned-*.md"):
            try:
                note.unlink()
            except OSError:
                continue
    return dest


def prepare_db(bench_db: Path, library: Path | None = None) -> Store:
    """A fresh store for one invocation, optionally seeded with past cases.

    Seeding copies the ``cases`` rows verbatim (with their help/harm counters)
    and nothing else: the benchmark must not inherit a challenge row, a run or a
    flag from the training database — only what the platform learned.
    """
    if bench_db.exists():
        bench_db.unlink()
    for suffix in ("-wal", "-shm"):
        side = bench_db.with_name(bench_db.name + suffix)
        if side.exists():
            side.unlink()
    store = Store(bench_db)
    if library is not None and library.exists():
        _import_cases(library, bench_db)
        store.close()
        store = Store(bench_db)
    return store


def _import_cases(source: Path, target: Path) -> int:
    src = sqlite3.connect(str(source))
    src.row_factory = sqlite3.Row
    try:
        # A library written before a column existed (``open_unknowns``,
        # ``help_count``, …) must still seed a bench: the cards are useful, the
        # missing fields have sensible empties. Failing here would mean an
        # operator has to upgrade the training database before they can measure.
        present = {row["name"] for row in src.execute("PRAGMA table_info(cases)")}
        columns = {
            "name": "''",
            "category": "'unknown'",
            "outcome": "'failed'",
            "route": "'[]'",
            "derivation": "''",
            "dead_ends": "'[]'",
            "open_unknowns": "'[]'",
            "techniques": "'[]'",
            "cost_usd": "0",
            "elapsed_s": "0",
            "help_count": "0",
            "harm_count": "0",
        }
        select = ", ".join(
            name if name in present else f"{default} AS {name}"
            for name, default in columns.items()
        )
        rows = src.execute(f"SELECT {select} FROM cases").fetchall()
    finally:
        src.close()
    dst = sqlite3.connect(str(target))
    try:
        # ``challenge_id`` is a foreign key into the training database and means
        # nothing here; -1 keeps the row valid without pretending it matches a
        # local challenge. ``run_id`` is unique per card and unused by scoring.
        for index, row in enumerate(rows, start=1):
            dst.execute(
                """INSERT INTO cases
                     (run_id, challenge_id, name, category, outcome, route, derivation,
                      dead_ends, open_unknowns, techniques, cost_usd, elapsed_s,
                      help_count, harm_count)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    -index, -1, row["name"], row["category"], row["outcome"],
                    row["route"], row["derivation"], row["dead_ends"],
                    row["open_unknowns"], row["techniques"], row["cost_usd"],
                    row["elapsed_s"], row["help_count"], row["harm_count"],
                ),
            )
        dst.commit()
    finally:
        dst.close()
    return len(rows)


async def _run_one(
    manager: "RunManager",
    store: Store,
    item: BenchItem,
    cfg: AppConfig,
    poll_s: float = 1.0,
    on_run: Any = None,
) -> BenchResult:
    challenge = load_challenge(item.directory)
    challenge_id: int = store.add_challenge(challenge)
    started = time.time()
    run = await manager.start(
        challenge_id, f"解出 {challenge.name} 并找到 flag", sealed_roots=item.sealed
    )
    if on_run is not None:
        hook = on_run(run)
        if asyncio.iscoroutine(hook):
            await hook
    deadline = started + cfg.limits.run_timeout_s + 300
    row = None
    while time.time() < deadline:
        await asyncio.sleep(poll_s)
        row = store.get_run(run.id)
        if row is None:
            break
        if row.status in _ENDED:
            break
    status = row.status if row is not None else "unknown"
    if row is not None and row.status not in TERMINAL_RUN_STATES:
        # ``needs_input`` (a candidate awaiting the operator) or a stuck run.
        # The bench has no operator, so it records what exists and stops.
        await manager.stop(run.id, "基准测试结束")
        status = store.get_run(run.id).status if store.get_run(run.id) else status

    flags = store.list_flags(run.id)
    matched = next((f for f in flags if item.matches(f.value)), None)
    verified = next(
        (f for f in flags if item.matches(f.value)
         and f.status in (FlagStatus.VERIFIED, FlagStatus.CONFIRMED)),
        None,
    )
    tasks = store.list_tasks(run.id)
    elapsed = (row.started_at and row.finished_at) or ""
    if elapsed:
        try:
            t0 = datetime.strptime(row.started_at, "%Y-%m-%d %H:%M:%S")
            t1 = datetime.strptime(row.finished_at, "%Y-%m-%d %H:%M:%S")
            elapsed_s = max(0.0, (t1 - t0).total_seconds())
        except ValueError:
            elapsed_s = time.time() - started
    else:
        elapsed_s = time.time() - started
    return BenchResult(
        name=item.name,
        category=item.category or challenge.category or "unknown",
        run_id=run.id,
        status=status,
        solved=bool(matched),
        hit=bool(matched),
        verified_hit=bool(verified),
        matched_value=matched.value if matched else "",
        expected=item.expect or item.expect_regex,
        cost_usd=round(row.cost_usd if row is not None else 0.0, 4),
        elapsed_s=round(elapsed_s, 1),
        tasks=len(tasks),
        flag_statuses=[(f.value, f.status) for f in flags],
    )


async def run_benchmark(
    items: list[BenchItem],
    cfg: AppConfig,
    *,
    library: Path | None = None,
    notes_dir: Path | None = None,
    bench_db: Path | None = None,
    keep_db: bool = False,
    poll_s: float = 1.0,
    on_result: Any = None,
    on_run: Any = None,
    backend: Any = None,
    sealed_roots: list[Path] | None = None,
) -> tuple[list[BenchResult], dict[str, Any]]:
    """Run every item in order and return the results plus a summary.

    Sequential on purpose: two runs at once share the machine and the API, and
    the cost and wall-clock numbers stop being comparable between invocations.

    ``sealed_roots`` are passed to every worker as directories it may not read
    or search. The benchmark directory belongs there: it holds the expected
    answers, and a worker that grepped its parent found them.

    ``notes_dir`` opts the benchmark *into* the technique notes distilled from
    training runs. The default is the opposite of ``library`` on purpose: cards
    are copied only when asked for, and notes are excluded unless asked for.
    Left alone, the run gets a copy of the skill library with every
    ``learned-*.md`` stripped — otherwise the benchmark silently measures the
    training set, which is exactly what the fresh database is there to prevent,
    and a benchmark run that distils a note would leave it in the live library
    for the next *real* run too.
    """
    from .backend import ClaudeSDKBackend  # noqa: PLC0415 - avoids an import cycle
    from .orchestrator import RunManager  # noqa: PLC0415

    workdir = Path(tempfile.mkdtemp(prefix="ctfagent-bench-"))
    db_path = bench_db or (workdir / "bench.db")
    bench_cfg = replace(
        cfg,
        data_dir=workdir / "workspaces",
        db_path=db_path,
        skills_dir=_bench_skills_dir(Path(cfg.skills_dir), workdir, notes_dir),
    )
    bench_cfg.ensure_dirs()
    store = prepare_db(db_path, library)
    bus = EventBus(store)
    manager = RunManager(
        store, bus, bench_cfg,
        backend if backend is not None else ClaudeSDKBackend(bench_cfg),
        probe_sandbox(bench_cfg), bench_cfg.skills_dir,
        sealed_roots=list(sealed_roots or []),
    )
    results: list[BenchResult] = []
    try:
        for item in items:
            try:
                result = await _run_one(manager, store, item, bench_cfg, poll_s, on_run)
            except Exception as exc:  # noqa: BLE001 - one bad item must not end the bench
                result = BenchResult(
                    name=item.name,
                    category=item.category or "unknown",
                    run_id=0,
                    status="error",
                    solved=False,
                    hit=False,
                    verified_hit=False,
                    matched_value="",
                    expected=item.expect or item.expect_regex,
                    cost_usd=0.0,
                    elapsed_s=0.0,
                    tasks=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(result)
            if on_result is not None:
                on_result(result)
    finally:
        await manager.stop_all()
        store.close()
        if not keep_db:
            shutil.rmtree(workdir, ignore_errors=True)
    summary = summarise(results)
    summary["library"] = str(library) if library else ""
    summary["notes"] = str(notes_dir) if notes_dir else "（未使用训练笔记）"
    summary["bench_db"] = str(db_path) if keep_db else ""
    return results, summary

def summarise(results: list[BenchResult]) -> dict[str, Any]:
    solved = [r for r in results if r.solved]
    verified = [r for r in results if r.verified_hit]
    total_cost = round(sum(r.cost_usd for r in results), 4)
    return {
        "items": len(results),
        "solved": len(solved),
        "verified": len(verified),
        "solve_rate": round(len(solved) / len(results), 4) if results else 0.0,
        "cost_usd": total_cost,
        "cost_per_solve_usd": round(total_cost / len(solved), 4) if solved else None,
        "elapsed_s": round(sum(r.elapsed_s for r in results), 1),
        "peak_hours": peak_hours(),
    }


#: The gateway doubles its rate on weekdays 09:00–12:00 and 14:00–18:00
#: Asia/Shanghai (from its own ``billing_expr``). A benchmark costs twice as
#: much at those hours, so a cost comparison across two runs taken at different
#: times measures the clock, not the code. Recorded with every report so the
#: next comparison can tell the two apart instead of celebrating a 2x "win".
_SHANGHAI = timezone(timedelta(hours=8))


def peak_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(_SHANGHAI)
    hour = now.hour
    return now.weekday() < 5 and (9 <= hour < 12 or 14 <= hour < 18)


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """A human-readable delta, item by item.

    The high-level numbers alone hide the case that matters most: one challenge
    solved that used to fail, outweighed by a lower bill.
    """
    lines: list[str] = []
    before = {r["name"]: r for r in baseline.get("results", [])}
    after = {r["name"]: r for r in current.get("results", [])}
    for name in sorted(set(before) | set(after)):
        old = before.get(name)
        new = after.get(name)
        if old is None:
            lines.append(f"+ {name}: 新增（{'解出' if new and new.get('solved') else '未解出'}）")
            continue
        if new is None:
            lines.append(f"- {name}: 本次未运行")
            continue
        flip = ""
        if bool(old.get("solved")) != bool(new.get("solved")):
            flip = "  ← 结果翻转"
        delta_cost = float(new.get("cost_usd") or 0) - float(old.get("cost_usd") or 0)
        lines.append(
            f"{'√' if new.get('solved') else '×'} {name}: "
            f"{'解出' if new.get('solved') else '未解出'}"
            f"（上次{'解出' if old.get('solved') else '未解出'}，"
            f"成本 {delta_cost:+.2f}）{flip}"
        )
    b = baseline.get("summary", {})
    c = current.get("summary", {})
    lines.append(
        f"\n解出 {b.get('solved', 0)}/{b.get('items', 0)} → {c.get('solved', 0)}/{c.get('items', 0)}"
        f"，总成本 ${b.get('cost_usd', 0):.2f} → ${c.get('cost_usd', 0):.2f}"
    )
    # The gateway doubles its rate at peak hours. Two runs taken at different
    # times differ by up to 2x in cost with no code change at all, so a quiet
    # word here stops the next comparison from being read as an improvement.
    old_peak, new_peak = b.get("peak_hours"), c.get("peak_hours")
    if old_peak is not None and new_peak is not None and bool(old_peak) != bool(new_peak):
        lines.append(
            "! 两次不在同一计费时段（网关工作日 09–12 / 14–18 点翻倍），"
            "成本差异里含最多 2 倍的时段因素，别当成优化成果。"
        )
    return lines


def save_report(path: Path, results: list[BenchResult], summary: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
        "results": [r.__dict__ for r in results],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def default_report_path(bench_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return bench_dir / "results" / f"bench-{stamp}.json"
