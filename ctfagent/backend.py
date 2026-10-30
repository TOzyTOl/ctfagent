"""Agent backend: one Claude Code session per worker task, one long-lived session
for the main agent.

The backend is an interface, not an implementation detail, because the whole
orchestrator is testable only if it can run without spawning LLM subprocesses.
``ClaudeSDKBackend`` talks to the real Claude Agent SDK; ``ScriptedBackend``
replays canned turns for tests.

Three things here are load-bearing on Windows and were learned the hard way:

1. **The SDK refuses ``.cmd``/``.bat`` shims.** The ``claude`` on PATH is a
   PowerShell shim, so ``cli_path`` must resolve to the native ``claude.exe``.
2. **User settings must not be inherited.** ``setting_sources=["project"]``
   loads only ``<cwd>/.claude`` — which is where the run's vendored skills live —
   and keeps the operator's global model/autoMode settings out of the session.
3. **The proxy reports tokens, not dollars.** ``max_budget_usd`` is still passed
   as a ceiling, but the orchestrator enforces token/turn/time limits itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from .config import AppConfig, _claude_settings_env

#: CLI-reported failures that mean "no usable credentials", not "the model
#: refused". Their assistant message is a synthetic prompt telling the operator
#: to run ``/login`` inside a non-interactive session, which is a dead end.
_AUTH_ERRORS = frozenset({"authentication_failed", "oauth_org_not_allowed"})

#: Prose the CLI puts in the result payload when it refused the credential. The
#: SDK re-raises that payload as a ``ResultError``; without recognising it, the
#: raw "Please run /login" line reaches the console as an error string.
_AUTH_TEXT = ("Not logged in", "Please run /login", "Failed to authenticate")


def _looks_like_auth(text: str) -> bool:
    return any(marker in text for marker in _AUTH_TEXT)


#: A gateway answered, and the answer was about the gateway itself — not about
#: the challenge and not about the task's work. Six worker tasks in runs 83/84
#: died this way in three minutes each ("API Error: 503 No available channel for
#: model spark-x2.6"), were recorded as ordinary failures, and the lead
#: re-dispatched the same direction four more times: the summary read like a
#: task outcome, so there was nothing telling it otherwise.
_GATEWAY_ERROR = re.compile(
    r"API Error:\s*(?:40[13]|42[49]|5\d\d)"
    r"|No available channel"
    r"|model_not_found"
    r"|(?:invalid|unknown)\s+model"
    r"|overloaded_error"
    r"|rate_limit_error",
    re.IGNORECASE,
)


def gateway_error(text: str) -> str:
    """The gateway's own words when ``text`` is an infrastructure failure, else "".

    Kept narrow on purpose: a task that legitimately reports "the target
    returned 503" must not be read as the endpoint being down, so the status
    code has to follow the CLI's ``API Error:`` prefix rather than appear on its
    own.
    """
    match = _GATEWAY_ERROR.search(str(text or ""))
    if match is None:
        return ""
    # One sentence, so it fits in an error column and a warning line.
    line = " ".join(str(text).split())
    return line[:400]


@dataclass
class SessionSpec:
    """Everything one session needs. Immutable for the life of the session."""

    role: str
    prompt: str
    cwd: Path
    system_prompt: str
    model: str
    max_turns: int
    max_budget_usd: float
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] = field(default_factory=dict)
    resume_session_id: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    stderr_path: Path | None = None
    #: Skills the session may invoke. ``None`` leaves the CLI default (everything
    #: discovered); a list is an allowlist and auto-enables the Skill tool.
    skills: list[str] | None = None
    #: Emit ``text_delta`` events as the model writes. Only the main agent wants
    #: this: its text is the conversation, and a reply that appears whole after
    #: thirty seconds does not read like a chat. Worker transcripts are read
    #: after the fact, so deltas there are pure overhead.
    stream_text: bool = False


@dataclass
class AgentEvent:
    """A normalised session event.

    kind is one of: ``init``, ``text``, ``thinking``, ``tool_use``, ``tool_result``,
    ``result``, ``error``.
    """

    kind: str
    payload: dict[str, Any]
    cost_usd: float = 0.0
    turns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.payload}


class AgentBackend(Protocol):
    def run(self, spec: SessionSpec) -> AsyncIterator[AgentEvent]: ...

    def open_client(self, spec: SessionSpec) -> "AgentClient": ...


class AgentClient(Protocol):
    """A persistent, multi-turn session (used by the main agent)."""

    async def __aenter__(self) -> "AgentClient": ...

    async def __aexit__(self, *exc: Any) -> None: ...

    def send(self, text: str) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None:
        """Cut the turn that is currently streaming, if any.

        The operator can speak at any moment; a turn that is blocked in a tool
        call has to end before the message can be read. Implementations that
        cannot interrupt may return without doing anything — the message is
        then delivered at the turn boundary as before.
        """
        ...


def _sdk():
    """Import lazily so tests and offline CLI commands work without the SDK."""
    import claude_agent_sdk as sdk  # noqa: PLC0415

    return sdk


def resolve_cli_path(configured: str = "") -> str:
    """Find a native ``claude`` executable.

    Returns an empty string when nothing usable is found, which makes the SDK
    fall back to its own bundled CLI. ``.cmd``/``.bat``/``.ps1`` shims are
    rejected because the SDK refuses to spawn them on Windows.
    """
    if configured:
        return configured
    env = os.environ.get("CTFAGENT_CLI_PATH", "")
    if env:
        return env
    candidates: list[Path] = []
    found = shutil.which("claude")
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            base = Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
            candidates += [base / "claude.exe", base / "claude"]
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            candidates += [Path(local) / "Programs" / "claude" / "claude.exe"]
    for candidate in candidates:
        if candidate.suffix.lower() in (".cmd", ".bat", ".ps1"):
            continue
        if candidate.is_file():
            return str(candidate)
    return ""


def credentials_missing(cfg: AppConfig) -> bool:
    """True when no child session can authenticate at all.

    Sessions run with ``setting_sources=["project"]``, so the CLI does **not**
    read ``~/.claude/settings.json`` by itself — the operator's usual
    ``ANTHROPIC_AUTH_TOKEN`` there never reaches a worker on its own. ``cfg``
    already resolved that fallback (see :func:`ctfagent.config._apply_env`), so
    the credential on the config object is what the child will actually get;
    the environment checks below cover tokens the operator set some other way
    (``/login`` credentials live outside both).
    """
    return not (
        cfg.models.auth_token
        or cfg.models.api_key
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )


#: Operator-facing explanation per CLI auth error. The CLI's own text for these
#: is the interactive ``Not logged in · Please run /login`` prompt, which is a
#: dead end in a non-interactive session and was being mistaken for the lead's
#: reply.
_AUTH_ERROR_TEXT = {
    "authentication_failed": "模型鉴权失败：服务进程没有可用的凭据",
    "oauth_org_not_allowed": "模型鉴权失败：凭据所属组织不被该端点接受",
}


#: Windows NTSTATUS values that reach us as a decimal exit code when a child
#: process cannot start at all. The raw number is useless to an operator; what
#: matters is that the machine, not the code, refused to run it.
_SPAWN_FAILURES = {
    "3221225794": (  # 0xC0000142 STATUS_DLL_INIT_FAILED
        "子进程没能初始化（0xC0000142），通常是系统内存不够。"
        "这个 CLI 是单个约 285 MB 的可执行文件，启动时就要提交一大块内存；"
        "提交内存接近上限时，每一次新建会话都会失败。"
        "关掉占内存的程序（WSL 的 vmmemWSL、Burp 的 java 常各占十几 GB）后重试。"
    ),
    "3221225725": "子进程栈溢出（0xC00000FD），没有执行任何东西。",  # STATUS_STACK_OVERFLOW
    "3221225477": "子进程启动即崩溃（0xC0000005 访问冲突）。",  # STATUS_ACCESS_VIOLATION
}


def spawn_error_message(error: str) -> str:
    """A readable line for a child process that never started.

    Measured on this host: a run failed in the same second it was created with
    ``exit code 3221225794`` and zero cost, because the machine had 0.8 GB of
    31.8 GB free and 91.9% of its commit limit in use. Nothing about the run was
    wrong — but the operator sees a ten-digit number and has no way to know that.
    """
    text = str(error or "")
    for code, hint in _SPAWN_FAILURES.items():
        if code in text:
            return hint
    return ""


def auth_error_message(cfg: AppConfig, error: str = "") -> str:
    """One actionable line for the operator when the CLI reports an auth error.

    Names the two things that actually differ: where the credential came from
    (``credential_source``) and what to change. A bare "Not logged in" sends the
    operator looking for a lost session, when the fix is a token in the launcher
    or in ``config.toml``.
    """
    head = _AUTH_ERROR_TEXT.get(error, f"模型鉴权失败（{error or '未知原因'}）")
    if credentials_missing(cfg):
        return (
            f"{head}。启动服务前设置 ANTHROPIC_AUTH_TOKEN（或 ANTHROPIC_API_KEY），"
            "或在 config.toml 的 [models] 里写 auth_token。"
            "子会话以 setting_sources=[\"project\"] 运行，不会自己读 ~/.claude/settings.json。"
        )
    source = {
        "env": "环境变量",
        "toml": "config.toml",
        "settings": "~/.claude/settings.json",
    }.get(cfg.models.credential_source, cfg.models.credential_source or "未知来源")
    return (
        f"{head}（凭据来自{source}）。令牌可能已失效或与 base_url 不匹配："
        f"检查 ANTHROPIC_BASE_URL={cfg.models.base_url or '默认值'} 与对应令牌。"
    )


def _credential_env(cfg: AppConfig, extra: dict[str, str] | None = None) -> dict[str, str]:
    env: dict[str, str] = {
        # Keep the child from emitting telemetry through the operator's proxy.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        # A wait_tasks call may legitimately block for 45s; the CLI's default
        # MCP tool timeout would cancel it first.
        "MCP_TOOL_TIMEOUT": "180000",
        "BASH_DEFAULT_TIMEOUT_MS": "120000",
        "BASH_MAX_TIMEOUT_MS": "600000",
    }
    if cfg.models.base_url:
        env["ANTHROPIC_BASE_URL"] = cfg.models.base_url
    if cfg.models.auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = cfg.models.auth_token
    if cfg.models.api_key:
        env["ANTHROPIC_API_KEY"] = cfg.models.api_key
    # Model aliases the CLI maps through. The operator's own values win: a
    # gateway that serves one model (a proxy whose only channel is
    # ``deepseek-v4-flash``, say) is configured through these, and overwriting
    # them here is how an otherwise working session starts answering 503 for
    # the alias model.
    extra = extra or {}
    settings_env = _claude_settings_env()
    for alias in ("OPUS", "SONNET", "HAIKU"):
        name_key = f"ANTHROPIC_DEFAULT_{alias}_MODEL_NAME"
        plain_key = f"ANTHROPIC_DEFAULT_{alias}_MODEL"
        if any(k in extra for k in (name_key, plain_key, "ANTHROPIC_MODEL")):
            continue
        if any(
            os.environ.get(k) or settings_env.get(k)
            for k in (name_key, plain_key, "ANTHROPIC_MODEL")
        ):
            continue
        env[name_key] = cfg.models.worker
    env.update(extra)
    return env



class ClaudeSDKBackend:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.cli_path = resolve_cli_path(cfg.cli_path)

    # -- option assembly ---------------------------------------------------
    def _options(self, spec: SessionSpec):
        sdk = _sdk()
        stderr_target = spec.stderr_path

        def _stderr(line: str) -> None:
            if stderr_target is None:
                return
            try:
                stderr_target.parent.mkdir(parents=True, exist_ok=True)
                with stderr_target.open("a", encoding="utf-8", errors="replace") as handle:
                    handle.write(line.rstrip() + "\n")
            except OSError:
                pass

        kwargs: dict[str, Any] = dict(
            cwd=str(spec.cwd),
            model=spec.model or None,
            max_turns=spec.max_turns,
            max_budget_usd=spec.max_budget_usd,
            system_prompt={"type": "preset", "preset": "claude_code", "append": spec.system_prompt},
            permission_mode="bypassPermissions",
            # Only <cwd>/.claude loads: the run's skills, nothing from the operator.
            setting_sources=["project"],
            env=_credential_env(self.cfg, spec.extra_env),
            mcp_servers=spec.mcp_servers or {},
            strict_mcp_config=True,
            hooks=spec.hooks or {},
            allowed_tools=spec.allowed_tools or [],
            disallowed_tools=spec.disallowed_tools or [],
            include_partial_messages=spec.stream_text,
            stderr=_stderr,
        )
        if self.cli_path:
            kwargs["cli_path"] = self.cli_path
        if self.cfg.models.fallback:
            kwargs["fallback_model"] = self.cfg.models.fallback
        if spec.resume_session_id:
            kwargs["resume"] = spec.resume_session_id
        if spec.skills is not None:
            kwargs["skills"] = spec.skills
        # Depth knobs. `--effort` is documented; `--thinking` /
        # `--max-thinking-tokens` are not in `--help` but the installed build
        # accepts both (verified by running them). See ``ModelConfig.thinking``.
        effort = self.cfg.models.effort_for(spec.role)
        if effort:
            kwargs["effort"] = effort
        # `--thinking` and `--max-thinking-tokens` are accepted by the installed
        # build even though `--help` omits them, and thinking is the one input
        # side lever there is: the transcript carries it to every later turn.
        budget = self.cfg.models.thinking_for(spec.role)
        if budget is not None:
            kwargs["thinking"] = (
                {"type": "disabled"}
                if budget == 0
                else {"type": "enabled", "budget_tokens": budget}
            )
        return sdk.ClaudeAgentOptions(**kwargs)

    # -- normalisation -----------------------------------------------------
    @staticmethod
    def _normalise(message: Any) -> list[AgentEvent]:
        sdk = _sdk()
        out: list[AgentEvent] = []
        if isinstance(message, sdk.AssistantMessage):
            # An authentication failure arrives as a *synthetic* assistant
            # message whose text is the CLI's own "Not logged in · Please run
            # /login". Emitting it as ``text`` is how the chat pane came to show
            # the CLI's login prompt as though the lead had said it; the
            # operator then looked for a lost session instead of a missing
            # token. The same message still carries the real cause in ``error``,
            # so that is what the UI gets, under a kind of its own.
            auth_error = getattr(message, "error", None)
            if auth_error in _AUTH_ERRORS:
                out.append(AgentEvent("auth_error", {"error": auth_error}))
                return out
            for block in message.content:
                name = type(block).__name__
                if name == "TextBlock":
                    out.append(AgentEvent("text", {"text": block.text}))
                elif name == "ThinkingBlock":
                    out.append(AgentEvent("thinking", {"text": getattr(block, "thinking", "")}))
                elif name == "ToolUseBlock":
                    out.append(
                        AgentEvent(
                            "tool_use",
                            {"tool": block.name, "input": _short(block.input), "id": block.id},
                        )
                    )
        elif isinstance(message, sdk.UserMessage):
            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    if type(block).__name__ == "ToolResultBlock":
                        out.append(
                            AgentEvent(
                                "tool_result",
                                {
                                    "id": getattr(block, "tool_use_id", ""),
                                    "is_error": bool(getattr(block, "is_error", False)),
                                    "content": _short(getattr(block, "content", "")),
                                },
                            )
                        )
        elif isinstance(message, getattr(sdk, "StreamEvent", ())):
            # Partial deltas are only requested for the main agent. Anything
            # other than text is left out: a half-formed tool_use is not
            # something the UI can act on, and the complete block arrives in
            # the AssistantMessage right after. A delta from a nested context
            # (parent_tool_use_id set) belongs to that tool call, not the reply.
            raw = message.event or {}
            delta = raw.get("delta") or {}
            if raw.get("type") == "content_block_delta" and not message.parent_tool_use_id:
                if delta.get("type") == "text_delta" and delta.get("text"):
                    out.append(AgentEvent("text_delta", {"text": delta["text"]}))
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    out.append(AgentEvent("thinking_delta", {"text": delta["thinking"]}))
        elif isinstance(message, sdk.SystemMessage):
            subtype = getattr(message, "subtype", "")
            data = getattr(message, "data", None)
            data = data if isinstance(data, dict) else {}
            if subtype == "thinking_tokens":
                # Emitted on every thinking block. Nothing reads it and it is
                # 167 bytes a line: 519k of them grew this database to 126 MB,
                # 87 MB of which was this one subtype.
                return out
            payload: dict[str, Any] = {
                "subtype": subtype,
                "session_id": data.get("session_id", ""),
                "skills": data.get("skills", []),
                "slash_commands": data.get("slash_commands", []),
                "model": data.get("model", ""),
            }
            # Background-task lifecycle. The CLI moves a command that outlives
            # its 600 s ceiling to the background and then emits nothing while
            # it runs. Without these fields the orchestrator cannot tell that
            # silence from a hung model. Observed live: four workers were killed
            # at exactly 305 s of silence while their own background cracks were
            # still printing progress.
            for key in ("task_id", "task_type", "tool_use_id", "status", "is_backgrounded"):
                value = data.get(key)
                if value is not None and value != "":
                    payload[key] = value
            patch = data.get("patch")
            if isinstance(patch, dict):
                # ``task_updated`` reports the raw status (including "killed")
                # inside the patch, not at the top level. ``is_backgrounded``
                # is how a foreground task that was later moved to the
                # background announces itself.
                if patch.get("status"):
                    payload["status"] = patch["status"]
                if patch.get("is_backgrounded") is not None:
                    payload["is_backgrounded"] = bool(patch["is_backgrounded"])
            tasks = data.get("tasks")
            if isinstance(tasks, list):
                # ``background_tasks_changed`` is a level signal, not a delta:
                # it carries every live task after the change and replaces the
                # consumer's set. Only the fields the orchestrator uses are
                # kept — the raw entries carry a dozen more.
                payload["tasks"] = [
                    {
                        "task_id": str(t.get("task_id") or ""),
                        "task_type": str(t.get("task_type") or ""),
                        "description": _short(str(t.get("description") or ""), limit=200),
                        "awaited": bool(t.get("awaited")),
                    }
                    for t in tasks
                    if isinstance(t, dict) and t.get("task_id")
                ]
            out.append(AgentEvent("init", payload))
        elif isinstance(message, sdk.ResultMessage):
            cost = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
            turns = int(getattr(message, "num_turns", 0) or 0)
            out.append(
                AgentEvent(
                    "result",
                    {
                        "subtype": getattr(message, "subtype", ""),
                        "is_error": bool(getattr(message, "is_error", False)),
                        "result": _short(getattr(message, "result", "")),
                        "session_id": getattr(message, "session_id", ""),
                        "usage": _short(getattr(message, "usage", {}) or {}, limit=2000),
                        # Why the turn ended. ``aborted_streaming``/``aborted_tools``
                        # mean an interrupt, not a dead session — the orchestrator
                        # must not close a run over one.
                        "terminal_reason": getattr(message, "terminal_reason", None),
                    },
                    cost_usd=cost,
                    turns=turns,
                )
            )
        return out

    # -- one-shot (worker) -------------------------------------------------
    async def run(self, spec: SessionSpec) -> AsyncIterator[AgentEvent]:
        sdk = _sdk()
        options = self._options(spec)
        auth_reported = False
        try:
            async for message in sdk.query(prompt=spec.prompt, options=options):
                for event in self._normalise(message):
                    if event.kind == "auth_error":
                        auth_reported = True
                    yield event
        except sdk.ResultError as exc:
            # The CLI yielded the error result (already normalised above) and
            # then exited non-zero, which the SDK turns into an exception. An
            # auth refusal is the one case where the payload is the synthetic
            # login prompt: report the cause, not the prompt — and only once,
            # since the frame already produced an ``auth_error``.
            if _looks_like_auth(f"{exc.result or ''}{exc}"):
                if not auth_reported:
                    yield AgentEvent("auth_error", {"error": "authentication_failed"})
            else:
                yield AgentEvent("error", {"error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:  # noqa: BLE001 - surfaced to the orchestrator
            yield AgentEvent("error", {"error": f"{type(exc).__name__}: {exc}"})

    # -- persistent (main agent) -------------------------------------------
    def open_client(self, spec: SessionSpec) -> "AgentClient":
        return _SDKClient(spec, self._options(spec))


class _SDKClient:
    def __init__(self, spec: SessionSpec, options: Any) -> None:
        self.spec = spec
        self.options = options
        self._client: Any = None

    async def __aenter__(self) -> "_SDKClient":
        sdk = _sdk()
        self._client = sdk.ClaudeSDKClient(options=self.options)
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(*exc)
            except Exception:  # noqa: BLE001 - closing must not mask the real error
                pass

    def send(self, text: str) -> AsyncIterator[AgentEvent]:
        return self._send(text)

    async def interrupt(self) -> None:
        if self._client is not None:
            await self._client.interrupt()

    async def _send(self, text: str) -> AsyncIterator[AgentEvent]:
        sdk = _sdk()
        await self._client.query(text)
        try:
            async for message in self._client.receive_response():
                for event in ClaudeSDKBackend._normalise(message):
                    yield event
                    if event.kind == "result":
                        return
        except sdk.ResultError as exc:
            # A failed turn is normally reported by its result frame, and an
            # auth refusal has already produced an ``auth_error`` above. This
            # branch covers a CLI that exits before the frame is delivered: the
            # exception text is again the synthetic login prompt, so it must be
            # translated rather than shown.
            if _looks_like_auth(f"{exc.result or ''}{exc}"):
                yield AgentEvent("auth_error", {"error": "authentication_failed"})
            else:
                yield AgentEvent("error", {"error": f"{type(exc).__name__}: {exc}"})


class ScriptedBackend:
    """Replays scripted turns. Used by the test suite.

    ``script`` maps role -> list of turn scripts; each turn is a list of events.
    When a role's script runs out, the session yields a successful result.

    ``driver`` is the escape hatch for testing the *orchestration* rather than
    the replay: an async callable ``(spec, text) -> list[AgentEvent]`` that is
    invoked for every turn before the scripted turns are consulted. A test uses
    it to act as the main agent — calling ``run.tools.dispatch_task(...)`` and
    friends from inside a "session turn" — without an LLM in the loop.
    """

    def __init__(self, script: dict[str, list[list[AgentEvent]]] | None = None) -> None:
        self.script = script or {}
        self.calls: list[SessionSpec] = []
        self.driver: Any = None
        self.turns: dict[str, int] = {}
        #: Turns served through ``open_client`` (the main agent), which does not
        #: go through ``run``. A test asserting the lead was not asked again
        #: after its session was spent needs this counter.
        self.client_turns: dict[str, int] = {}
        #: Every prompt a scripted client received, in order. Tests read this to
        #: check what the operator's turn actually said to the lead.
        self.client_texts: list[str] = []
        #: Specs the main-agent client was opened with (one per open), so a test
        #: can see whether a session was resumed.
        self.client_specs: list[SessionSpec] = []
        #: Roles whose in-flight turn an operator message cut short.
        self.interrupts: list[str] = []

    async def run(self, spec: SessionSpec) -> AsyncIterator[AgentEvent]:
        self.calls.append(spec)
        self.turns[spec.role] = self.turns.get(spec.role, 0) + 1
        turns = self.script.get(spec.role) or []
        saw_result = False
        if turns:
            for event in turns.pop(0):
                saw_result = saw_result or event.kind == "result"
                yield event
        if not saw_result:
            # A scripted turn that ends in a result (an exhausted task, say) is
            # complete on its own; appending a success would overwrite it.
            yield AgentEvent(
                "result",
                {"subtype": "success", "is_error": False, "result": "", "session_id": "", "usage": {}},
                cost_usd=0.0,
                turns=1,
            )

    def open_client(self, spec: SessionSpec) -> "AgentClient":
        self.client_specs.append(spec)
        return _ScriptedClient(self, spec)


class _ScriptedClient:
    def __init__(self, backend: ScriptedBackend, spec: SessionSpec) -> None:
        self.backend = backend
        self.spec = spec

    async def __aenter__(self) -> "_ScriptedClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def send(self, text: str) -> AsyncIterator[AgentEvent]:
        return self._send(text)

    async def interrupt(self) -> None:
        self.backend.interrupts.append(self.spec.role)

    async def _send(self, text: str) -> AsyncIterator[AgentEvent]:
        self.backend.client_turns[self.spec.role] = (
            self.backend.client_turns.get(self.spec.role, 0) + 1
        )
        self.backend.client_texts.append(text)
        saw_result = False
        driver = self.backend.driver
        if driver is not None:
            produced = await driver(self.spec, text)
            for event in produced or []:
                saw_result = saw_result or event.kind == "result"
                yield event
        turns = self.backend.script.get(self.spec.role) or []
        if turns:
            for event in turns.pop(0):
                saw_result = saw_result or event.kind == "result"
                yield event
        if not saw_result:
            yield AgentEvent(
                "result",
                {"subtype": "success", "is_error": False, "result": "", "session_id": "", "usage": {}},
                cost_usd=0.0,
                turns=1,
            )


# --------------------------------------------------------------------- helpers


def _short(value: Any, limit: int = 4000) -> Any:
    """Bound what reaches the DB and the UI. Full output stays in the workspace."""
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        return value[:limit] + f"\n…（已截断，另有 {len(value) - limit} 个字符）"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_short(v, limit) for v in value[:40]]
    return value
