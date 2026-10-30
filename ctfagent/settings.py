"""Model credentials: where they come from, and how to change them.

Three sources can supply a token — the environment, ``config.toml``, and
``~/.claude/settings.json`` — and before this module the only way to find out
which one had won was to run ``ctfagent doctor`` in a terminal and then hand-edit
a file. The settings page needs the same knowledge in the browser: the resolved
state, the secrets masked, and a write path that does not eat the comments in
``config.toml``.

On top of that sits the provider list (``providers`` table): the endpoints the
operator has tried, kept as rows so switching is one click instead of an edit;
see :func:`apply_provider` for why a row and not the file.

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
    "toml": "config.toml / 凭据页",
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


def _set_section_keys(lines: list[str], section: str, wanted: dict[str, str]) -> list[str]:
    """Set ``key = value`` pairs under one ``[section]``, in place.

    Shared by the credentials writer and the model-assignment writer so both
    keep the surrounding comments and the order of the file.

    A new key is inserted directly after the section's last ``key = value``
    line, not at the end of the section. The end of a section is usually a blank
    line followed by the prose that explains it, so appending there produced a
    file like ``worker = …`` / blank / ``verifier = …`` — valid TOML, but the
    assignment no longer reads as one block.
    """
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        match = _SECTION.match(line)
        if match is None:
            continue
        if start is None and match.group(1).strip() == section:
            start = i
            continue
        if start is not None:
            end = i
            break

    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"[{section}]")
        start = len(lines) - 1
        end = len(lines)

    def last_key_line(stop: int) -> int:
        """Index just past the section's final ``key = value`` line."""
        for i in range(stop - 1, start, -1):
            if re.match(r"^\s*[A-Za-z0-9_.\"'-]+\s*=", lines[i]):
                return i + 1
        return start + 1

    written: list[str] = []
    for key, value in wanted.items():
        pattern = re.compile(rf"^\s*(?:{re.escape(key)}\b\s*=|{_toml_str(key)}\s*=)")
        found = False
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if _SECTION.match(lines[i]):
                end = i
                break
            if pattern.match(lines[i]):
                lines[i] = f"{key} = {_toml_str(value)}"
                found = True
                break
        if not found:
            lines.insert(last_key_line(end), f"{key} = {_toml_str(value)}")
        written.append(key)
    return written


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
    written = _set_section_keys(lines, "models", wanted)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return written


def write_models(
    path: Path,
    *,
    roles: dict[str, str],
    directions: dict[str, str] | None = None,
) -> list[str]:
    """Set the per-role model assignment, and rewrite ``[models.roles]``.

    ``roles`` is the four fixed roles; ``directions`` is the per-direction
    override table, where an empty value means "follow ``worker``" and is
    therefore *removed* rather than written as ``""``. Writing the whole table
    every time is deliberate: the page shows the complete assignment, so a key
    left behind from a previous edit would reappear as a model the operator
    never chose.
    """
    wanted = {role: str(value).strip() for role, value in roles.items() if str(value).strip()}
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written = _set_section_keys(lines, "models", wanted) if wanted else []

    table = {
        direction: str(value).strip()
        for direction, value in (directions or {}).items()
        if str(value).strip()
    }
    # Drop the old table wholesale, then append the new one if it has content.
    kept: list[str] = []
    inside = False
    for line in lines:
        match = _SECTION.match(line)
        if match is not None:
            inside = match.group(1).strip() == "models.roles"
            if inside:
                continue
        if not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if table:
        kept.append("")
        kept.append("[models.roles]")
        for direction, value in table.items():
            kept.append(f"{direction} = {_toml_str(value)}")
        written.append("models.roles")

    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return written


def assigned_models(cfg: Any) -> list[str]:
    """Every model name the current assignment mentions, in role order."""
    models = cfg.models
    out: list[str] = []
    for name in (
        models.main, models.worker, models.verifier, models.writer,
        *models.roles.values(),
    ):
        value = str(name or "").strip()
        if value and value not in out:
            out.append(value)
    return out


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
        # True only when a ``CTFAGENT_*`` variable wins (it outranks the file) —
        # an ambient ``ANTHROPIC_*`` no longer overrides what the page saved, so
        # warning about it here would be a false alarm.
        "env_wins": models.credential_source == "env",
        "main": models.main,
        "worker": models.worker,
        "verifier": models.verifier,
        "writer": models.writer,
        "roles": dict(models.roles),
        "pricing_configured": bool(models.pricing),
        # Assigned models with no ``[models.pricing]`` entry. Their sessions book
        # the CLI's own number, which is an Anthropic price-table lookup by name
        # and therefore fiction for a gateway model — the watchdog then judges a
        # real-money ceiling against an invented one.
        "unpriced": [m for m in assigned_models(cfg) if m not in models.pricing],
    }


