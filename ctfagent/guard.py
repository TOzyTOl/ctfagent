"""Session-level guard rails, applied as PreToolUse hooks.

The orchestrator runs workers with ``bypassPermissions`` because a CTF box is
the one place where "the agent must not stop to ask" is the point. That makes
this hook the *only* hard boundary, so it must hold for the two things that
would be unforgivable rather than merely unhelpful:

1. commands that damage the host outside the workspace, and
2. writes outside the workspace and the session's own temp dir.

It is a denylist, not a sandbox: it stops accidents, not a determined model.
There is no other boundary today — ``sandbox.mode=docker`` is read by the
prompt and by ``doctor``, but nothing here wraps the shell tool in a container,
so a ``bypassPermissions`` session really does run on the host.

Both halves used to be advisory rather than enforced, and both were measured
failing on 2026-10-23:

- ``rm -rf /etc``, ``rm -rf /home/user/data`` and ``del /f /s /q <Desktop>``
  all passed. The old check read the token *after* the verb, which for
  ``rm -rf /etc`` is the switch ``-rf``; it then skipped it as an option and
  never looked at the target at all.
- ``echo x > C:\\Windows\\System32\\drivers\\etc\\hosts``, ``cp /etc/passwd
  C:\\Windows\\Temp\\p.txt`` and ``mv D:\\important D:\\trash`` all passed.
  Only the ``Write``/``Edit`` tools were ever handed to ``check_write``, so a
  worker with a shell had no write boundary whatsoever.

Paths are now taken from the command itself rather than from the first token
after a verb, and shell redirects are checked as writes. A relative path is
still left alone: it resolves against the worker's own cwd, which is inside the
workspace by construction, and refusing it would break ``rm -rf out/*``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

#: Patterns that are refused regardless of context.
DANGEROUS_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|~|/\*|[A-Za-z]:[\\/])\s*$"), "递归删除根目录或主目录"),
    (re.compile(r"\b(mkfs|fdisk|parted)\b"), "磁盘分区或格式化"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork 炸弹"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b"), "关机或重启系统"),
    (re.compile(r"\bformat\s+[a-zA-Z]:"), "格式化磁盘"),
    (re.compile(r"\bdiskpart\b"), "diskpart 磁盘操作"),
    (re.compile(r"\bdel\s+/[sfq].*\b[a-zA-Z]:\\(\*|\s*$)"), "递归删除整个磁盘"),
    (re.compile(r"\brmdir\s+/s\s+/q\s+[a-zA-Z]:\\\s*$"), "递归删除整个磁盘"),
    (re.compile(r"Remove-Item\s+.*-Recurse.*-Force.*[A-Za-z]:\\(?:\s|$)"), "递归删除整个磁盘"),
    (re.compile(r"\b(curl|wget|Invoke-WebRequest)\b[^|]*\|\s*(sh|bash|zsh|powershell|pwsh)\b"), "下载后直接执行"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\s+/\s*$"), "把 / 权限改成 777"),
]

#: Paths that must never be written. Checked as substrings after normalisation.
PROTECTED_SUBSTRINGS = (
    "\\.ssh\\", "/.ssh/", "\\.gnupg\\", "/.gnupg/", "\\windows\\system32", "/etc/passwd",
    "/etc/shadow", "\\boot\\", "/boot/", "\\program files",
)

#: Verbs whose path arguments have to stay inside the workspace. The
#: PowerShell cmdlets are listed because the shell tool on Windows is
#: ``PowerShell``, and ``Remove-Item``/``Set-Content`` are how it deletes and
#: writes — a list of only the POSIX names would leave this platform blind.
DESTRUCTIVE_VERBS = re.compile(
    r"\b(rm|del|erase|rmdir|rd|mv|move|cp|copy|tee|dd|shred|unlink|truncate|ln|link"
    r"|remove-item|ri|move-item|copy-item|set-content|add-content|out-file|clear-content"
    r"|new-item|rename-item)\b",
    re.IGNORECASE,
)

#: Shell redirection targets. ``2>&1`` is a descriptor, not a file, and is
#: filtered by the leading-character test in ``check_command``.
REDIRECT = re.compile(r"(?:^|[^<>\d])>>?\s*([^\s;|&<>]+)")

#: One shell argument, quotes respected.
_ARG = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s;|&<>]+')

#: Ends one statement's argument list. Used only to find the gap *between*
#: arguments — ``_ARG`` never emits these as tokens, so a ``for token in
#: _ARG.findall(...)`` loop cannot see the pipe that ends the statement.
_STATEMENT_BREAK = re.compile(r"[;|&\n]")

#: A switch rather than a path. Windows verbs take ``/f /s /q``; POSIX verbs
#: take ``-rf``. ``/etc`` is deliberately *not* a switch: three characters
#: after the slash is a path, one is a switch, and nothing in a CTF workspace
#: lives at ``/x``.
_FLAG = re.compile(r"^-{1,3}[\w-]+$|^/[A-Za-z?]$")

#: Discard devices. Writing here is not a write to the host, and
#: ``2>/dev/null`` is too common to refuse.
_SINKS = frozenset(
    {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "/nul", "nul"}
)


def _norm(path: str) -> str:
    return str(Path(path)).replace("/", "\\").lower() if "\\" in path or ":" in path else path.lower()


#: ``/mnt/<drive>/...`` is how WSL names the host's drives.
_WSL_DRIVE = re.compile(r"^/mnt/([a-zA-Z])(?:/|$)")


def _host_path(token: str) -> Path:
    """The host path a command token actually reaches.

    WSL spells the host's drives under ``/mnt``: ``/mnt/c/Users/x`` *is*
    ``C:\\Users\\x``. ``Path`` on Windows knows nothing about that, so it
    resolves the token against the current drive instead, and every
    containment test comes out wrong in both directions. Measured on the
    easyrs run (2026-10-27):

    - a worker redirecting into **its own workspace** through WSL was
      refused as "重定向目标不在工作区内" — ``/mnt/c/Users/.../work/disasm.txt``
      resolved to ``D:\\mnt\\c\\Users\\...``, which is under no allowed root;
    - the mirror case would have been worse: a sealed path reached through
      ``/mnt/d/...`` would have resolved nowhere near the sealed root and
      been allowed.

    Translate first, then resolve. A non-matching token resolves as before.
    """
    if sys.platform == "win32":
        match = _WSL_DRIVE.match(token)
        if match:
            rest = token[match.end():].replace("/", "\\")
            return Path(f"{match.group(1).upper()}:\\{rest}").resolve()
    return Path(token).resolve()


def _is_sink(token: str) -> bool:
    return token.strip("\"'`),;").replace("\\", "/").lower() in _SINKS


def _looks_absolute(token: str) -> bool:
    """A path the shell will not resolve against the worker's cwd.

    ``/etc`` is absolute here even though ``Path("/etc").is_absolute()`` is
    False on Windows — the check has to hold for the POSIX form a worker types
    on this host as much as for ``C:\\etc``.
    """
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|[\\/])", token))


def _refuse_shell_target(token: str, allowed_roots: list[Path], what: str) -> str | None:
    """Refuse an absolute shell target that lands outside every allowed root."""
    token = token.strip("\"'`),;")
    if not token or _is_sink(token) or _FLAG.match(token):
        return None
    if not _looks_absolute(token):
        # Relative: resolves against the worker's cwd, which is the workspace.
        return None
    norm = token.replace("/", "\\").lower()
    for protected in PROTECTED_SUBSTRINGS:
        if protected.lower() in norm:
            return f"已拒绝：{what}指向受保护路径：{token}"
    try:
        resolved = _host_path(token)
    except OSError:
        return None
    if any(_is_under(resolved, root) for root in allowed_roots):
        return None
    return f"已拒绝：{what}不在工作区内：{token}"


def check_command(command: str, allowed_roots: list[Path]) -> str | None:
    """Return a refusal reason, or None to allow."""
    for pattern, reason in DANGEROUS_COMMANDS:
        if pattern.search(command):
            return f"已拒绝：{reason}"

    # A redirect is a write. Guarding only the Write tool left `echo x >
    # C:\Windows\...` wide open.
    for match in REDIRECT.finditer(command):
        target = match.group(1)
        if target[:1] in ("&", "|"):
            continue
        reason = _refuse_shell_target(target, allowed_roots, "重定向目标")
        if reason:
            return reason

    # Every path argument of a destructive verb, not just the first token
    # after it — that token is usually a switch.
    #
    # A verb's arguments end where its statement ends. Observed in the second
    # easyrs run (2026-10-27): ``New-Item ... "$w\ghidra_proj" | Out-Null``
    # ended there, and a later line assigned ``$gh = "D:\tools\ghidra\...\analyzeHeadless.bat"``
    # — an *assignment*, not an argument. The sweep read straight past the pipe
    # and refused the whole command as "操作目标不在工作区内" for naming a tool
    # path the command never touched.
    #
    # ``_ARG`` does not emit separators as tokens, so the statement boundary has
    # to come from the gap *between* matches: a pipe, ``;``, ``&`` or a newline
    # there ends the argument list. Both shells treat a newline as a statement
    # separator, so this holds for ``bash`` and PowerShell alike.
    for match in DESTRUCTIVE_VERBS.finditer(command):
        rest = command[match.end():]
        end = 0
        for arg in _ARG.finditer(rest):
            if _STATEMENT_BREAK.search(rest[end:arg.start()]):
                break
            end = arg.end()
            reason = _refuse_shell_target(arg.group(), allowed_roots, "操作目标")
            if reason:
                return reason
    return None


def check_write(path: str, allowed_roots: list[Path]) -> str | None:
    if _is_sink(path):
        return None
    norm = _norm(path)
    for protected in PROTECTED_SUBSTRINGS:
        if protected.lower() in norm:
            return f"已拒绝：不能写入受保护路径：{path}"
    resolved = _host_path(path)
    if not any(_is_under(resolved, root) for root in allowed_roots):
        return f"已拒绝：不能写入工作区之外：{path}"
    return None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def check_read(path: str, deny_roots: list[Path]) -> str | None:
    """Refuse a read that would reach a deliberately sealed directory.

    The benchmark needs this: its manifest holds the expected answer, and a
    worker that greps the host for "flag" found it and submitted it. An agent
    cannot be expected not to look at a file it can see, so the boundary has to
    be here rather than in the prompt.
    """
    if not deny_roots or not path:
        return None
    try:
        resolved = _host_path(path)
    except OSError:
        return None
    for root in deny_roots:
        if _is_under(resolved, root):
            return f"已拒绝：该路径属于基准/评卷区域，运行期间不可读取：{path}"
    return None


def _mentions(command: str, deny_roots: list[Path]) -> str | None:
    """Sealed roots named literally in a shell command, or searched en bloc.

    Shell cannot be parsed reliably, so this is a substring test plus a scan of
    the absolute paths the command names. The substring half catches
    ``Select-String -Path ...\\bench.yaml``; the path scan catches the case that
    actually leaked an answer — ``grep -r flag D:\\agentsec\\CTFAGENT`` names
    the *parent* of the bench folder, which no substring test would find.

    Relative paths are ignored: they resolve against the worker's cwd (its
    workspace), not this process's, and pretending otherwise would deny an
    ordinary ``grep -r flag .``.
    """
    flat = command.replace("/", "\\").lower()
    for root in deny_roots:
        needle = str(root.resolve()).replace("/", "\\").lower().rstrip("\\")
        if needle and needle in flat:
            return f"已拒绝：命令引用了基准/评卷区域，运行期间不可访问：{root}"
    for match in _ABSOLUTE_PATH.finditer(command):
        token = match.group().strip("\"'`),;")
        if not token:
            continue
        # A lone ``/`` inside a quoted string is punctuation, not a path.
        # Observed in the second easyrs run (2026-10-27): the worker printed a
        # banner ``"=== PDB: easyrs / crate name ==="`` and the guard read that
        # slash as the drive root — an ancestor of every sealed root — and
        # refused the whole command. ``/`` as its own shell word (``grep -r flag
        # /``) is still a real search root and is still refused.
        if token == "/" and _inside_quotes(command, match.start()):
            continue
        # A bare ``/`` as its own shell word is the POSIX root, and searching it
        # reaches every drive through ``/mnt``. ``Path("/").resolve()`` cannot
        # answer this: on Windows it is the *current* drive, so the containment
        # test below only fired when the sealed root happened to sit on the same
        # drive as the process (both guard tests failed on a C: tmp_path with a
        # D: cwd, and the refusal silently became an allow).
        if token == "/" and deny_roots:
            return f"已拒绝：搜索范围包含基准/评卷区域，运行期间不可访问：{token}"
        reason = _exposes(token, deny_roots)
        if reason:
            return reason
    return None


def _inside_quotes(text: str, pos: int) -> bool:
    """Whether ``pos`` falls inside a single- or double-quoted run of ``text``."""
    quote = ""
    for index, char in enumerate(text[:pos]):
        if quote:
            if char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
    return bool(quote)


#: Absolute paths as they appear in a shell command: a Windows drive path or a
#: POSIX path starting at the root. Anything else is relative and stays inside
#: the workspace by construction.
#:
#: The lookahead is not decoration. Measured on the easyrs run (2026-10-27): the
#: worker grepped for ``"Fail"/"Success"``, the ``/`` *between two string
#: literals* was read as an absolute path, and ``Path("/").resolve()`` is the
#: current drive root — an ancestor of every sealed root, so a legitimate grep
#: of the worker's own workspace was refused as "搜索范围包含基准/评卷区域".
#: A slash glued to a quote is punctuation between words, not a path. A bare
#: ``/`` standing on its own (``grep -r flag /``) still matches and is still
#: refused.
_ABSOLUTE_PATH = re.compile(
    r"[A-Za-z]:[\\/][^\s\"'|&;<>()]*|(?<![\w.\-/])/(?=[^\s\"'|&;<>()]|\s|$)[^\s\"'|&;<>()]*"
)


def _exposes(base: str, deny_roots: list[Path]) -> str | None:
    """A search rooted at or above a sealed directory would return its contents.

    Observed: a worker grepped ``D:\\agentsec\\CTFAGENT`` for the challenge name
    and the result list pointed straight at ``bench/bench.yaml``. The tool never
    named the sealed path, so a path-equality check saw nothing. Searching a
    parent of a sealed root is refused outright.
    """
    if not base or not deny_roots:
        return None
    try:
        resolved = _host_path(base)
    except OSError:
        return None
    for root in deny_roots:
        if _is_under(resolved, root) or _is_under(root, resolved):
            return f"已拒绝：搜索范围包含基准/评卷区域，运行期间不可访问：{base}"
    return None


def build_hooks(allowed_roots: list[Path], deny_roots: list[Path] | None = None) -> dict[str, Any]:
    """PreToolUse hooks in the SDK's shape.

    The callback signature is ``(input, tool_use_id, context) -> HookJSONOutput``.
    Returning ``{}`` lets the call proceed; ``permissionDecision: deny`` blocks it
    and the reason is shown to the model, which is what makes a refusal useful.

    The value must be ``HookMatcher`` objects, not dicts: the SDK's
    ``_hooks_to_internal_format`` reads ``matcher.matcher`` / ``matcher.hooks``
    by attribute, so a dict silently degrades to "no hooks at all".
    """

    async def pre_tool_use(input: dict[str, Any], _tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
        tool = input.get("tool_name", "")
        args = input.get("tool_input") or {}
        reason: str | None = None
        sealed = deny_roots or []
        if tool in SHELL_TOOLS:
            command = str(args.get("command", ""))
            reason = check_command(command, allowed_roots) or _mentions(command, sealed)
        elif tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            target = args.get("file_path") or args.get("notebook_path") or ""
            if target:
                reason = check_write(str(target), allowed_roots)
                if reason is None:
                    for root in sealed:
                        if _is_under(_host_path(str(target)), root):
                            reason = f"已拒绝：不能写入基准/评卷区域：{target}"
                            break
        elif tool == "Read":
            reason = check_read(str(args.get("file_path") or ""), sealed)
        elif tool in ("Glob", "Grep"):
            # A search rooted at "/" is how the leaked answer was found: the
            # tool never names the sealed file, only a broad pattern.
            pattern = str(args.get("pattern") or "")
            base = str(args.get("path") or "")
            if base:
                reason = _exposes(base, sealed)
            if reason is None and pattern:
                reason = _mentions(pattern, sealed)
        if reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        return {}

    try:
        from claude_agent_sdk import HookMatcher  # noqa: PLC0415
    except ImportError:  # pragma: no cover - offline tests never reach the SDK
        return {"PreToolUse": [{"matcher": TOOL_MATCHER, "hooks": [pre_tool_use]}]}
    return {"PreToolUse": [HookMatcher(matcher=TOOL_MATCHER, hooks=[pre_tool_use])]}


#: Tools the guard inspects. Everything else (WebFetch, TodoWrite, …) needs no
#: boundary here. The shell tool is not always called "Bash": the CLI names it
#: after the host shell, so on Windows the same tool arrives as "PowerShell".
#: Matching only the POSIX name would leave the guard blind on the platform this
#: runs on. Read/Glob/Grep are inspected only when a sealed root is configured;
#: they still need a matcher or the SDK never calls the hook for them.
SHELL_TOOLS = ("Bash", "PowerShell")
TOOL_MATCHER = (
    "Bash|PowerShell|Read|Glob|Grep|Write|Edit|MultiEdit|NotebookEdit"
)
