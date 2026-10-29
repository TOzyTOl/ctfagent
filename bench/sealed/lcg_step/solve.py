"""Solver for lcg_step — verify from materials only."""

from __future__ import annotations

from pathlib import Path

M = 2**31 - 1
A = 1103515245
C = 12345

materials = Path(__file__).resolve().parents[2] / "challenges" / "lcg_step" / "materials"
outputs = [int(x) for x in (materials / "keystream.txt").read_text().split()]

s0 = ((outputs[0] - C) * pow(A, -1, M)) % M
state = s0
for expected in outputs:  # the whole chain must reproduce
    state = (A * state + C) % M
    assert state == expected, (state, expected)
print(s0)
