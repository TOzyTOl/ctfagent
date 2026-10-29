"""Technique distillation: solved runs become notes in the skill library.

The case library remembers challenges — keyed to one challenge's vocabulary.
A note is how the platform remembers a *method*: trigger conditions, steps,
verified pitfalls, readable by any future worker of the direction whatever
the wording of the challenge in front of it. Each test guards one way this
goes wrong: teaching from an unverified run, carrying the flag literal,
being invisible to the workers who should apply it, or piling up
near-duplicates.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.backend import ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.models import FlagStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus

NOTE = """# Fermat 分解：两个素因子挨得很近的 n

## 什么时候用
RSA 模数 n 的两个素因子位长相近（Fermat 平方法几步内命中），
或题面暗示「素数是相邻选取的」。

## 怎么做
1. `isqrt(n)` 起步，逐平方探测 a^2-n 直到完全平方。
2. 用 gmpy2 的 iroot 判定，纯 Python 的浮点 isqrt 在大 n 上会误判。

## 坑
不要先试 rho/pollard——相邻素因子下它比 Fermat 慢几个量级（实测 2000 步内 Fermat 必中）。

## 边界
仅当 |p-q| < n^{1/4} 量级时有效；因子位长差一半以上就不要走这条路。
flag{never_appears_in_a_note}
""" + "适用场景与变体的补充说明：当 e 也异常小时，分解后先检查 d 的结构再解密。\n" * 3


@pytest.fixture()
def run(cfg, tmp_path: Path, fixtures_dir: Path, store_and_bus):
    """A run whose skills point at a throwaway library, not the real one.

    The default ``cfg.skills_dir`` is the operator's live plugin tree; a note
    installed there would reach every future run on this machine.
    """
    cfg.skills_dir = tmp_path / "skills"
    (cfg.skills_dir / "ctf-crypto").mkdir(parents=True)
    (cfg.skills_dir / "ctf-crypto" / "SKILL.md").write_text("# ctf-crypto\n", encoding="utf-8")
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    return manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))


def _confirm(run, value: str = "flag{confirmed}") -> None:
    flag = run.store.add_flag(run.id, None, value, "work/solve.py", "费马分解", "high")
    assert flag is not None
    run.store.set_flag_status(flag.id, FlagStatus.CONFIRMED)


def _notes(run) -> list[Path]:
    return sorted((run.cfg.skills_dir / "ctf-crypto").glob("learned-*.md"))


async def test_a_run_without_a_confirmed_flag_teaches_nothing(run) -> None:
    """An unverified method in the library teaches the next run to fail."""
    _ = run.store.add_flag(run.id, None, "flag{x}", "s", "m", "high")  # still candidate
    out = await run.distill_technique()
    assert "error" in out and "确认" in out["error"]
    assert _notes(run) == []


async def test_a_solved_run_becomes_a_scrubbed_note_in_the_library(run) -> None:
    _confirm(run)
    (run.workspace.work / "technique.md").write_text(NOTE, encoding="utf-8")

    out = await run.distill_technique()

    assert "error" not in out, out
    notes = _notes(run)
    assert len(notes) == 1
    body = notes[0].read_text(encoding="utf-8")
    # The note teaches the method, never the answer.
    assert "flag{" not in body and "<flag>" in body
    assert "什么时候用" in body and run.challenge.name in body  # stamped with its origin
    kinds = [e.kind for e in run.store.events_since(run.id)]
    assert "skill.note_added" in kinds and "skill.distill_dispatched" in kinds


async def test_redisilling_the_same_note_overwrites_instead_of_piling_up(run) -> None:
    _confirm(run)
    (run.workspace.work / "technique.md").write_text(NOTE, encoding="utf-8")
    first = await run.distill_technique()
    second = await run.distill_technique()

    assert "error" not in first and "error" not in second
    assert first["path"] == second["path"]  # filename derives from the content
    assert len(_notes(run)) == 1


async def test_a_note_the_writer_did_not_write_is_not_installed(run) -> None:
    _confirm(run)
    out = await run.distill_technique()
    assert "error" in out
    assert _notes(run) == []


async def test_the_directions_workers_see_the_note_the_verifier_does_not(run) -> None:
    """Discovery is the whole point: an undetected note teaches nothing."""
    _confirm(run)
    (run.workspace.work / "technique.md").write_text(NOTE, encoding="utf-8")
    await run.distill_technique()

    def spec_for(role: str):
        task = run.store.create_task(run.id, role, "obj", "ctx")
        return run._worker_spec(task)

    crypto = spec_for("crypto")
    assert "实战沉淀笔记" in crypto.system_prompt
    assert "learned-" in crypto.system_prompt and "Fermat 分解" in crypto.system_prompt
    # The verifier must reproduce from artifacts; a method index is exactly
    # the shortcut it exists to rule out.
    assert "实战沉淀笔记" not in spec_for("verifier").system_prompt
    assert "实战沉淀笔记" not in spec_for("writer").system_prompt
    # The note itself is openable through the skill-doc tool.
    name = _notes(run)[0].name
    task = run.store.create_task(run.id, "crypto", "obj", "ctx")
    from ctfagent.worker_tools import WorkerTools

    opened = WorkerTools(run, task.id).read_skill_doc(name)
    assert "Fermat" in opened.get("content", "")


def test_distillation_on_confirm_is_off_by_default() -> None:
    """It spends a writer session at the one moment a competition cares least."""
    from ctfagent.config import LimitsConfig

    assert LimitsConfig().distill_on_confirm is False


async def test_confirming_distils_only_when_the_operator_turned_it_on(run) -> None:
    """Off: confirming stays free (the note is a button/CLI away).

    On: the note is produced as part of confirmation, and a run that already
    closed is not reopened by it.
    """
    _confirm(run)
    (run.workspace.work / "technique.md").write_text(NOTE, encoding="utf-8")

    # Off by default — a confirmed flag must not start a second writer.
    flag = run.store.list_flags(run.id)[0]
    await run.confirm_flag(flag.id)
    assert _notes(run) == []
    assert [t for t in run.store.list_tasks(run.id) if t.role == "writer"] == []

    run.cfg.limits.distill_on_confirm = True
    await run._maybe_distill_on_confirm()
    assert len(_notes(run)) == 1
    assert "Fermat" in _notes(run)[0].read_text(encoding="utf-8")


async def test_a_failed_distillation_does_not_cost_the_solve(run) -> None:
    """A lost note is not a lost solve: the close has to happen either way."""
    _confirm(run)
    run.cfg.limits.distill_on_confirm = True

    # The writer produces nothing usable (no technique.md).
    await run._maybe_distill_on_confirm()

    assert _notes(run) == []
    kinds = [e.kind for e in run.store.events_since(run.id)]
    assert "skill.note_failed" in kinds
    assert run.closed is False  # only the caller closes