#: Roles the per-role dropdown always offers, in the order they are shown.
ROLE_ORDER = ("main", "worker", "verifier", "writer")

#: Directions that can be pinned to a model of their own. ``main`` is the lead,
#: not a dispatch direction, so it is not in this list.
DIRECTIONS = (
    "triage", "web", "crypto", "pwn", "reverse", "forensics",
    "malware", "misc", "osint", "ai_ml",
)

#: Roles and directions whose work is reasoning-bound, so switching endpoints
#: keeps them on the stronger tier when the new gateway names its models that
#: way. Same split the ``[models.roles]`` comment argues for.
STRONG_ROLES = ("main", "verifier", "reverse", "pwn", "crypto", "malware")

#: Tier words in a model name. They are not a quality judgement — only a way to
#: keep a role's intent when the endpoint changes, since gateways spell the same
#: tiers differently (``deepseek-v4-pro``, ``gpt-5``, ``claude-opus-5-5``).
_STRONG_MARKERS = ("pro", "opus", "large", "max", "ultra", "reason", "think")
_FAST_MARKERS = ("flash", "mini", "lite", "small", "fast", "haiku", "turbo", "air")

#: Name fragments that mark a generator rather than a chat model. A gateway's
#: ``/v1/models`` lists its whole catalogue — fastmodel.top returns 38 entries
#: including ``gpt-image-2.5`` and ``minimax_h3_image_audio_to_video`` — and a
#: role pointed at one of those fails on every turn. Only used to order the
#: candidates; if an endpoint serves nothing else, they are what it has.
_NON_CHAT = (
    "image", "video", "audio", "tts", "speech", "embed", "rerank", "imagine", "asr",
)


def _chat_first(models: list[str]) -> list[str]:
    """``models`` with the generators moved to the end, order otherwise kept."""
    chat = [m for m in models if not any(w in m.lower() for w in _NON_CHAT)]
    generators = [m for m in models if any(w in m.lower() for w in _NON_CHAT)]
    return chat + generators



def _local_time(value: str) -> str:
    """SQLite writes UTC; the page reads like a person's clock, not the database's."""
    from datetime import datetime, timezone

    try:
        stamp = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return str(value or "")
    return stamp.astimezone().strftime("%m-%d %H:%M")


def _probe_summary(provider: Any) -> str:
    """The probe's detail line, minus what the card already says.

    A successful probe ends with ``· N 个模型`` and the row above already prints
    that count, so repeating it made the card read as two facts that were one.
    """
    note = (provider.last_probe_note or "").strip()
    if not note:
        return ""
    if provider.last_probe == "ok" and "· " in note:
        return note.rsplit("· ", 1)[0].strip()
    return note


def provider_view(provider: Any, active_id: int) -> dict[str, Any]:
    """One provider card: what to show, with the secret reduced to a hint."""
    return {
        "id": provider.id,
        "name": provider.name or provider.base_url or f"#{provider.id}",
        "base_url": provider.base_url or "CLI 默认端点",
        "api_format": provider.api_format,
        "masked": mask(provider.secret),
        "has_secret": bool(provider.secret),
        "models": list(provider.models),
        "model_count": len(provider.models),
        "last_probe": provider.last_probe,
        "last_probe_note": _probe_summary(provider),
        "last_probed_at": _local_time(provider.last_probed_at),
        "imported_from": provider.imported_from,
        "active": provider.id == active_id,
    }


def role_view(cfg: Any) -> dict[str, Any]:
    """The model-assignment card: four roles, plus the direction overrides."""
    models = cfg.models
    return {
        "main": models.main,
        "worker": models.worker,
        "verifier": models.verifier,
        "writer": models.writer,
        "roles": dict(models.roles),
        "directions": [(d, models.roles.get(d, "")) for d in DIRECTIONS],
    }


