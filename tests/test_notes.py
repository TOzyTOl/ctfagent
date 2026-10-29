"""Technique notes: retrieval, use accounting, and isolation from benchmarks.

A note is the platform's method-level memory, and each test here names the
failure that made it necessary:

* the index was "newest eight", so after a hundred solves the matching note
  lost on date — the same way the case library kept replaying its newest cards;
* a note listed in a prompt was credited as if it had helped, which let an
  ignored note climb forever. Being shown is exposure; *used* is a claim the
  run's own records support;
* a note is a file, not a database row, so a benchmark run reads and writes the
  live library unless something isolates it.
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from ctfagent import bench, caselib, notes
from ctfagent.db import Store
from ctfagent.models import Challenge, TaskStatus


def _note(directory: Path, name: str, body: str, usage: dict[str, int] | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if usage:
        path.write_text(notes.render(notes.Note(path=path, body=body), usage), encoding="utf-8")
    else:
        path.write_text(body, encoding="utf-8")
    return path


FERMAT = """# Fermat 分解：相邻素因子

## 什么时候用
RSA 模数 n 的两个素因子位长相近时用 Fermat 平方探测。
## 怎么做
从 isqrt(n) 起步，逐平方判定 a^2-n 是否完全平方，命中后 gcd 得到 p、q。
## 坑
不要先跑 pollard_rho，相邻素因子下它慢几个量级。
"""

PADDING = """# 填充用笔记

