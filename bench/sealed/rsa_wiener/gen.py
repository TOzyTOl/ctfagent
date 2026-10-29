"""rsa_wiener — a large public exponent betraying a small private one.

Difficulty: medium-hard. Wiener's attack: d < N^(1/4)/3 makes e/N's continued
fraction convergents leak k/d. Pure-python friendly (continued fractions +
one quadratic), no external engine needed.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import b2i, json_file, prime, rng, write_out  # noqa: E402

FLAG = b"flag{w13n3r_l0v3s_sm4ll_d}"


def main() -> None:
    p = prime(512, "rsa_wiener-p")
    q = prime(512, "rsa_wiener-q")
    n = p * q
    phi = (p - 1) * (q - 1)
    r = rng("rsa_wiener-d")
    while True:
        d = int.from_bytes(bytes(r.getrandbits(8) for _ in range(16)), "big") | 1
        if d.bit_length() != 128:
            continue
        if math.gcd(d, phi) == 1:
            break
    e = pow(d, -1, phi)
    assert d < n ** 0.25 / 3, "Wiener's bound must hold"
    c = pow(b2i(FLAG), e, n)
    write_out(
        "rsa_wiener",
        {
            "pubkey.txt": (
                "运营平台泄露的公钥与一条加密广播：\n\n"
                f"N = {n}\ne = {e}\nc = {c}\n"
            ),
        },
        answer=FLAG.decode(),
    )


if __name__ == "__main__":
    main()