def apply_models(cfg: Any, roles: dict[str, str], directions: dict[str, str]) -> None:
    """Put a saved assignment into the running config.

    Sessions read ``cfg.models.model_for(role)`` when they are created, so a
    saved assignment changes the next dispatch and leaves running workers alone —
    which is the behaviour an operator switching models mid-competition wants.
    """
    cfg.models.main = roles.get("main", cfg.models.main)
    cfg.models.worker = roles.get("worker", cfg.models.worker)
    cfg.models.verifier = roles.get("verifier", cfg.models.verifier)
    cfg.models.writer = roles.get("writer", cfg.models.writer)
    cfg.models.roles = dict(directions)


def pick_served_model(
    served: list[str], current: str, *, strong: bool, priced: Any = ()
) -> str:
    """The model ``served`` by this endpoint that best continues ``current``'s job.

    Switching endpoints used to leave the assignment untouched, so the new
    gateway received a model name it had never heard of and answered
    ``503 model_not_found`` — six worker tasks died that way in one evening,
    three minutes each. A name the endpoint serves is worth more than the name
    the operator typed for a different endpoint.

    The match keeps intent, not prestige: a reasoning role stays on whatever
    tier word the new gateway uses for "strong", a bulk role on "fast". When
    the names share nothing, the endpoint's first model is used, because the
    only thing that matters is that it exists there.

    ``priced`` is the set of models ``[models.pricing]`` has a rate for. A
    gateway often serves the same tier under two names (fastmodel.top lists
    both ``deepseek-flash`` and ``deepseek-v4-flash``); without this tie-break
    the first one wins and the run silently books CLI-estimated money.
    """
    if not served:
        return current
    if current in served:
        return current
    candidates = _chat_first(served)
    known = set(priced or ())

    def best(marker: str) -> str:
        matches = [m for m in candidates if marker in m.lower()]
        if not matches:
            return ""
        return next((m for m in matches if m in known), matches[0])

    markers = _STRONG_MARKERS if strong else _FAST_MARKERS
    lower = str(current or "").lower()
    # A name that already states its tier keeps that tier if the new endpoint
    # spells it the same way.
    for marker in markers:
        if marker in lower:
            same = best(marker)
            if same:
                return same
    for marker in markers:
        hit = best(marker)
        if hit:
            return hit
    return next((m for m in candidates if m in known), candidates[0])


def remap_to_served(
    served: list[str],
    roles: dict[str, str],
    directions: dict[str, str],
    priced: Any = (),
) -> tuple[dict[str, str], dict[str, str], list[tuple[str, str, str]]]:
    """Re-point an assignment at models the endpoint actually serves.

    Returns ``(roles, directions, changes)`` with ``changes`` as
    ``(label, old, new)``, so the caller can write the result and tell the
    operator what moved. An empty ``served`` means the probe returned no list:
    nothing is changed, because there is nothing to change *to*.
    """
    if not served:
        return dict(roles), dict(directions), []
    changes: list[tuple[str, str, str]] = []
    new_roles: dict[str, str] = {}
    for role, value in roles.items():
        picked = pick_served_model(
            served, value, strong=role in STRONG_ROLES, priced=priced
        )
        new_roles[role] = picked
        if picked != str(value):
            changes.append((role, str(value), picked))
    new_directions: dict[str, str] = {}
    for name, value in directions.items():
        picked = pick_served_model(
            served, value, strong=name in STRONG_ROLES, priced=priced
        )
        new_directions[name] = picked
        if picked != str(value):
            changes.append((name, str(value), picked))
    return new_roles, new_directions, changes


def apply_provider(cfg: Any, provider: Any | None, source: str = "toml") -> None:
    """Put a provider's endpoint and secret into the running config.

    The point of the provider list is that switching takes effect on the *next
    dispatch*, not on the next restart: sessions read ``cfg.models`` when they
    are created (see ``backend._credential_env``). ``source`` only labels where
    the winning token came from, so the page can keep warning about an
    environment variable that will take over again at the next start.
    """
    models = cfg.models
    if provider is None:
        models.base_url = ""
        models.auth_token = ""
        models.api_key = ""
        models.credential_source = ""
        return
    models.base_url = provider.base_url
    models.auth_token = provider.auth_token
    models.api_key = provider.api_key
    models.api_format = provider.api_format or "anthropic"
    models.credential_source = source


