"""Model credentials: where they come from, and how to change them.

Three sources can supply a token — the environment, ``config.toml``, and
``~/.claude/settings.json`` — and before this module the only way to find out
which one had won was to run ``ctfagent doctor`` in a terminal and then hand-edit
a file. The settings page needs the same knowledge in the browser: the resolved
state, the secrets masked, and a write path that does not eat the comments in
``config.toml``.

Writing is deliberately line-based rather than a full TOML round-trip:
``tomllib`` can read but not write, and a round-trip through a third-party
serialiser would discard every comment in the file — which is where this project
keeps the reasons for its defaults.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")

_SOURCE_LABEL = {
    "env": "环境变量",
    "toml": "config.toml",
    "settings": "~/.claude/settings.json",
}


def mask(secret: str) -> str:
    """Enough of a secret to recognise it, not enough to use it.

    Short values get nothing back: showing the last two characters of an
    eight-character token is showing a quarter of it.
    """
    value = str(secret or "")
    if not value:
        return ""
    if len(value) < 12:
        return "…"
    return "…" + value[-4:]


def _toml_str(value: str) -> str:
    """A TOML basic string. Escaping matters: a token can contain ``\\`` or ``\"``."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_credentials(
    path: Path,
    *,
    base_url: str | None = None,
    auth_token: str | None = None,
    api_key: str | None = None,
) -> list[str]:
    """Set the given keys under ``[models]``, leaving everything else alone.

    ``None`` means "leave it as it is"; an empty string means "clear it". Returns
    the keys that were written.
    """
    wanted: dict[str, str] = {}
    if base_url is not None:
        wanted["base_url"] = base_url.strip()
    if auth_token is not None:
        wanted["auth_token"] = auth_token.strip()
    if api_key is not None:
        wanted["api_key"] = api_key.strip()
    if not wanted:
        return []

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        match = _SECTION.match(line)
        if match is None:
            continue
        if start is None and match.group(1).strip() == "models":
            start = i
            continue
        if start is not None:
            end = i
            break

    written: list[str] = []
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[models]")
        for key, value in wanted.items():
            lines.append(f"{key} = {_toml_str(value)}")
            written.append(key)
    else:
        for key, value in wanted.items():
            pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
            for i in range(start + 1, end):
                if pattern.match(lines[i]):
                    lines[i] = f"{key} = {_toml_str(value)}"
                    written.append(key)
                    break
            else:
                lines.insert(end, f"{key} = {_toml_str(value)}")
                end += 1
                written.append(key)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def state(cfg: Any) -> dict[str, Any]:
    """What the page shows: resolved values, secrets masked, and the source."""
    models = cfg.models
    return {
        "base_url": models.base_url,
        "api_format": models.api_format,
        "auth_token_masked": mask(models.auth_token),
        "api_key_masked": mask(models.api_key),
        "has_auth_token": bool(models.auth_token),
        "has_api_key": bool(models.api_key),
        "source": models.credential_source,
        "source_label": _SOURCE_LABEL.get(models.credential_source, "未知"),
        # The environment wins over config.toml on the next start, so a value
        # typed here would look saved and then be silently overridden.
        "env_wins": models.credential_source == "env",
        "main": models.main,
        "worker": models.worker,
        "verifier": models.verifier,
        "writer": models.writer,
        "roles": dict(models.roles),
        "pricing_configured": bool(models.pricing),
    }


def probe(base_url: str, token: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Ask the endpoint for its model list. Free, and it proves the token works.

    A model list is the cheapest authenticated call any Anthropic- or
    OpenAI-compatible gateway exposes, so this verifies address and token without
    spending anything. A gateway that does not implement it answers 404 — which
    still proves the host is reachable and the route exists, so that case is
    reported as "reachable, no model list" rather than as a failure.
    """
    if not base_url:
        return False, "没有配置 base_url（当前走 CLI 默认端点）"
    if not token:
        return False, "没有可用的 token"
    url = base_url.rstrip("/") + "/v1/models"
    request = urllib.request.Request(
        url,
        headers={
            "authorization": f"Bearer {token}",
            "x-api-key": token,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(300).decode("utf-8", "replace")
            return True, f"HTTP {response.status} · {body[:160]}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True, "端点可达，但未提供 /v1/models（不影响推理）"
        return False, f"HTTP {exc.code}：{exc.reason}"
    except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
        return False, f"{type(exc).__name__}: {exc}"
