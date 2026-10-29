"""The authored bench challenges: ground truth that must hold forever.

The benchmark's value is that its answers never drift. Every test here guards
one way that could break silently: a manifest typo that scores a solvable
challenge as a miss, a regenerated material set whose answer differs from the
one recorded, or a "unique" password that was never actually unique. The
solvers run for real, from materials only — the same discipline demanded of
the platform being measured.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from ctfagent.bench import load_manifest
from ctfagent.challenge import load_challenge
from ctfagent.config import REPO_ROOT

BENCH_DIR = REPO_ROOT / "bench"
SEALED = BENCH_DIR / "sealed"
AUTHORED = ("lcg_step", "aes_pin", "rsa_broadcast", "crackme_z3", "rsa_wiener", "sha256_ext")


def test_the_manifest_is_eight_wellformed_items() -> None:
    items = load_manifest(BENCH_DIR)
    assert len(items) == 8
    assert [i.name for i in items][-6:] == list(AUTHORED)
    for item in items:
        assert (item.directory / "challenge.yaml").exists(), item.name
        # whatever the layout (materials/ subdir or files beside the yaml), a
        # bench item with nothing to solve from is unscoreable
        challenge = load_challenge(item.directory)
        assert challenge.files, item.name
        assert item.expect or item.expect_regex


def test_every_manifest_answer_matches_its_sealed_answer_file() -> None:
    """A typo in ``expect`` scores a solvable challenge as a miss forever."""
    items = {i.name: i for i in load_manifest(BENCH_DIR)}
    for name in AUTHORED:
        answer = (SEALED / name / "answer.txt").read_text(encoding="utf-8").strip()
        assert items[name].expect == answer, name
        # the judge tolerates the flag{} wrapper on either side — but only one
        # layer: an answer that already carries it must be submitted as-is
        assert items[name].matches(answer)
        if not answer.startswith(("flag{", "ctf{", "key{")):
            assert items[name].matches(f"flag{{{answer}}}")


def test_every_authored_challenge_is_solvable_from_its_materials() -> None:
    """Each sealed solver must reproduce the answer without touching it.

    This is the same bar the platform is held to: start from the materials,
    end at the answer. If a solver stops working (a library change, a mutated
    material), the bench item is broken and every score recorded against it is
    meaningless.
    """
    for name in AUTHORED:
        proc = subprocess.run(
            [sys.executable, str(SEALED / name / "solve.py")],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=REPO_ROOT,
        )
        answer = (SEALED / name / "answer.txt").read_text(encoding="utf-8").strip()
        assert proc.returncode == 0, f"{name}: {proc.stderr[-400:]}"
        assert proc.stdout.strip() == answer, name


def test_the_crackme_accepts_its_password_and_only_readable_chars() -> None:
    """The emitted checker must accept the recorded password."""
    spec = importlib.util.spec_from_file_location(
        "crackme", BENCH_DIR / "challenges" / "crackme_z3" / "materials" / "crackme.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = (SEALED / "crackme_z3" / "answer.txt").read_text(encoding="utf-8").strip()
    assert len(answer) == 16
    assert all(0x21 <= ord(c) <= 0x7E for c in answer)
    assert module.check(answer)
    assert not module.check("A" * 16)