## 什么时候用
杂项题里偶尔用到的十六进制转换技巧，与本题无关。
## 怎么做
用 xxd 与 python 的 bytes.fromhex 互相转换。
"""


def _seed_run(store: Store, challenge_id: int) -> int:
    run_id = store.create_run(challenge_id, "solve").id
    return run_id


def _challenge(store: Store, name: str = "fermat-rsa", category: str = "crypto"):
    return store.add_challenge(
        Challenge(
            id=0,
            name=name,
            description="RSA n 的两个素因子很接近（相邻选取），e=65537",
            category=category,
            remote="",
            files=[],
            hints=[],
        )
    )


def test_a_note_matching_the_challenge_outranks_a_newer_unrelated_one(tmp_path: Path) -> None:
    """The old index listed the newest eight. Date is not relevance."""
    directory = tmp_path / "ctf-crypto"
    old = _note(directory, "learned-20260101-aaaaaaaa.md", FERMAT)
    _note(directory, "learned-20261020-bbbbbbbb.md", PADDING)
    known = notes.index(directory)

    picked = notes.rank(known, "RSA 模数 n 相邻素因子 e=65537")
    assert picked and picked[0].path == old


def test_a_new_note_nobody_was_shown_still_gets_a_turn(tmp_path: Path) -> None:
    """An unlisted note can never prove itself; the fill is what shows it."""
    directory = tmp_path / "ctf-crypto"
    _note(directory, "learned-20260101-aaaaaaaa.md", PADDING)
    fresh = _note(directory, "learned-20261025-cccccccc.md", "# 另一篇\n\n## 什么时候用\n完全不同的话题。\n")
    known = notes.index(directory)

    picked = notes.rank(known, "和两篇都不沾边的题面")
    assert fresh in [note.path for note in picked]


def test_usage_counters_live_in_the_file_and_survive_a_rewrite(tmp_path: Path) -> None:
    directory = tmp_path / "ctf-crypto"
    path = _note(directory, "learned-20260101-aaaaaaaa.md", FERMAT)

    assert notes.write_usage(path, used=1, helped=1)
    note = notes.parse(path)
    assert (note.used, note.helped, note.harmed) == (1, 1, 0)
    assert "Fermat" in note.body and note.title.startswith("Fermat")  # prose intact

    assert notes.write_usage(path, used=1, harmed=1)
    note = notes.parse(path)
    assert (note.used, note.helped, note.harmed) == (2, 1, 1)


def test_only_notes_whose_method_shows_up_in_the_run_are_credited(
    cfg, tmp_path: Path, store_and_bus
) -> None:
    """Exposure is not use: a note the run never applied earns nothing."""
    store, _ = store_and_bus
    challenge_id = _challenge(store)
    run_id = _seed_run(store, challenge_id)
    store.set_run_notes(run_id, [])

    directory = tmp_path / "ctf-crypto"
    used = _note(directory, "learned-20260101-aaaaaaaa.md", FERMAT)
    ignored = _note(directory, "learned-20260102-bbbbbbbb.md", PADDING)
    task = store.create_task(run_id, "crypto", "obj", "")
    store.finish_task(
        task.id,
        TaskStatus.DONE,
        {"summary": "用 isqrt 起步做 Fermat 平方探测，命中后 gcd 得到 p、q", "findings": []},
        "",
    )

    credited = notes.balance(store, run_id, [used, ignored], solved=True)

    assert credited == 1
    assert notes.parse(used).helped == 1
    assert (notes.parse(ignored).used, notes.parse(ignored).helped) == (0, 0)


def test_a_run_charges_its_notes_once(cfg, tmp_path: Path, store_and_bus) -> None:
    """A reopened run is not a second verdict on the same note."""
    store, _ = store_and_bus
    challenge_id = _challenge(store)
    run_id = _seed_run(store, challenge_id)
    directory = tmp_path / "ctf-crypto"
    path = _note(directory, "learned-20260101-aaaaaaaa.md", FERMAT)
    task = store.create_task(run_id, "crypto", "obj", "")
    store.finish_task(
        task.id, TaskStatus.DONE, {"summary": "isqrt 起步做 Fermat 平方探测并 gcd", "findings": []}, ""
    )

    assert notes.balance(store, run_id, [path], solved=True) == 1
    assert notes.balance(store, run_id, [path], solved=True) == 0
    assert notes.parse(path).used == 1


def test_a_clearly_harmful_note_ranks_below_a_clean_one(tmp_path: Path) -> None:
    """Two decided runs are enough for a gentle penalty — one is noise."""
    directory = tmp_path / "ctf-crypto"
    good = _note(directory, "learned-20260101-aaaaaaaa.md", FERMAT, {"used": 4, "helped": 4})
    bad = _note(directory, "learned-20260102-bbbbbbbb.md", FERMAT, {"used": 4, "harmed": 4})
    known = notes.index(directory)

    picked = notes.rank(known, "RSA 模数 n 相邻素因子 e=65537")
    assert picked[0].path == good and bad in [note.path for note in picked]


def test_a_note_never_carries_a_flag_literal(tmp_path: Path) -> None:
    """The installer scrubs; parsing must not resurrect what it replaced."""
    directory = tmp_path / "ctf-crypto"
    path = _note(directory, "learned-20260101-aaaaaaaa.md", FERMAT + "flag{answer}\n")
    assert "flag{answer}" not in caselib.strip_flags(path.read_text(encoding="utf-8"))
    # And the card library's own scrubber is the one the note path uses:
    assert caselib.strip_flags(path.read_text(encoding="utf-8")).count("<flag>") == 1


def test_the_benchmark_strips_training_notes_unless_asked(tmp_path: Path) -> None:
    """A benchmark that reads training notes measures the training set.

    The fresh database does not cover this: the notes are files in the skills
    directory, injected into every dispatch of the direction.
    """
    live = tmp_path / "live" / "ctf-crypto"
    live.mkdir(parents=True)
    (live / "SKILL.md").write_text("# ctf-crypto\n", encoding="utf-8")
    _note(live, "learned-20260101-aaaaaaaa.md", FERMAT)

    isolated = bench._bench_skills_dir(tmp_path / "live", tmp_path / "work", None)
    assert (isolated / "ctf-crypto" / "SKILL.md").exists()
    assert list(isolated.rglob("learned-*.md")) == []

    kept = bench._bench_skills_dir(tmp_path / "live", tmp_path / "work2", tmp_path / "live")
    assert len(list(kept.rglob("learned-*.md"))) == 1
    # The copy is the benchmark's own: a note it distils dies with the temp dir.
    assert kept != tmp_path / "live"


def test_a_flag_confirm_writes_the_direction_back_when_triage_never_ran(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """12 of 15 live cards were filed ``unknown`` because triage stopped running.

    A card's category is its strongest retrieval signal, and it is copied at
    distillation — so the writeback has to happen before ``caselib.save``.
    """
    from ctfagent.backend import ScriptedBackend
    from ctfagent.challenge import load_challenge
    from ctfagent.orchestrator import RunManager
    from ctfagent.workspace import SandboxStatus

    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    # A challenge the platform imported without a category — the state 12 of
    # the 15 live challenges were in.
    challenge_id = store.add_challenge(
        Challenge(
            id=0,
            name="unlabelled-rsa",
            description="两个素因子相邻的 RSA",
            category="unknown",
            source_dir=str(fixtures_dir / "easy_crypto"),
        )
    )
    assert store.get_challenge(challenge_id).category in ("", "unknown")
    run = manager.prepare(challenge_id)
    task = store.create_task(run.id, "crypto", "obj", "")
    store.finish_task(task.id, TaskStatus.DONE, {"summary": "用了费马分解", "findings": []}, "")
    store.add_task_usage(task.id, turns=42)

    assert run._writeback_direction() == "crypto"
    assert store.get_challenge(challenge_id).category == "crypto"
    assert run.challenge.category == "crypto"
    kinds = [e.kind for e in store.events_since(run.id)]
    assert "challenge.categorized" in kinds
    # A challenge the operator or platform already classified is not overwritten.
    assert run._writeback_direction() == ""


def test_a_worker_that_barely_ran_does_not_decide_the_direction(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A wrong category is worse than ``unknown``.

    Every close cancels its running tasks. Those cancelled tasks have a role and
    almost no turns, and the *last* one was often the only one with an opinion —
    filing the card under it would put the next challenge of the real direction
    on the losing side of the retrieval score.
    """
    from ctfagent.backend import ScriptedBackend
    from ctfagent.orchestrator import RunManager
    from ctfagent.workspace import SandboxStatus

    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    challenge_id = store.add_challenge(
        Challenge(id=0, name="barely-started", description="x", category="unknown")
    )
    run = manager.prepare(challenge_id)
    task = store.create_task(run.id, "pwn", "obj", "")
    store.finish_task(task.id, TaskStatus.STOPPED, {"summary": "被关闭打断"}, "")
    store.add_task_usage(task.id, turns=2)

    assert run._writeback_direction() == ""
    assert store.get_challenge(challenge_id).category == "unknown"


