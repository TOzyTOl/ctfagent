"""crackme_z3 — a 16-char password under mixed linear / xor / modular constraints.

Difficulty: medium (reverse-flavoured). The checker is plain readable Python;
the pain is that the constraint mix (modular products, xor offsets) makes
hand-solving error-prone while a solver pass is trivial. The answer is the
unique satisfying password — uniqueness is *proven here with z3* before the
challenge is written, because an item with two valid answers would poison
every baseline recorded against it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import z3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _common import rng, write_out  # noqa: E402

TARGET = "Z3s0lv3sTh3K3y!!"
LENGTH = len(TARGET)
assert LENGTH == 16


def _z3_expr(kind: str, spec: dict, w: list[z3.BitVecRef]) -> z3.BitVecRef:
    # 16-bit vectors: every expression stays far below the wraparound range
    # (chars <= 0x7E, at most four terms), so bit-vector semantics here equal
    # the plain-int semantics of the emitted checker.
    if kind == "lin":
        total = z3.Sum([spec["coeff"].get(str(i), 0) * w[i] for i in spec["idx"]])
        return total == spec["rhs"]
    if kind == "xordiff":
        return (w[spec["i"]] ^ spec["k"]) - w[spec["j"]] == spec["rhs"]
    if kind == "mulmod":
        return (w[spec["i"]] * w[spec["j"]]) % spec["mod"] == spec["rhs"]
    raise ValueError(kind)


def _solution_count(constraints: list[dict], limit: int) -> int:
    w = [z3.BitVec(f"w{i}", 16) for i in range(LENGTH)]
    solver = z3.Solver()
    solver.add([z3.And(0x21 <= c, c <= 0x7E) for c in w])  # printable, no space
    solver.add([_z3_expr(c["kind"], c["spec"], w) for c in constraints])
    found = 0
    while solver.check() == z3.sat and found < limit:
        model = solver.model()
        values = [model.eval(c).as_long() for c in w]
        found += 1
        solver.add(z3.Or([c != v for c, v in zip(w, values)]))
    return found


def _lin_rhs(spec: dict, codes: list[int]) -> int:
    return sum(spec["coeff"].get(str(i), 0) * codes[i] for i in spec["idx"])


def _term(coeff: int, i: int) -> str:
    if coeff == 1:
        return f"+ w[{i}]"
    if coeff == -1:
        return f"- w[{i}]"
    sign = "+" if coeff > 0 else "-"
    return f"{sign} {abs(coeff)}*w[{i}]"


def main() -> None:
    codes = [ord(ch) for ch in TARGET]
    r = rng("crackme_z3")
    constraints: list[dict] = []

    def add(kind: str, spec: dict) -> None:
        if kind == "lin":
            spec["rhs"] = _lin_rhs(spec, codes)
        elif kind == "xordiff":
            spec["rhs"] = (codes[spec["i"]] ^ spec["k"]) - codes[spec["j"]]
        elif kind == "mulmod":
            spec["rhs"] = (codes[spec["i"]] * codes[spec["j"]]) % spec["mod"]
        constraints.append({"kind": kind, "spec": spec})

    # a mix that reads like a hand-written checker: linear ties across distant
    # indices, xor-offset differences, one modular product
    add("lin", {"idx": [0, 3, 7], "coeff": {"0": 1, "3": 1, "7": -1}})
    add("lin", {"idx": [1, 15, 8], "coeff": {"1": 2, "15": 1, "8": -1}})
    add("xordiff", {"i": 4, "j": 11, "k": 0x2A})
    add("mulmod", {"i": 5, "j": 12, "mod": 97})
    add("lin", {"idx": [2, 9, 13, 14], "coeff": {"2": 1, "9": 1, "13": 1, "14": -1}})
    add("xordiff", {"i": 6, "j": 10, "k": 0x5C})
    while _solution_count(constraints, limit=2) != 1:
        # tighten with fresh index triples until exactly one password fits
        idx = r.sample(range(LENGTH), 3)
        add("lin", {"idx": idx, "coeff": {str(idx[0]): 1, str(idx[1]): -1, str(idx[2]): 1}})

    lines = [
        "import sys",
        "",
        "",
        "def check(s):",
        "    if len(s) != 16:",
        "        return False",
        "    w = [ord(ch) for ch in s]",
    ]
    for c in constraints:
        kind, spec = c["kind"], c["spec"]
        if kind == "lin":
            terms = " ".join(_term(spec["coeff"].get(str(i), 0), i) for i in spec["idx"])
            if terms.startswith("+ "):
                terms = terms[2:]
            lines.append(f"    if {terms} != {spec['rhs']}: return False")
        elif kind == "xordiff":
            lines.append(
                f"    if (w[{spec['i']}] ^ {spec['k']:#04x}) - w[{spec['j']}] != {spec['rhs']}: return False"
            )
        else:
            lines.append(
                f"    if (w[{spec['i']}] * w[{spec['j']}]) % {spec['mod']} != {spec['rhs']}: return False"
            )
    lines += [
        "    return True",
        "",
        "",
        'if __name__ == "__main__":',
        '    s = input("口令> ").strip()',
        '    print("正确。口令就是本题答案。" if check(s) else "不对。")',
        "",
    ]
    write_out(
        "crackme_z3",
        {"crackme.py": "\n".join(lines)},
        answer=TARGET,
    )
    # the constraint machine-readable form, for the sealed solver and the test
    sealed = Path(__file__).resolve().parent
    (sealed / "constraints.json").write_text(
        json.dumps([{"kind": c["kind"], "spec": c["spec"], "length": LENGTH} for c in constraints], indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


if __name__ == "__main__":
    main()
