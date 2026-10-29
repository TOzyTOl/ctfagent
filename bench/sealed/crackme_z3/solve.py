"""Solver for crackme_z3 — re-encode the constraints and hand them to z3."""

from __future__ import annotations

import json
from pathlib import Path

import z3

sealed = Path(__file__).resolve().parent
constraints = json.loads((sealed / "constraints.json").read_text(encoding="utf-8"))
length = constraints[0]["length"]

w = [z3.BitVec(f"w{i}", 16) for i in range(length)]
solver = z3.Solver()
solver.add([z3.And(0x21 <= c, c <= 0x7E) for c in w])
for c in constraints:
    kind, spec = c["kind"], c["spec"]
    if kind == "lin":
        solver.add(z3.Sum([spec["coeff"].get(str(i), 0) * w[i] for i in spec["idx"]]) == spec["rhs"])
    elif kind == "xordiff":
        solver.add((w[spec["i"]] ^ spec["k"]) - w[spec["j"]] == spec["rhs"])
    else:
        solver.add((w[spec["i"]] * w[spec["j"]]) % spec["mod"] == spec["rhs"])

assert solver.check() == z3.sat
model = solver.model()
password = "".join(chr(model.eval(c).as_long()) for c in w)
print(password)
