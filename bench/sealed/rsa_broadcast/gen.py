"""rsa_broadcast — Håstad's broadcast attack, e=3, three moduli, no padding.

Difficulty: medium. The item tests whether the platform recognises the shape
(same plaintext encrypted under three small-e moduli without padding) and can
carry out CRT plus an exact integer cube root.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import b2i, json_file, prime, write_out  # noqa: E402

FLAG = b"flag{h4st4d_sh0rt_p4d}"
E = 3


def main() -> None:
    m = b2i(FLAG)
    assert pow(m, E).bit_length() < 512 * 3, "m^3 must stay under the CRT modulus"
    captures = []
    for index in range(E):
        n = prime(256, f"rsa_broadcast-p{index}") * prime(256, f"rsa_broadcast-q{index}")
        captures.append({"n": n, "c": pow(m, E, n)})
    write_out(
        "rsa_broadcast",
        {
            "captures.json": json_file(
                {"note": "同一条消息用 RSA 加密后从三个节点截获（无填充）", "e": E, "captures": captures}
            ),
        },
        answer=FLAG.decode(),
    )


if __name__ == "__main__":
    main()