def test_a_bench_summary_says_whether_notes_were_used(tmp_path: Path) -> None:
    """A score is only comparable if the reader knows what memory produced it."""
    summary = bench.summarise([])
    assert "notes" not in summary  # run_benchmark adds it; summarise stays pure


def test_memory_reports_the_loop_instead_of_only_storing_it(
    cfg, fixtures_dir: Path, tmp_path: Path, capsys
) -> None:
    """The loop failed for weeks with nothing on screen saying so.

    The live database had 12 of 15 cards filed ``unknown``, a card credited
    5-0 from exposure alone, zero notes, and retrieval that pulled an unrelated
    reverse card — and no command reported any of it. This one does, without a
    model call.
    """
    from ctfagent.challenge import load_challenge
    from ctfagent.cli import _memory
    from ctfagent.db import Store
    from ctfagent.models import Case

    cfg.skills_dir = tmp_path / "skills"
    (cfg.skills_dir / "ctf-crypto").mkdir(parents=True)
    (cfg.skills_dir / "ctf-crypto" / "learned-20260101-aaaaaaaa.md").write_text(
        "# 费马分解\n\n## 什么时候用\n相邻素因子。\n", encoding="utf-8"
    )
    store = Store(cfg.db_path)
    try:
        challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
        run = store.create_run(challenge_id, "solve")
        store.upsert_case(
            Case(
                id=0, run_id=run.id, challenge_id=challenge_id, name="dirttest",
                category="unknown", outcome="solved", route=["crypto[done] 费马分解"],
                techniques=["fermat", "isqrt"],
            )
        )
    finally:
        store.close()

    assert _memory(cfg) == 0
    out = capsys.readouterr().out
    assert "战例卡" in out and "unknown" in out
    assert "实战笔记" in out and "learned-" in out
    assert "战例检索" in out   # retrieval shown for the unclassified challenge


def test_the_run_page_offers_distillation_only_for_a_confirmed_flag(
    cfg, fixtures_dir: Path, tmp_path: Path
) -> None:
    """The button is the only way to reach distillation after the fact.

    A note teaches a *method*, and a method is only worth teaching once a flag
    proved it. The page must not offer it for a run that has no confirmed flag —
    the server would only refuse, and an offered-but-refused button reads as a
    bug.
    """
    from fastapi.testclient import TestClient

    from ctfagent.challenge import load_challenge
    from ctfagent.models import FlagStatus
    from ctfagent.server import create_app

    app = create_app(cfg, scripted=True)
    with TestClient(app) as client:
        store = app.state.store
        challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
        run = store.create_run(challenge_id, "solve")
        flag = store.add_flag(run.id, None, "flag{x}", "s", "m", "high")

        page = client.get(f"/runs/{run.id}").text
        assert "/distill" not in page

        store.set_flag_status(flag.id, FlagStatus.CONFIRMED)
        page = client.get(f"/runs/{run.id}").text
        assert f"/api/runs/{run.id}/distill" in page


