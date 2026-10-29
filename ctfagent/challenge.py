"""Challenge loading: a directory with ``challenge.yaml`` (or ``.json``/``.yml``).

The format is deliberately tiny. Anything the operator knows goes in; anything
missing is inferred from the files (name from the directory, category from the
attachments' file types when triage runs).

    name: silentrotor
    category: crypto          # optional; "unknown" lets triage decide
    description: |
      Multi-stage rotor cipher.
    remote: "nc host 31337"   # optional
    flag_format: "flag\\{...\\}" # optional, narrows candidate extraction
    hints: ["..."]            # optional
    time_limit_s: 3600
    files: [attachments/]     # optional; defaults to everything in the source dir
    source: "D:/path/to/files" # optional; where the attachments live, if not here
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import Challenge

META_NAMES = ("challenge.yaml", "challenge.yml", "challenge.json")

#: A URL with a scheme, host and explicit port. The host group stops at any
#: delimiter that could start userinfo, a port or a path, so it yields a bare
#: hostname.
_SCHEME_HOST_PORT = re.compile(
    r"(?:https?|ftp)://([^/\s?#:@]+):(\d{2,5})", re.IGNORECASE
)
#: A URL with a scheme and no explicit port. Used only for ``remote``, where
#: the operator has declared that this host is the target.
_SCHEME_HOST = re.compile(r"(?:https?|ftp)://([^/\s?#:@]+)", re.IGNORECASE)
#: ``host:port`` written without a scheme — the usual CTF target notation.
_HOST_PORT = re.compile(
    r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}|(?:\d{1,3}\.){3}\d{1,3}):\d{2,5}(?!\d)"
)
#: ``nc host 31337`` / ``nc -v host 31337`` / ``ncat --ssl host 443``.
_NC_HOST = re.compile(
    r"\bnc(?:at)?\s+(?:--?\w+\s+)*"
    r"([A-Za-z0-9][A-Za-z0-9.-]*\.?|(?:\d{1,3}\.){3}\d{1,3})\s+\d{2,5}\b",
    re.IGNORECASE,
)
#: Ports the operator's proxy forwards correctly. A target on one of them does
#: not need a bypass, and treating a referenced article as one would send its
#: fetches direct — which fails on a censored host.
_DEFAULT_PORTS = {"80", "443"}


def _add(found: list[str], raw: str) -> None:
    host = raw.strip().strip(".").lower()
    if host and host not in found:
        found.append(host)


def target_hosts(challenge: Challenge) -> list[str]:
    """Hosts the workers must reach directly instead of via the operator's proxy.

    ``remote`` is only populated when the operator filled that field in; pasted
    challenges routinely carry the URL in the description or the title instead
    (observed: every URL-pasted run in this deployment had ``remote == ""``).
    So both are scanned.

    The two sources are not treated equally. ``remote`` is an explicit
    declaration of the target, so any host in it counts. In the prose, only a
    host with an explicit non-default port counts: that is exactly the shape the
    proxy breaks, and it keeps an incidental link to a reference article out of
    ``NO_PROXY``, where bypassing the proxy could make it unreachable instead.
    """
    found: list[str] = []
    for pattern in (_SCHEME_HOST, _SCHEME_HOST_PORT, _HOST_PORT, _NC_HOST):
        for match in pattern.finditer(challenge.remote):
            _add(found, match.group(1))
    prose = "\n".join([challenge.name, challenge.description])
    for match in _SCHEME_HOST_PORT.finditer(prose):
        if match.group(2) not in _DEFAULT_PORTS:
            _add(found, match.group(1))
    for pattern in (_HOST_PORT, _NC_HOST):
        for match in pattern.finditer(prose):
            _add(found, match.group(1))
    return found


def no_proxy_value(challenge: Challenge) -> str:
    """The ``NO_PROXY`` value for a run, or ``""`` when the run has no target."""
    hosts = target_hosts(challenge)
    if not hosts:
        return ""
    # The loopback entries are for a local replica the worker stands up itself;
    # they are always safe to bypass.
    return ",".join(hosts + ["localhost", "127.0.0.1", "::1"])


def _unescape(text: str) -> str:
    """Resolve the escapes a YAML double-quoted scalar allows.

    Only the ones a challenge file plausibly contains. An unrecognised escape is
    kept as written rather than raising: the value may be a regex, and a
    manifest that already shipped with ``\\{`` should keep working.
    """
    out: list[str] = []
    i = 0
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
            else:
                out.append(ch + nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_yaml(text: str) -> dict[str, Any]:
    """Parse the small YAML subset challenge files use.

    PyYAML is not a dependency: a challenge file is flat keys plus a few lists
    and block strings, and an extra dependency in the loader is a supply-chain
    surface for no benefit. Anything beyond this subset is a parse error with a
    clear message rather than a silent misread.
    """
    data: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line.startswith((" ", "\t")):
            raise ValueError(f"第 {i + 1} 行缩进不对：{line!r}")
        if ":" not in line:
            raise ValueError(f"第 {i + 1} 行应该是「键: 值」：{line!r}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest in ("|", ">"):
            i += 1
            block: list[str] = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            data[key] = "\n".join(block).strip()
            continue
        if not rest:
            # A bare key introduces either a block list or a nested block.
            i += 1
            items: list[str] = []
            nested: list[str] = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                stripped = lines[i].strip()
                if stripped.startswith("- "):
                    items.append(stripped[2:].strip().strip("\"'"))
                elif stripped:
                    nested.append(stripped)
                i += 1
            if items:
                data[key] = items
            elif nested:
                data[key] = "\n".join(nested)
            else:
                data[key] = ""
            continue
        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1].strip()
            data[key] = [item.strip().strip("\"'") for item in inner.split(",") if item.strip()] if inner else []
        elif rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
            # YAML double-quoted scalars process backslash escapes; single-quoted
            # ones do not. Without this, a correctly written
            # ``flag_format: "flag\\{[^}]+\\}"`` becomes a regex with literal
            # backslashes that matches no flag at all — silently, since the
            # pattern is still valid.
            data[key] = _unescape(rest[1:-1])
        elif rest.startswith("'") and rest.endswith("'") and len(rest) >= 2:
            data[key] = rest[1:-1].replace("''", "'")
        elif rest.isdigit():
            data[key] = int(rest)
        elif rest in ("true", "false"):
            data[key] = rest == "true"
        else:
            data[key] = rest
        i += 1
    return data


def load_meta(directory: Path) -> dict[str, Any]:
    for name in META_NAMES:
        candidate = directory / name
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8")
        if name.endswith(".json"):
            return json.loads(text)
        return _parse_yaml(text)
    return {}


def discover_files(directory: Path) -> list[str]:
    """Relative paths of everything except the metadata file itself."""
    out: list[str] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if path.parent == directory and path.name in META_NAMES:
            continue
        if any(part.startswith(".") for part in path.relative_to(directory).parts):
            continue
        out.append(path.relative_to(directory).as_posix())
    return out


def load_challenge_dir(directory: str | Path) -> Challenge:
    """Alias used by the CLI and the web app."""
    return load_challenge(directory)


def load_challenge(directory: str | Path) -> Challenge:
    directory = Path(directory).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"找不到题目目录：{directory}")
    meta = load_meta(directory)
    name = str(meta.get("name") or directory.name)
    # `source:` points at the directory holding the attachments when the
    # manifest lives elsewhere (a shared challenges/ folder pointing at a
    # competition download, say). Relative paths resolve against the manifest.
    source = directory
    if raw_source := str(meta.get("source") or "").strip():
        candidate = Path(raw_source).expanduser()
        source = candidate if candidate.is_absolute() else (directory / candidate)
        source = source.resolve()
    files = meta.get("files") or discover_files(source)
    if isinstance(files, str):
        files = [files]
    return Challenge(
        id=0,
        name=name,
        category=str(meta.get("category") or "unknown"),
        description=str(meta.get("description") or ""),
        files=[str(f) for f in files],
        remote=str(meta.get("remote") or ""),
        flag_format=str(meta.get("flag_format") or ""),
        hints=list(meta.get("hints") or []),
        time_limit_s=int(meta.get("time_limit_s") or 3600),
        source_dir=str(source),
    )


def challenge_block(challenge: Challenge, work_dir: Path | None = None) -> str:
    """The prompt block every agent sees. It is the same everywhere on purpose."""
    parts = [f"## {challenge.name}", ""]
    if challenge.category and challenge.category != "unknown":
        parts.append(f"Category (declared): {challenge.category}")
    if challenge.description:
        parts += ["", challenge.description]
    if challenge.remote:
        parts += ["", f"Remote: {challenge.remote}"]
    if challenge.flag_format:
        parts += ["", f"Flag format: {challenge.flag_format}"]
    if challenge.hints:
        parts += ["", "Hints:"] + [f"- {h}" for h in challenge.hints]
    if challenge.files:
        where = f" (relative to {work_dir})" if work_dir else ""
        parts += ["", f"Attachments{where}:"] + [f"- {f}" for f in challenge.files]
    parts += ["", f"Time budget: {challenge.time_limit_s}s"]
    # The platform clock is what ends a run; this line is only the competition's
    # own limit, and the two can differ. Saying so stops a worker from pacing
    # itself to a number the orchestrator does not enforce.
    parts.append(
        "This is the competition's stated limit. The orchestrator enforces its own wall clock; "
        "do not treat this figure as a deadline you can read the remaining time from."
    )
    return "\n".join(parts)
