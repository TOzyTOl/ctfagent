"""lcg_step — a linear congruential generator run backwards one step.

Difficulty: easy (the anchor item: a healthy platform solves this every time;
a miss here is a regression alarm, not a hard-challenge miss).

s_{i+1} = (a * s_i + c) mod m with every parameter published and six
consecutive outputs given. The seed s_0 never appeared in the output stream;
recovering it is one modular inverse.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import write_out  # noqa: E402

M = 2**31 - 1  # 2147483647, prime
A = 1103515245
C = 12345
S0 = 987654321


def main() -> None:
    outputs = []
    s = S0
    for _ in range(6):
        s = (A * s + C) % M
        outputs.append(s)
    write_out(
        "lcg_step",
        {
            "keystream.txt": "\n".join(str(x) for x in outputs) + "\n",
        },
        answer=str(S0),
    )


if __name__ == "__main__":
    main()
