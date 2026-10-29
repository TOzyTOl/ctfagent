"""Challenge loading and the prompt block."""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.challenge import (
    challenge_block,
    load_challenge,
    load_challenge_dir,
    no_proxy_value,
    target_hosts,
)
from ctfagent.models import Challenge


def test_loads_yaml_fixture(fixtures_dir: Path) -> None:
    challenge = load_challenge(fixtures_dir / "easy_crypto")
    assert challenge.name == "easy-crypto"
    assert challenge.category == "crypto"
    assert challenge.flag_format.startswith("flag")
    assert challenge.time_limit_s == 600
    assert "params.txt" in challenge.files


def test_block_contains_essentials(fixtures_dir: Path) -> None:
    challenge = load_challenge(fixtures_dir / "easy_rev")
    block = challenge_block(challenge)
    assert "easy-rev" in block
    assert "Flag format" in block
    assert "check.py" in block


def test_alias(fixtures_dir: Path) -> None:
    assert load_challenge_dir(fixtures_dir / "easy_crypto").name == "easy-crypto"


def test_directory_without_metadata_is_still_loadable(tmp_path: Path) -> None:
    """A bare directory of attachments is a valid challenge: name from the dir."""
    (tmp_path / "flag.txt").write_text("x", encoding="utf-8")
    challenge = load_challenge(tmp_path)
    assert challenge.name == tmp_path.name
    assert challenge.category == "unknown"
    assert challenge.files == ["flag.txt"]


def test_json_metadata(tmp_path: Path) -> None:
    (tmp_path / "challenge.json").write_text(
        '{"name": "j", "category": "misc", "description": "d"}', encoding="utf-8"
    )
    challenge = load_challenge(tmp_path)
    assert challenge.name == "j"
    assert challenge.category == "misc"


def test_yaml_list_and_block_string(tmp_path: Path) -> None:
    (tmp_path / "challenge.yaml").write_text(
        "name: x\n"
        "hints:\n"
        "  - first\n"
        "  - second\n"
        "description: |\n"
        "  line one\n"
        "  line two\n",
        encoding="utf-8",
    )
    challenge = load_challenge(tmp_path)
    assert challenge.hints == ["first", "second"]
    assert "line one" in challenge.description
    assert "line two" in challenge.description


def test_double_quoted_scalar_processes_escapes(tmp_path: Path) -> None:
    """A quoted flag_format must arrive as a regex that actually matches.

    Live symptom: every benchmark manifest wrote ``flag_format: "flag\\\\{...\\\\}"``
    and the loader returned the backslashes literally, so the extractor's pattern
    matched nothing. Nothing failed — the run just never noticed a flag in a
    report that contained one.
    """
    import re

    (tmp_path / "challenge.yaml").write_text(
        'name: x\nflag_format: "flag\\\\{[^}]+\\\\}"\n', encoding="utf-8"
    )
    pattern = load_challenge(tmp_path).flag_format
    assert re.search(pattern, "flag{abc}"), pattern


def test_single_quoted_scalar_keeps_backslashes(tmp_path: Path) -> None:
    """Single quotes are literal in YAML; only ``''`` is an escape."""
    import re

    (tmp_path / "challenge.yaml").write_text(
        "name: x\nflag_format: 'flag\\{[^}]+}'\n", encoding="utf-8"
    )
    pattern = load_challenge(tmp_path).flag_format
    assert re.search(pattern, "flag{abc}"), pattern


def _challenge(**kw) -> Challenge:
    fields = dict(id=1, name="x", category="web", description="")
    fields.update(kw)
    return Challenge(**fields)


def test_target_hosts_reads_the_remote_field() -> None:
    hosts = target_hosts(_challenge(remote="http://hbc2.haobachang.com:31960/"))
    assert hosts == ["hbc2.haobachang.com"]


def test_target_hosts_reads_a_url_pasted_into_the_description() -> None:
    """Pasted challenges leave ``remote`` empty and put the URL in the prose.

    Live symptom: every URL-pasted run in this deployment had ``remote == ""``,
    so the remote discipline and the proxy bypass never reached the workers that
    were talking to a live target.
    """
    hosts = target_hosts(
        _challenge(description="http://hbc2.haobachang.com:46931/ 这题 sql注入的 找到flag")
    )
    assert hosts == ["hbc2.haobachang.com"]


def test_target_hosts_reads_a_bare_host_colon_port() -> None:
    assert target_hosts(_challenge(description="try 10.0.0.5:8080/index.php")) == ["10.0.0.5"]


def test_target_hosts_reads_an_nc_invocation() -> None:
    assert target_hosts(_challenge(remote="nc -v pwn.ctf.cn 9999")) == ["pwn.ctf.cn"]


def test_no_proxy_is_empty_without_a_target() -> None:
    """A local-only challenge must not widen NO_PROXY at all."""
    assert no_proxy_value(_challenge(description="an RSA puzzle, no network")) == ""


def test_no_proxy_keeps_loopback_droppable() -> None:
    value = no_proxy_value(_challenge(remote="http://a.example.org:8000/"))
    assert value.split(",")[0] == "a.example.org"
    assert {"localhost", "127.0.0.1", "::1"} <= set(value.split(","))


def test_a_reference_link_in_the_prose_is_not_a_target() -> None:
    """Only a non-default port marks a prose host as the target.

    The proxy passes port 80/443 correctly, so a link to a documentation site
    needs no bypass — and putting it in ``NO_PROXY`` would send its fetches
    direct, which on a censored host makes it unreachable.
    """
    challenge = _challenge(description="background: https://en.wikipedia.org/wiki/RSA_(cryptosystem)")
    assert target_hosts(challenge) == []


def test_remote_declares_the_target_even_on_a_default_port() -> None:
    assert target_hosts(_challenge(remote="https://ctf.example.com")) == ["ctf.example.com"]
