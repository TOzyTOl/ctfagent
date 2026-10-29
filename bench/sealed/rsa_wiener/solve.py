"""Solver for rsa_wiener — continued fractions on e/N, then verify via phi."""

from __future__ import annotations

import math
from pathlib import Path

materials = Path(__file__).resolve().parents[2] / "challenges" / "rsa_wiener" / "materials"
values = {}
for line in (materials / "pubkey.txt").read_text(encoding="utf-8").splitlines():
    if "=" in line:
        key, _, value = line.partition("=")
        values[key.strip()] = int(value.strip())
n, e, c = values["N"], values["e"], values["C" if "C" in values else "c"]


def convergents(a: int, b: int):
    while b:
        q = a // b
        yield q
        a, b = b, a - q * b


def solve() -> int:
    # standard continued-fraction expansion of e/n
    num, den, pnum, pden = 1, 0, 0, 1  # convergent k/d built incrementally
    a, b = e, n
    while b:
        q = a // b
        num, pnum = q * num + pnum, num
        den, pden = q * den + pden, den
        a, b = b, a - q * b
        k, d = num, den
        if k == 0 or (e * d - 1) % k:
            continue
        phi = (e * d - 1) // k
        s = n - phi + 1  # p+q; p,q are roots of x^2 - s*x + n
        disc = s * s - 4 * n
        if disc < 0:
            continue
        root = math.isqrt(disc)
        if root * root != disc:
            continue
        if (s + root) % 2 or (s + root) // 2 * ((s - root) // 2) != n:
            continue
        return d
    raise SystemExit("wiener failed")


d = solve()
m = pow(c, d, n)
print(m.to_bytes((m.bit_length() + 7) // 8, "big").decode())
