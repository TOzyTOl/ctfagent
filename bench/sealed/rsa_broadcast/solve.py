"""Solver for rsa_broadcast — CRT then exact integer cube root."""

from __future__ import annotations

import json
from pathlib import Path

import gmpy2

materials = Path(__file__).resolve().parents[2] / "challenges" / "rsa_broadcast" / "materials"
data = json.loads((materials / "captures.json").read_text(encoding="utf-8"))
captures = data["captures"]

# CRT over the three moduli
N = 1
for cap in captures:
    N *= cap["n"]
x = 0
for cap in captures:
    other = N // cap["n"]
    x += cap["c"] * other * pow(other, -1, cap["n"])
x %= N

m, exact = gmpy2.iroot(x, data["e"])
assert exact, "cube root is not exact — padding present?"
print(int.to_bytes(int(m), (int(m).bit_length() + 7) // 8, "big").decode())