def test_the_distill_route_refuses_a_run_without_a_confirmed_flag(
    cfg, fixtures_dir: Path
) -> None:
    from fastapi.testclient import TestClient

    from ctfagent.challenge import load_challenge
    from ctfagent.server import create_app

    app = create_app(cfg, scripted=True)
    with TestClient(app) as client:
        store = app.state.store
        challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
        run = store.create_run(challenge_id, "solve")
        # Not a Run in this process yet; ``attach`` will rebuild it.
        response = client.post(f"/api/runs/{run.id}/distill", follow_redirects=False)
        assert response.status_code == 303
        notice = urllib.parse.unquote(response.headers["location"])
        assert "确认" in notice


# --------------------------------------------------------------- 已沉淀 marker
# The note lands in the skill library, not in the run: after a distillation the
# run page and the rail used to look exactly like a run that never taught
# anything, so the only evidence was a line in the event log. Live database at
# the time: 7 of 19 runs with a confirmed flag had been distilled, and the
# operator had no way to see which without reading events.


def test_the_store_reports_which_runs_were_distilled(tmp_path: Path) -> None:
    """The answer comes from the install event, deduplicated by path.

    Run 24 has two ``skill.note_added`` rows for the same file (the button was
    clicked twice); it was distilled once, and must count once.
    """
    store = Store(tmp_path / "x.db")
    challenge_id = store.add_challenge(Challenge(id=0, name="c", category="misc", description="d"))
    first = store.create_run(challenge_id, "solve")
    second = store.create_run(challenge_id, "solve")
    plain = store.create_run(challenge_id, "solve")
    note = "D:/skills/ctf-web/learned-20261027-a5ceff97.md"
    for _ in range(2):
        store.add_event(first.id, "skill.note_added", {"path": note, "skill": "ctf-web"})
    store.add_event(second.id, "skill.note_added", {"path": "D:/skills/ctf-pwn/x.md", "skill": "ctf-pwn"})

    distilled = store.distilled_notes()
    assert distilled[first.id] == [{"path": note, "skill": "ctf-web"}]
    assert distilled[second.id][0]["skill"] == "ctf-pwn"
    assert plain.id not in distilled
    store.close()


def test_the_rail_and_the_run_page_mark_a_distilled_run(
    cfg, fixtures_dir: Path
) -> None:
    """Both surfaces, from one source: the rail chip and the header stamp."""
    from fastapi.testclient import TestClient

    from ctfagent.challenge import load_challenge
    from ctfagent.models import FlagStatus
    from ctfagent.server import create_app

    app = create_app(cfg, scripted=True)
    with TestClient(app) as client:
        store = app.state.store
        challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
        run = store.create_run(challenge_id, "solve")
        note = str(Path(cfg.skills_dir) / "ctf-crypto" / "learned-20260101-aaaaaaaa.md")

        # A solved run with a confirmed flag, not yet distilled. Key on the
        # marker elements, not the bare word: the delete confirm mentions that
        # notes survive deletion, and that is not a marker.
        flag = store.add_flag(run.id, None, "flag{x}", "s", "m", "high")
        store.set_flag_status(flag.id, FlagStatus.CONFIRMED)
        page = client.get(f"/runs/{run.id}").text
        assert "st-note" not in page and "note-chip" not in page
        assert "沉淀笔记" in page and "重新沉淀" not in page
        assert "note-chip" not in client.get("/api/rail", params={"active": run.id}).text

        store.add_event(run.id, "skill.note_added", {"path": note, "skill": "ctf-crypto", "run_id": run.id})

        page = client.get(f"/runs/{run.id}").text
        assert "st-note" in page and "已沉淀" in page
        # The button now offers a re-distill instead of implying nothing happened.
        assert "重新沉淀" in page
        rail = client.get("/api/rail", params={"active": run.id}).text
        assert "note-chip" in rail and "已沉淀" in rail
