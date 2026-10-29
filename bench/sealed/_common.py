"""Shared deterministic construction helpers for the authored bench challenges.

Every generator here is deterministic: fixed seeds, fixed constants, and
``gmpy2.next_prime`` (a deterministic operation) — the same run always produces
the same materials and the same answer. A bench item whose answer could drift
between builds would silently invalidate every baseline recorded against it.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import gmpy2

#: One seeded source per generator keeps generated values reproducible and
#: independent of the order a generator draws in.
GLOBAL_SEED = 20261024


def rng(tag: str) -> random.Random:
    """A private deterministic RNG per purpose."""
    return random.Random(f"{GLOBAL_SEED}:{tag}")


def prime(bits: int, tag: str) -> int:
    """A deterministic probable prime of exactly ``bits`` bits."""
    r = rng(tag)
    while True:
        candidate = r.getrandbits(bits) | (1 << (bits - 1)) | 1
        n = int(gmpy2.next_prime(candidate))
        if n.bit_length() == bits:
            return n


def b2i(data: bytes) -> int:
    return int.from_bytes(data, "big")


def i2b(value: int) -> bytes:
    length = (value.bit_length() + 7) // 8 or 1
    return value.to_bytes(length, "big")


def write_out(dir_name: str, files: dict[str, str], answer: str) -> None:
    """Write one challenge's materials and answer.

    Materials land in ``bench/challenges/<name>/materials`` (what a run sees),
    the answer in ``bench/sealed/<name>/answer.txt`` (never leaves this tree).
    """
    root = Path(__file__).resolve().parent.parent
    materials = root / "challenges" / dir_name / "materials"
    materials.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (materials / name).write_text(body, encoding="utf-8", newline="\n")
    sealed = root / "sealed" / dir_name
    sealed.mkdir(parents=True, exist_ok=True)
    (sealed / "answer.txt").write_text(answer.strip() + "\n", encoding="utf-8", newline="\n")
    print(f"{dir_name}: materials={sorted(files)} answer={answer.strip()[:24]}…")


def json_file(obj: object) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