def provider_id_from_state(store: Any, cfg: Any) -> int:
    """Which provider row the page should mark as active.

    Three cases, in decreasing order of confidence. A ``toml`` credential that
    matches no row (a file written before this page existed) is adopted into one,
    so the operator does not open the page to an empty list while a working
    endpoint sits in ``config.toml``. An environment credential is *not*
    adopted: it would shadow whatever the page then says is active, which is the
    exact confusion this page is meant to end.
    """
    stored = store.get_setting("active_provider")
    if stored.isdigit():
        return int(stored)
    if cfg.models.credential_source == "env" or not cfg.models.base_url:
        return 0
    if not (cfg.models.auth_token or cfg.models.api_key):
        return 0
    from .models import Provider  # local import: models must not import settings

    new_id = store.add_provider(
        Provider(
            id=0,
            name=_host_label(cfg.models.base_url),
            base_url=cfg.models.base_url,
            auth_token=cfg.models.auth_token,
            api_key=cfg.models.api_key,
            api_format=cfg.models.api_format,
            imported_from="config.toml",
        )
    )
    store.set_setting("active_provider", str(new_id))
    return new_id


def _host_label(base_url: str) -> str:
    """A short name for a provider that was adopted rather than typed."""
    host = re.sub(r"^https?://", "", str(base_url or "")).rstrip("/")
    return host.split("/")[0] or "未命名"


def import_from_ccswitch() -> tuple[list[Any], str]:
    """Read the provider rows of a local CC Switch install, if there is one.

    CC Switch (``~/.cc-switch/cc-switch.db``) keeps one JSON ``settings_config``
    per provider, in exactly the ``~/.claude/settings.json`` shape. That is the
    same shape this module already knows how to read for its fallback, so the
    import is a translation rather than a new format.

    Read-only and best effort: a missing file, an old schema, or a locked
    database returns an empty list and a sentence saying why. Nothing is written
    here — the caller decides what to insert, and imported rows keep
    ``imported_from="ccswitch"`` so a re-import can update instead of duplicate.
    """
    import json
    import os
    import sqlite3

    db = Path(os.environ.get("CC_SWITCH_DB") or (Path.home() / ".cc-switch" / "cc-switch.db"))
    if not db.exists():
        return [], f"没有找到 {db}（没装 CC Switch，或换了位置）"
    try:
        # A URI with mode=ro: this must never write to another app's database,
        # and it may be open in its own process.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error as exc:
        return [], f"打不开 {db}：{exc}"
    try:
        rows = conn.execute(
            "SELECT id, name, settings_config FROM providers WHERE app_type='claude'"
        ).fetchall()
    except sqlite3.Error as exc:
        return [], f"表结构不认识（{exc}）——CC Switch 版本可能变了"
    finally:
        conn.close()

    from .models import Provider  # local import: models must not import settings

    found: list[Provider] = []
    for row_id, name, raw in rows:
        try:
            config = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        env = config.get("env") if isinstance(config, dict) else None
        if not isinstance(env, dict):
            continue
        base_url = str(env.get("ANTHROPIC_BASE_URL") or "")
        token = str(env.get("ANTHROPIC_AUTH_TOKEN") or "")
        key = str(env.get("ANTHROPIC_API_KEY") or "")
        if not base_url or not (token or key):
            continue
        found.append(
            Provider(
                id=0,
                name=str(name or "").strip() or _host_label(base_url),
                base_url=base_url,
                auth_token=token,
                api_key=key,
                api_format="anthropic",
                imported_from="ccswitch",
            )
        )
    if not found:
        return [], "CC Switch 里没有可导入的 Claude 供应商（需要同时有 base_url 和令牌）"
    return found, f"读取到 {len(found)} 个供应商"


def probe_models(base_url: str, token: str, timeout: float = 8.0) -> tuple[bool, str, list[str]]:
    """:func:`probe`, plus the model ids the endpoint advertises.

    The list is what the per-role dropdowns are built from, so the token that
    verifies a new gateway immediately replaces hand-typed model names with the
    ones that gateway actually serves. Parsed leniently: ``{"data": [{"id":…}]}``
    is the documented shape, a bare list of strings also appears in the wild, and
    anything else is reported as "reachable, no model list" rather than as a
    failure.
    """
    ok, detail = probe(base_url, token, timeout=timeout)
    if not ok:
        return ok, detail, []
    import json
    import urllib.request

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
            body = response.read(1 << 20).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the outer probe already succeeded
        return ok, detail, []
    try:
        parsed = json.loads(body)
    except ValueError:
        return ok, detail, []
    items = parsed.get("data") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return ok, detail, []
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name") or ""
        else:
            value = item
        if isinstance(value, str) and value and value not in ids:
            ids.append(value)
    note = detail if not ids else f"{detail.split(' · ')[0]} · {len(ids)} 个模型"
    return ok, note, ids


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
