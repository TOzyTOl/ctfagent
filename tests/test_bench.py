"""The benchmark is the only falsifiable claim the project makes.

Its failure modes are all quiet ones: a manifest that silently yields no items,
a regex that matches everything, a seeded library that carries the answer, or a
report trimmed down to a number that cannot be compared across weeks. Each test
below pins one of them.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from ctfagent import bench
from ctfagent.challenge import load_challenge


def _manifest(tmp_path: Path, body: str) -> Path:
    directory = tmp_path / "bench"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "bench.yaml").write_text(body, encoding="utf-8")
    return directory


def test_a_manifest_that_says_nothing_raises_instead_of_scoring_zero(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """A silently empty bench reports 0/0 and reads as "nothing regressed".

    The failure has to be loud: a manifest is written by hand, and a missing
    ``items:`` or a mis-indented entry is the likeliest way to get no results.
    """
    directory = _manifest(tmp_path, "challenges:\n  - name: easy\n")
    with pytest.raises(ValueError):
        bench.load_manifest(directory)

    empty = _manifest(tmp_path / "second", "items:\n")
    with pytest.raises(ValueError):
        bench.load_manifest(empty)


def test_an_item_resolves_its_directory_and_expected_value(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    directory = _manifest(
        tmp_path,
        "items:\n"
        "  - name: easy\n"
        f"    directory: {fixtures_dir / 'easy_rev'}\n"
        "    expect: flag{easy_rsa}\n"
        "    category: reverse\n"
        "  - name: hex\n"
        f"    directory: {fixtures_dir / 'easy_rev'}\n"
        '    expect_regex: "[0-9a-f]{32}"\n',
    )
    items = bench.load_manifest(directory)
    assert [i.name for i in items] == ["easy", "hex"]
    assert items[0].matches("flag{easy_rsa}")
    assert not items[0].matches("flag{easy_rsa}\n extra")
    assert items[1].matches("96b30dd3d5f0ec32ecdf3147fe8d39ce")
    assert not items[1].matches("96b30dd3d5f0ec32ecdf3147fe8d39c")


def test_an_item_without_an_expected_value_matches_nothing(tmp_path: Path, fixtures_dir: Path) -> None:
    """A missing answer must not turn every submission into a pass."""
    item = bench.BenchItem(name="x", directory=fixtures_dir / "easy_rev")
    assert not item.matches("anything")
    assert not item.matches("")


def test_the_declared_wrapper_is_presentation_not_part_of_the_answer() -> None:
    """easyrs scored as a miss on a real run for this reason.

    Its manifest lists the bare hex; the worker submitted ``flag{<hex>}``. Both
    are the same value. The comparison stays exact otherwise — a prefix or a
    near-miss is still a miss, because that is how a decoy gets counted.
    """
    bare = bench.BenchItem(name="e", directory=Path("."), expect="96b30dd3d5f0ec32ecdf3147fe8d39ce")
    assert bare.matches("flag{96b30dd3d5f0ec32ecdf3147fe8d39ce}")
    assert bare.matches("  FLAG{96b30DD3D5F0EC32ECDF3147FE8D39CE}  ")
    assert not bare.matches("flag{96b30dd3d5f0ec32ecdf3147fe8d39c}")
    assert not bare.matches("prefix flag{96b30dd3d5f0ec32ecdf3147fe8d39ce}")

    wrapped = bench.BenchItem(
        name="w", directory=Path("."), expect="flag{6WfLIuInIf9ZvhT5JGp1Ee6MwBoE7o3B}"
    )
    assert wrapped.matches("6WfLIuInIf9ZvhT5JGp1Ee6MwBoE7o3B")


def test_a_regex_item_accepts_the_value_with_or_without_its_wrapper() -> None:
    """``flag_format`` usually declares ``flag\\{...\\}``; a bare token is the same answer."""
    item = bench.BenchItem(
        name="r", directory=Path("."), expect_regex=r"[0-9a-f]{32}"
    )
    assert item.matches("96b30dd3d5f0ec32ecdf3147fe8d39ce")
    assert item.matches("flag{96b30dd3d5f0ec32ecdf3147fe8d39ce}")
    assert not item.matches("flag{not-hex}")


def test_an_item_can_seal_directories_of_its_own(tmp_path: Path, fixtures_dir: Path) -> None:
    """The original material folder often sits beside the solver's notes.

    n17's real folder had ``answer.json`` and ``solve.py``; curating the
    attachments did not stop a worker from grepping the host for the challenge
    name and finding the original. A sealed entry closes that for one item
    without sealing the same path for every other challenge.
    """
    directory = _manifest(
        tmp_path,
        "items:\n"
        "  - name: n17\n"
        f"    directory: {fixtures_dir / 'easy_rev'}\n"
        "    expect: 8a6179153fd89b40b1eac51e3bc6825f\n"
        '    sealed: "D:/agentsec/ctf_n17; ../sibling"\n',
    )
    item = bench.load_manifest(directory)[0]
    assert item.sealed == [
        Path("D:/agentsec/ctf_n17"),
        (directory / "../sibling").resolve(),
    ]


def test_seeding_the_library_copies_cases_and_nothing_else(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """The train/test split: cards may cross into a bench, answers may not.

    If a challenge row came along, the bench could inject its own earlier card
    and call the result "learning"; if a flag row came along it would be the
    answer sitting in the database.
    """
    from ctfagent.db import Store
    from ctfagent.models import Case

    source = tmp_path / "train.db"
    trained = Store(source)
    challenge_id = trained.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    run_id = trained.create_run(challenge_id, "solve").id
    trained.upsert_case(
        Case(
            id=0, run_id=run_id, challenge_id=challenge_id, name="trained",
            category="reverse", outcome="solved", route=["reverse[done] found it"],
            open_unknowns=["为什么没有运行时比对？"], help_count=2,
        )
    )
    trained.add_flag(run_id, None, "flag{train_answer}", "out.bin", "method", "high")
    trained.close()

    target = tmp_path / "bench.db"
    store = bench.prepare_db(target, source)
    try:
        assert store.list_challenges() == []
        assert store.list_flags(1) == []
        cards = store.list_cases()
        assert [c.name for c in cards] == ["trained"]
        assert cards[0].open_unknowns == ["为什么没有运行时比对？"]
        assert cards[0].help_count == 2
    finally:
        store.close()


def test_compare_names_the_item_that_flipped(tmp_path: Path) -> None:
    """A better average hides the only result anyone cares about."""
    baseline = {
        "summary": {"items": 2, "solved": 2, "cost_usd": 4.0},
        "results": [
            {"name": "a", "solved": True, "cost_usd": 2.0},
            {"name": "b", "solved": True, "cost_usd": 2.0},
        ],
    }
    current = {
        "summary": {"items": 2, "solved": 1, "cost_usd": 1.0},
        "results": [
            {"name": "a", "solved": False, "cost_usd": 0.5},
            {"name": "b", "solved": True, "cost_usd": 0.5},
        ],
    }
    text = "\n".join(bench.compare(baseline, current))
    assert "a" in text and "结果翻转" in text
    assert "解出 2/2 → 1/2" in text


def test_a_report_round_trips_with_everything_compare_needs(tmp_path: Path) -> None:
    result = bench.BenchResult(
        name="a", category="reverse", run_id=7, status="solved", solved=True,
        hit=True, verified_hit=False, matched_value="flag{x}", expected="flag{x}",
        cost_usd=1.25, elapsed_s=61.0, tasks=3,
        flag_statuses=[("flag{x}", "candidate")],
    )
    summary = bench.summarise([result])
    path = bench.save_report(tmp_path / "r.json", [result], summary)
    loaded = bench.load_report(path)
    assert loaded["summary"]["solve_rate"] == 1.0
    assert loaded["results"][0]["matched_value"] == "flag{x}"
    assert json.loads(path.read_text(encoding="utf-8"))["results"][0]["name"] == "a"


def test_the_duration_line_survives_a_fractional_elapsed_time() -> None:
    """``elapsed_s`` is a float, and this crashed a finished benchmark.

    When the run never recorded a finish time the bench falls back to
    ``time.time() - started``, which is fractional. ``seconds // 60`` on a float
    is a float, and ``:02d`` on a float raises — the anchor run solved its
    challenge, printed the table header, and died before writing the report.
    """
    from ctfagent.cli import _duration

    assert _duration(61.7) == "1分01秒"
    assert _duration(0.0) == "0分00秒"
    assert _duration(3599.9) == "59分59秒"
    assert _duration(3725) == "62分05秒"


async def test_each_item_is_handed_back_before_the_next_one_starts(
    tmp_path: Path, fixtures_dir: Path, cfg
) -> None:
    """The CLI saves the report from this callback, so it must fire per item.

    A single callback at the end is how the anchor run's result was lost: the
    work directory is deleted in a ``finally``, so an exception while printing
    leaves nothing at all. Firing as each item finishes keeps whatever was
    already paid for on disk.
    """
    from ctfagent.backend import ScriptedBackend

    directory = tmp_path / "bench"
    directory.mkdir(parents=True)
    (directory / "bench.yaml").write_text(
        "items:\n"
        "  - name: one\n"
        f"    directory: {fixtures_dir / 'easy_rev'}\n"
        "    expect: flag{right}\n"
        "  - name: two\n"
        f"    directory: {fixtures_dir / 'easy_crypto'}\n"
        "    expect: flag{right}\n",
        encoding="utf-8",
    )

    seen: list[tuple[str, int]] = []

    async def play(run) -> None:
        await run.propose_flag("flag{right}", "scripted:", "out.bin", "high")
        run.request_finish("failed", "done")

    def collect(result) -> None:
        seen.append((result.name, len(seen)))

    await bench.run_benchmark(
        bench.load_manifest(directory), cfg, poll_s=0.05, on_run=play,
        on_result=collect, backend=ScriptedBackend(),
    )
    assert seen == [("one", 0), ("two", 1)]


async def test_a_bench_run_is_scored_on_the_submitted_value_not_on_confidence(
    tmp_path: Path, fixtures_dir: Path, cfg
) -> None:
    """The end-to-end path, with the backend scripted.

    Two challenges: the lead submits the right value on the first and a wrong
    one — with the same "high" confidence — on the second. A bench that scored
    confidence would report 2/2 and flatter the platform.

    ``on_run`` is the seam that lets the test play the lead: it is called as
    soon as the run exists and closes the run without consulting any model.
    """
    from ctfagent.backend import ScriptedBackend

    backend = ScriptedBackend()
    submitted = iter([("flag{right}", "high"), ("flag{nope}", "high")])

    async def play(run) -> None:
        value, confidence = next(submitted)
        await run.propose_flag(value, "scripted:", "out.bin", confidence)
        run.request_finish("failed", "scripted lead is done")

    directory = tmp_path / "bench"
    directory.mkdir(parents=True)
    (directory / "bench.yaml").write_text(
        "items:\n"
        "  - name: good\n"
        f"    directory: {fixtures_dir / 'easy_rev'}\n"
        "  - name: bad\n"
        f"    directory: {fixtures_dir / 'easy_crypto'}\n",
        encoding="utf-8",
    )
    items = bench.load_manifest(directory)
    for item in items:
        item.expect = "flag{right}"

    results, summary = await bench.run_benchmark(
        items, cfg, poll_s=0.05, on_run=play, backend=backend
    )
    assert [r.solved for r in results] == [True, False]
    assert summary["solved"] == 1
    assert results[1].flag_statuses  # the wrong value was submitted, and still lost
    # Nothing survives the invocation: the next bench cannot inherit this one.
    assert summary["bench_db"] == ""


async def test_a_bench_keeps_the_analysis_but_not_the_answers_of_a_leftover_db(
    tmp_path: Path, fixtures_dir: Path, cfg
) -> None:
    """``--keep-db`` is for inspection; its path must not be mistaken for a library."""
    from ctfagent.backend import ScriptedBackend

    directory = tmp_path / "bench"
    directory.mkdir(parents=True)
    (directory / "bench.yaml").write_text(
        "items:\n"
        "  - name: good\n"
        f"    directory: {fixtures_dir / 'easy_rev'}\n"
        "    expect: flag{right}\n",
        encoding="utf-8",
    )
    items = bench.load_manifest(directory)
    keep = tmp_path / "kept.db"
    store = bench.prepare_db(keep)
    store.close()

    async def close_now(run) -> None:
        run.request_finish("failed", "done")

    _, summary = await bench.run_benchmark(
        items, cfg, poll_s=0.05, bench_db=keep, keep_db=True, on_run=close_now,
        backend=ScriptedBackend(),
    )
    assert summary["bench_db"] == str(keep)
    assert keep.exists()


def test_a_library_written_before_a_column_existed_still_seeds_the_bench(
    tmp_path: Path,
) -> None:
    """The operator's training DB is older than this week's schema.

    Seeding it used to fail with ``no such column: open_unknowns``, which would
    have meant "you cannot measure until you migrate by hand". The cards are the
    valuable part; a column the database does not have yet has an obvious empty.
    """
    import sqlite3

    source = tmp_path / "old.db"
    conn = sqlite3.connect(source)
    conn.executescript(
        """
        CREATE TABLE cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, challenge_id INTEGER,
            name TEXT, category TEXT, outcome TEXT, route TEXT, derivation TEXT,
            dead_ends TEXT, techniques TEXT, cost_usd REAL, elapsed_s REAL,
            used_count INTEGER DEFAULT 0, created_at TEXT, last_used_at TEXT
        );
        INSERT INTO cases
            (run_id, challenge_id, name, category, outcome, route, derivation,
             dead_ends, techniques, cost_usd, elapsed_s, used_count)
        VALUES (1, 1, 'legacy', 'reverse', 'solved', '["a"]', 'd', '[]', '["t"]', 1.0, 2.0, 1);
        """
    )
    conn.commit()
    conn.close()

    store = bench.prepare_db(tmp_path / "bench.db", source)
    try:
        (card,) = store.list_cases()
        assert card.name == "legacy"
        assert card.route == ["a"]
        assert card.open_unknowns == [] and card.help_count == 0
    finally:
        store.close()


def _skills_tree(root: Path, notes: bool = True) -> Path:
    skill = root / "ctf-crypto"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# ctf-crypto\n", encoding="utf-8")
    if notes:
        (skill / "learned-20260101-aaaaaaaa.md").write_text(
            "# 费马分解\n\n## 什么时候用\n相邻素因子。\n", encoding="utf-8"
        )
    return root


def test_the_bench_gets_a_skill_tree_without_training_notes(tmp_path: Path) -> None:
    """A note is a file, so the fresh database does not isolate it.

    Without this the benchmark reads the notes distilled from its own training
    runs — measuring memory of the training set, exactly what ``prepare_db``'s
    fresh database exists to prevent — and a benchmark run that distils a note
    writes into the live library, contaminating real runs.
    """
    live = _skills_tree(tmp_path / "live")
    dest = bench._bench_skills_dir(live, tmp_path / "work", None)

    assert dest != live
    assert (dest / "ctf-crypto" / "SKILL.md").exists()
    assert not list(dest.rglob("learned-*.md"))
    # The live library is untouched.
    assert len(list(live.rglob("learned-*.md"))) == 1


def test_the_bench_uses_training_notes_only_when_opted_in(tmp_path: Path) -> None:
    """``--notes`` is the explicit "measure with what training taught" switch."""
    live = _skills_tree(tmp_path / "live", notes=False)
    training = _skills_tree(tmp_path / "training", notes=True)

    dest = bench._bench_skills_dir(live, tmp_path / "work", training)

    copied = list(dest.rglob("learned-*.md"))
    assert len(copied) == 1 and "费马" in copied[0].read_text(encoding="utf-8")


async def test_a_bench_run_works_in_a_skill_tree_it_can_throw_away(
    tmp_path: Path, fixtures_dir: Path, cfg
) -> None:
    """The other direction of the leak: whatever a benchmark run writes stays here.

    ``_install_learned_note`` writes into ``cfg.skills_dir``. Under the old
    wiring that was the operator's live plugin tree, so a benchmark run's note
    — distilled from a challenge that is by definition part of the test set —
    would be injected into every later real run.
    """
    from ctfagent.backend import ScriptedBackend

    cfg.skills_dir = _skills_tree(tmp_path / "live")
    directory = tmp_path / "bench"
    directory.mkdir(parents=True)
    (directory / "bench.yaml").write_text(
        "items:\n"
        "  - name: good\n"
        f"    directory: {fixtures_dir / 'easy_crypto'}\n"
        "    expect: flag{right}\n",
        encoding="utf-8",
    )
    items = bench.load_manifest(directory)

    seen: dict[str, Path] = {}

    async def play(run) -> None:
        seen["skills"] = Path(run.cfg.skills_dir)
        (run.workspace.work / "technique.md").write_text(
            "# 笔记\n\n" + "费马分解说明。" * 40, encoding="utf-8"
        )
        await run.propose_flag("flag{right}", "scripted:", "out.bin", "high")
        run.request_finish("failed", "scripted lead is done")

    _, summary = await bench.run_benchmark(
        items, cfg, poll_s=0.05, on_run=play, backend=ScriptedBackend()
    )

    assert seen["skills"] != Path(cfg.skills_dir), "the bench wrote into the live skill tree"
    assert summary["notes"] == "（未使用训练笔记）"
    live_notes = list(Path(cfg.skills_dir).rglob("learned-*.md"))
    assert len(live_notes) == 1  # the fixture's training note, and nothing new


def test_peak_hours_follow_the_gateways_own_doubling_rule() -> None:
    """A cost comparison across two runs measures the clock unless this is known.

    The gateway doubles its rate on weekdays 09–12 and 14–18 Asia/Shanghai, so
    two identical benchmarks run six hours apart differ by up to 2x. The summary
    records which window it ran in, and ``compare`` says so when they differ.
    """
    from datetime import datetime, timezone, timedelta

    cst = timezone(timedelta(hours=8))
    # Tuesday 2026-10-27
    assert bench.peak_hours(datetime(2026, 10, 27, 10, 0, tzinfo=cst)) is True
    assert bench.peak_hours(datetime(2026, 10, 27, 15, 0, tzinfo=cst)) is True
    assert bench.peak_hours(datetime(2026, 10, 27, 12, 30, tzinfo=cst)) is False
    assert bench.peak_hours(datetime(2026, 10, 27, 20, 0, tzinfo=cst)) is False
    # Saturday is never peak, even mid-morning.
    assert bench.peak_hours(datetime(2026, 10, 31, 10, 0, tzinfo=cst)) is False


def test_a_summary_always_records_whether_it_ran_at_peak() -> None:
    result = bench.BenchResult(
        name="a", category="crypto", run_id=1, status="solved", solved=True,
        hit=True, verified_hit=True, matched_value="x", expected="x",
        cost_usd=1.0, elapsed_s=10.0, tasks=1, flag_statuses=[],
    )
    summary = bench.summarise([result])
    assert isinstance(summary["peak_hours"], bool)


def test_compare_warns_when_the_two_runs_were_billed_at_different_rates() -> None:
    """Silence here would attribute a 2x clock effect to the code."""
    old = {"summary": {"items": 1, "solved": 1, "cost_usd": 10.0, "peak_hours": True},
           "results": [{"name": "a", "solved": True, "cost_usd": 10.0}]}
    new = {"summary": {"items": 1, "solved": 1, "cost_usd": 5.0, "peak_hours": False},
           "results": [{"name": "a", "solved": True, "cost_usd": 5.0}]}
    text = "\n".join(bench.compare(old, new))
    assert "翻倍" in text

    same = {"summary": {"items": 1, "solved": 1, "cost_usd": 4.0, "peak_hours": False},
            "results": [{"name": "a", "solved": True, "cost_usd": 4.0}]}
    assert "翻倍" not in "\n".join(bench.compare(same, new))


def test_the_bench_table_renders_every_outcome_without_raising() -> None:
    """The table is what the operator reads; it is also where the run died.

    Four shapes, because they take different branches: solved, wrong value,
    hard error, and nothing submitted at all. The original crash was a float
    reaching ``:02d`` on the very first row, which is why every branch here
    carries a fractional ``elapsed_s``.
    """
    from ctfagent.cli import _bench_lines

    def r(name, **kw):
        base = dict(name=name, category="crypto", run_id=1, status="stopped",
                    solved=False, hit=False, verified_hit=False, matched_value="",
                    expected="x", cost_usd=0.0, elapsed_s=61.7, tasks=1,
                    flag_statuses=[], error="")
        base.update(kw)
        return bench.BenchResult(**base)

    results = [
        r("solved", solved=True, hit=True, matched_value="987654321", cost_usd=0.27),
        r("wrong", flag_statuses=[("flag{nope}", "rejected")]),
        r("broken", error="ValueError: boom"),
        r("empty"),
    ]
    summary = bench.summarise(results)
    lines = _bench_lines(results, summary)
    text = "\n".join(lines)

    assert "987654321" in text                 # the value that was found
    assert "没有提交匹配的 flag" in text        # submitted, but wrong
    assert "ValueError: boom" in text          # errored
    assert "没有提交任何 flag" in text          # nothing at all
    assert "1分01秒" in text                   # the float, rendered
    assert "解出 1/4" in text
    # A Chinese Windows console is GBK, and ``print`` of a character outside that
    # code page raises. The report is written first, but the operator loses the
    # summary and sees a traceback for a run that was fine — that is exactly how
    # the easyrs benchmark ended on 2026-10-27 (``✗``).
    text.encode("cp936")


def test_the_comparison_also_survives_a_gbk_console() -> None:
    """``--baseline`` prints its own glyphs; both runs must stay printable."""
    old = {"summary": {"items": 1, "solved": True, "cost_usd": 3.0, "peak_hours": True},
           "results": [{"name": "a", "solved": True, "cost_usd": 3.0}]}
    new = {"summary": {"items": 1, "solved": False, "cost_usd": 1.0, "peak_hours": False},
           "results": [{"name": "a", "solved": False, "cost_usd": 1.0}]}
    "\n".join(bench.compare(old, new)).encode("cp936")


def test_the_bench_command_reaches_its_output_and_numbers(
    tmp_path: Path, cfg, monkeypatch, capsys
) -> None:
    """``main(["bench", ...])`` end to end, with the run itself faked.

    The one time this path ran for real it solved the challenge and then died
    formatting the table, leaving no report behind: the work directory is
    removed in a ``finally``. This drives the command's own plumbing — the
    per-item report flush, the table, the summary and the exit code — without a
    model, so a crash here costs a test run instead of a paid benchmark.
    """
    from ctfagent import bench as bench_mod
    from ctfagent import cli

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    directory = _manifest(tmp_path, "items:\n  - name: one\n    directory: ./\n")

    def fake_result(name: str, solved: bool, cost: float) -> bench_mod.BenchResult:
        return bench_mod.BenchResult(
            name=name, category="crypto", run_id=1, status="closed", solved=solved,
            hit=solved, verified_hit=solved,
            matched_value="123456" if solved else "", expected="123456",
            cost_usd=cost, elapsed_s=61.7, tasks=1, flag_statuses=[], error="",
        )

    async def fake_run(items, config, **kwargs):
        results = [fake_result("one", True, 0.5), fake_result("two", False, 1.25)]
        for result in results:
            kwargs["on_result"](result)  # the CLI saves the report from here
        return results, bench_mod.summarise(results)

    monkeypatch.setattr(bench_mod, "run_benchmark", fake_run)
    out = tmp_path / "report.json"

    code = cli.main(["bench", str(directory), "--out", str(out)])
    text = capsys.readouterr().out

    assert code == 1, "one of two solved is a partial pass, not success"
    assert "解出 1/2" in text and "总成本 $1.75" in text
    assert "1分01秒" in text
    assert out.exists()  # the unit that was paid for is on disk
    assert [r["name"] for r in json.loads(out.read_text(encoding="utf-8"))["results"]] == [
        "one",
        "two",
    ]

    # And the diff an operator actually reads: --baseline against that file.
    code = cli.main(["bench", str(directory), "--out", str(tmp_path / "new.json"),
                     "--baseline", str(out)])
    text = capsys.readouterr().out
    assert "对比：" in text
    assert code == 1


def test_the_bench_command_refuses_an_unknown_only_filter(tmp_path: Path, cfg, monkeypatch, capsys) -> None:
    """``--only`` with a typo must say so, not run zero items and report success."""
    from ctfagent import cli

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    directory = _manifest(tmp_path, "items:\n  - name: one\n    directory: ./\n")

    assert cli.main(["bench", str(directory), "--only", "nope"]) == 2
    assert "没有匹配的条目" in capsys.readouterr().err


def test_a_report_records_the_assignment_that_produced_it(cfg) -> None:
    """Two reports from two model sets were indistinguishable but for a timestamp.

    The 2026-10-30 comparison nearly read a cost difference as a code change
    when it was a placeholder price table, because nothing in the JSON said
    which models had been live.
    """
    from ctfagent.cli import _bench_lines

    cfg.models.worker = "m-one"
    cfg.models.roles = {"reverse": "m-two"}
    fingerprint = bench.config_fingerprint(cfg)

    assert fingerprint["models"]["worker"] == "m-one"
    assert fingerprint["models"]["roles"] == {"reverse": "m-two"}
    assert fingerprint["limits"]["task_timeout_s"] == cfg.limits.task_timeout_s

    lines = _bench_lines([], {"solved": 0, "items": 0, "cost_usd": 0,
                              "elapsed_s": 0, "config": fingerprint})
    assert "worker=m-one" in lines[0]
    assert "reverse=m-two" in lines[0]


def test_host_layer_access_is_contamination_not_a_solve() -> None:
    """ezcms 2026-10-31: a misc worker ran ``docker exec ezcms-lab cat /flag``.

    The web workers had found the E-string injection and the admin hashes, but
    the value came from the host's Docker daemon. Counting that as solved
    measures the machine's permissions, not the platform's skill — and sealing
    files does not seal the socket.
    """
    assert bench.contamination_signal(["docker exec ezcms-lab cat /flag"])
    assert bench.contamination_signal(["docker cp ezcms-lab:/flag ."])
    assert bench.contamination_signal(["docker inspect ezcms-lab"])
    assert bench.contamination_signal(["podman exec x cat /flag"])
    assert bench.contamination_signal(["kubectl exec pod -- cat /flag"])
    assert bench.contamination_signal(["curl http://127.0.0.1:18080/search?q=x"]) == ""
    # The rule text itself must not match, or every benchmark run is flagged.
    assert bench.contamination_signal([bench._BENCH_RULE]) == ""

    result = bench.BenchResult(
        name="ezcms",
        category="web",
        run_id=1,
        status="failed",
        solved=False,
        hit=True,
        verified_hit=False,
        matched_value="flag{x}",
        expected="flag{x}",
        cost_usd=0.1,
        elapsed_s=1.0,
        tasks=1,
        contaminated=True,
        contamination="docker exec ezcms-lab cat /flag",
    )
    summary = bench.summarise([result])
    assert summary["solved"] == 0
    assert summary["contaminated"] == 1


def test_the_table_marks_a_contaminated_run(cfg) -> None:
    """The number that matters is visible in the table, not only in the JSON."""
    from ctfagent.cli import _bench_lines

    result = bench.BenchResult(
        name="ezcms",
        category="web",
        run_id=1,
        status="failed",
        solved=False,
        hit=True,
        verified_hit=False,
        matched_value="flag{x}",
        expected="flag{x}",
        cost_usd=0.1,
        elapsed_s=1.0,
        tasks=1,
        contaminated=True,
        contamination="docker exec ezcms-lab cat /flag",
    )
    lines = _bench_lines(
        [result],
        {"solved": 0, "items": 1, "contaminated": 1, "cost_usd": 0.1,
         "elapsed_s": 1, "config": {}},
    )
    text = "\n".join(lines)
    assert "污染" in text
    assert "不计解出" in text


async def test_a_bench_report_carries_the_board_numbers(tmp_path: Path, fixtures_dir: Path, cfg) -> None:
    """§8 of the design doc: board mode cannot be judged without its numbers.

    The metrics are read from the benchmark's own database and must reach both
    the JSON summary and the operator's table — a number that only exists in a
    discarded temp file is not a measurement. Hypotheses are posted from the
    ``on_run`` seam, which is what the lead does in board mode; the run itself
    is left in the default (star) shape so the test does not depend on the
    coordinator's timing.
    """
    from ctfagent import swarm
    from ctfagent.backend import ScriptedBackend
    from ctfagent.cli import _bench_lines

    directory = tmp_path / "bench"
    directory.mkdir(parents=True)
    (directory / "bench.yaml").write_text(
        "items:\n"
        "  - name: good\n"
        f"    directory: {fixtures_dir / 'easy_crypto'}\n"
        "    expect: flag{right}\n",
        encoding="utf-8",
    )

    async def play(run) -> None:
        run.post_hypothesis(
            family="crypto", statement="模数可分解",
            criterion="枚举 10^6 内无因子", evidence="p、q 相差很小",
        )
        await run.propose_flag("flag{right}", "scripted:", "out.bin", "high")
        run.request_finish("failed", "scripted lead is done")

    _, summary = await bench.run_benchmark(
        bench.load_manifest(directory), cfg, poll_s=0.05, on_run=play,
        backend=ScriptedBackend(),
    )

    board = summary["board"]
    assert board["hypotheses"] == 1
    assert board["tasks_bound"] == 0
    assert "families_with_two_claimants" in board
    text = "\n".join(_bench_lines([], {"solved": 0, "items": 1, "cost_usd": 0,
                                       "elapsed_s": 1, "config": {}, "board": board}))
    assert "板子：" in text and "1 条假设" in text
    # Latency is shown when the board recorded it, and omitted (not "0分00秒")
    # when the run never reached the stage.
    summary["board"]["claim_latency_s"] = 7.0
    summary["board"]["idle_events"] = 0
    text = "\n".join(_bench_lines([], {"solved": 0, "items": 1, "cost_usd": 0,
                                       "elapsed_s": 1, "config": {}, "board": summary["board"]}))
    assert "上板→认领 0分07秒" in text and "认领→结论" not in text
    # Zero is a measurement (the board never converged), so it is printed;
    # ``None`` means the database predates the event and must stay silent.
    assert "收敛到 idle 0 次" in text
    summary["board"]["idle_events"] = None
    text = "\n".join(_bench_lines([], {"solved": 0, "items": 1, "cost_usd": 0,
                                       "elapsed_s": 1, "config": {}, "board": summary["board"]}))
    assert "收敛到 idle" not in text
    # Peak concurrency is printed when the database has task rows; a bench with
    # no tasks says nothing rather than "0 个".
    summary["board"]["peak_concurrency"] = 3
    text = "\n".join(_bench_lines([], {"solved": 0, "items": 1, "cost_usd": 0,
                                       "elapsed_s": 1, "config": {}, "board": summary["board"]}))
    assert "并发峰值 3 个 worker" in text
    text.encode("cp936")


def test_board_metrics_count_overlap_not_retries(tmp_path: Path) -> None:
    """The duplicate-dispatch number must not fire on a legitimate retry.

    A direction whose first worker hit its ceiling is claimed again by the next
    one — that is the board working, and counting claimants per family would
    report it as the rule being broken. What the rule forbids is two sessions of
    one family *alive at once*, so the metric reads the task clock.
    """
    from ctfagent.db import Store

    db = tmp_path / "board.db"
    store = Store(db)
    try:
        challenge_id = store.add_challenge(load_challenge(Path("tests/fixtures/easy_crypto")))
        run_id = store.create_run(challenge_id, "solve").id
        from ctfagent import swarm

        # Two retries of one direction, back to back: no overlap.
        row, _ = store.add_hypothesis(
            run_id,
            swarm.Hypothesis(
                family="crypto", statement="模数可分解",
                criterion="枚举 10^6 内无因子", evidence="p、q 相差很小",
            ),
        )
        first = store.create_task(run_id, "crypto", "obj1", "", row.id)
        second = store.create_task(run_id, "crypto", "obj2", "", row.id)
        store.mark_task_started(first.id)
        store.finish_task(first.id, "exhausted", {}, "")
        store.mark_task_started(second.id)
        store.finish_task(second.id, "done", {}, "")
        # A second family, one worker.
        web = store.create_task(run_id, "web", "obj3", "", 2)
        store.mark_task_started(web.id)
        store.finish_task(web.id, "done", {}, "")
    finally:
        store.close()

    metrics = bench.swarm_metrics(db)
    assert metrics["tasks_bound"] == 3
    assert metrics["families_with_two_claimants"] == 0


def test_board_metrics_count_peak_concurrency_not_session_count(tmp_path: Path) -> None:
    """The number that decides whether raising a bound is worth anything.

    Three workers that never ran together is a peak of 1, not 3. A verifier
    alive while two directions run is a peak of 2, not 3: it is not a second
    direction and the coordinator's bound does not count it.
    """
    import sqlite3

    from ctfagent.db import Store

    db = tmp_path / "peak.db"
    store = Store(db)
    try:
        challenge_id = store.add_challenge(load_challenge(Path("tests/fixtures/easy_crypto")))
        run_id = store.create_run(challenge_id, "solve").id
        a = store.create_task(run_id, "web", "1", "", None)
        b = store.create_task(run_id, "crypto", "2", "", None)
        v = store.create_task(run_id, "verifier", "3", "", None)
        c = store.create_task(run_id, "reverse", "4", "", None)
    finally:
        store.close()
    conn = sqlite3.connect(db)
    spans = {
        a.id: ("2026-11-01 10:00:00", "2026-11-01 10:10:00"),
        b.id: ("2026-11-01 10:11:00", "2026-11-01 10:20:00"),  # after a
        v.id: ("2026-11-01 10:11:00", "2026-11-01 10:15:00"),  # overlaps b
        c.id: ("2026-11-01 10:15:00", "2026-11-01 10:30:00"),  # verifier gone
    }
    for task_id, (start, end) in spans.items():
        conn.execute(
            "UPDATE tasks SET started_at=?, finished_at=? WHERE id=?", (start, end, task_id)
        )
    conn.commit()
    conn.close()

    metrics = bench.swarm_metrics(db)
    # b and c do overlap (10:11-10:20 vs 10:15-10:30) while the verifier never
    # counts, so the peak is two directions even though four sessions existed.
    assert metrics["peak_concurrency"] == 2


def test_board_metrics_report_a_real_overlap(tmp_path: Path) -> None:
    """The other side of the number: two sessions of one family at once is a
    defect, and the report has to be able to see it — otherwise the zero above
    proves nothing."""
    import sqlite3

    from ctfagent.db import Store

    db = tmp_path / "overlap.db"
    store = Store(db)
    try:
        challenge_id = store.add_challenge(load_challenge(Path("tests/fixtures/easy_crypto")))
        run_id = store.create_run(challenge_id, "solve").id
        from ctfagent import swarm

        row, _ = store.add_hypothesis(
            run_id,
            swarm.Hypothesis(
                family="crypto", statement="模数可分解",
                criterion="枚举 10^6 内无因子", evidence="p、q 相差很小",
            ),
        )
        a = store.create_task(run_id, "crypto", "obj1", "", row.id)
        b = store.create_task(run_id, "crypto", "obj2", "", row.id)
    finally:
        store.close()

    conn = sqlite3.connect(db)
    conn.execute("UPDATE tasks SET started_at='2026-11-01 10:00:00', finished_at='2026-11-01 10:10:00' WHERE id=?", (a.id,))
    conn.execute("UPDATE tasks SET started_at='2026-11-01 10:05:00', finished_at='2026-11-01 10:20:00' WHERE id=?", (b.id,))
    conn.commit()
    conn.close()

    assert bench.swarm_metrics(db)["families_with_two_claimants"] == 1


def test_board_latency_metrics_pair_first_events_and_say_nothing_when_missing(
    tmp_path: Path,
) -> None:
    """§8 survival-time: posted → claimed, claimed → settled.

    Two failure modes are pinned. Pairing *every* dispatch with the next settle
    event pairs one worker's start with another worker's finish and reports a
    latency nobody experienced; and a run that never settled must be ``None``
    rather than 0, or "never finished" reads as "finished instantly".
    """
    import sqlite3

    from ctfagent.db import Store

    db = tmp_path / "latency.db"
    store = Store(db)
    try:
        challenge_id = store.add_challenge(load_challenge(Path("tests/fixtures/easy_crypto")))
        run_id = store.create_run(challenge_id, "solve").id
        from ctfagent import swarm

        store.add_hypothesis(
            run_id,
            swarm.Hypothesis(
                family="crypto", statement="模数可分解",
                criterion="枚举 10^6 内无因子", evidence="p、q 相差很小",
            ),
        )
    finally:
        store.close()

    conn = sqlite3.connect(db)
    # Times are backdated by hand: the events table stamps ``now``.
    stamps = {
        "hypothesis.posted": "2026-11-02 10:00:00",
        "board.dispatched": "2026-11-02 10:00:07",
        # A second dispatch, two minutes later, that must not be paired with
        # the first settle.
        "hypothesis.supported": "2026-11-02 10:01:30",
    }
    for kind, stamp in stamps.items():
        cur = conn.execute(
            "INSERT INTO events (run_id, kind, payload, created_at) VALUES (?,?,?,?)",
            (run_id, kind, "{}", stamp),
        )
    conn.execute(
        "INSERT INTO events (run_id, kind, payload, created_at) VALUES (?,?,?,?)",
        (run_id, "board.dispatched", "{}", "2026-11-02 10:02:00"),
    )
    conn.commit()
    conn.close()

    metrics = bench.swarm_metrics(db)
    assert metrics["claim_latency_s"] == 7.0
    assert metrics["settle_latency_s"] == 83.0  # 10:00:07 -> 10:01:30

    # A run whose board never settled has no latency to report.
    db2 = tmp_path / "unsettled.db"
    store = Store(db2)
    try:
        challenge_id = store.add_challenge(load_challenge(Path("tests/fixtures/easy_crypto")))
        run2 = store.create_run(challenge_id, "solve").id
        store.add_hypothesis(
            run2,
            swarm.Hypothesis(
                family="crypto", statement="模数可分解",
                criterion="枚举 10^6 内无因子", evidence="p、q 相差很小",
            ),
        )
    finally:
        store.close()
    conn2 = sqlite3.connect(db2)
    conn2.execute(
        "INSERT INTO events (run_id, kind, payload, created_at) VALUES (?,?,?,?)",
        (run2, "hypothesis.posted", "{}", "2026-11-02 10:00:00"),
    )
    conn2.commit()
    conn2.close()
    metrics2 = bench.swarm_metrics(db2)
    assert metrics2["claim_latency_s"] is None
    assert metrics2["settle_latency_s"] is None
    # This database has board events but no ``board.idle``: zero convergences is
    # the measurement, not "unmeasured".
    assert metrics2["idle_events"] == 0


def test_rmtree_force_removes_read_only_git_objects(tmp_path: Path) -> None:
    """``shutil.rmtree(ignore_errors=True)`` cannot delete read-only files, so a
    workdir with a ``.git`` leaks on Windows. The helper clears the read-only
    bit and retries, and the directory actually goes away."""
    work = tmp_path / "ctfagent-bench-leaky"
    git = work / "workspaces" / "run-1" / "easy" / "work" / ".git" / "objects"
    git.mkdir(parents=True)
    (git / "readonly.bin").write_bytes(b"\x00" * 8)
    # Read-only, exactly like the real .git objects on Windows.
    os.chmod(git / "readonly.bin", stat.S_IREAD)

    bench._rmtree_force(work)
    assert not work.exists()


def test_sweep_stale_bench_dirs_removes_only_old_leftovers(tmp_path: Path, monkeypatch) -> None:
    import time as _time
    root = tmp_path
    old = root / "ctfagent-bench-old"
    fresh = root / "ctfagent-bench-fresh"
    keep = root / "not-a-bench-dir"
    for d in (old, fresh, keep):
        (d / "workspaces").mkdir(parents=True)

    monkeypatch.setattr("ctfagent.bench._rmtree_force", lambda p: None)  # just count
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(root))
    now = 100000.0
    old.touch()  # mtime = now-ish via touch default; we pass explicit now

    # Make `old` look old, `fresh` look fresh.
    os.utime(old, (now - 7 * 3600, now - 7 * 3600))
    os.utime(fresh, (now - 60, now - 60))

    removed = bench._sweep_stale_bench_dirs(now)
    # old was swept, fresh was not, non-bench dir ignored.
    assert removed == 1
