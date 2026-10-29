"""Configuration: TOML file + environment overrides.

Priority (highest first): environment -> config.toml -> defaults, with one
exception: credentials and the endpoint fall back to the operator's Claude Code
``settings.json`` when neither the environment nor ``config.toml`` sets them.

Credentials come from the environment by default because that is where the Claude
Code CLI reads them too (``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN`` or
``ANTHROPIC_API_KEY``). A local Anthropic-compatible proxy is the primary setup.

The ``settings.json`` fallback exists because sessions run with
``setting_sources=["project"]``, so the child CLI never reads that file: a
machine whose only token lives in ``~/.claude/settings.json`` looks logged in to
``claude`` and gets ``Not logged in · Please run /login`` from every CTFAGENT
run.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _claude_settings_env() -> dict[str, str]:
    """``env`` entries from the operator's Claude Code settings, if readable.

    Read-only and best effort: a missing, malformed or unreadable file simply
    means "no fallback". Only the keys this module consumes are returned, so a
    settings file cannot inject arbitrary environment into every session.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    wanted = {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # Model aliases: a one-channel gateway (only ``deepseek-v4-flash``, say)
        # is configured here, and the child sessions must see the same aliases
        # or their internal "fast" model calls fail.
        "ANTHROPIC_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    }
    try:
        raw = json.loads((Path(base) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    env = raw.get("env")
    if not isinstance(env, dict):
        return {}
    return {k: str(v) for k, v in env.items() if k in wanted and v}



@dataclass
class ModelConfig:
    """Per-role model names."""

    main: str = "claude-opus-4-8"
    worker: str = "claude-sonnet-5-5"
    verifier: str = "claude-sonnet-5-5"
    writer: str = "claude-sonnet-5-5"
    fallback: str = ""

    #: Per-role model override, e.g. ``[models.roles] reverse = "..."``.
    #:
    #: Directions differ in how much reasoning they need and how much latency
    #: they can afford. Reversing an obfuscated binary is reasoning-bound: a
    #: fast model that flails for 90 turns is slower end-to-end than a slow model
    #: that converges in 40. Writing a report is not. Splitting the assignment is
    #: the one lever that moves solve rate without touching the architecture.
    roles: dict[str, str] = field(default_factory=dict)

    def model_for(self, role: str) -> str:
        """The model a role's session should run on."""
        if role in self.roles:
            return self.roles[role]
        if role == "verifier":
            return self.verifier
        if role == "writer":
            return self.writer
        return self.worker

    #: Base URL of the Anthropic-compatible endpoint. Empty = let the CLI decide.
    base_url: str = ""
    auth_token: str = ""
    api_key: str = ""
    #: USD per million tokens, per model: ``{"input":…, "cache_read":…, "output":…}``.
    #:
    #: The CLI computes ``total_cost_usd`` by looking the model name up in
    #: Anthropic's price table, which is fiction for a gateway model
    #: (``deepseek-*`` via fastmodel.top). Every result event carries the
    #: session's cumulative token usage, so with prices filled in here the
    #: orchestrator books cost from what was actually consumed. A model without
    #: an entry keeps using the CLI's number — the two accounting modes never mix.
    pricing: dict[str, dict[str, float]] = field(default_factory=dict)
    #: How the CLI's own dollar compares to the gateway's, per model:
    #: ``cli_dollars / gateway_dollars`` for a real session.
    #:
    #: Needed because the two ceilings in ``[limits]`` are judged by different
    #: accountants. ``role_budget_usd`` goes to the SDK as ``max_budget_usd`` and
    #: the CLI measures it against its own running total (tokens x its price
    #: table, looked up by model name); ``run_budget_usd`` is filled by
    #: ``_priced_cost`` from ``[models.pricing]`` — real gateway money. Without a
    #: factor, a ceiling of 8 is compared against a remainder in a different
    #: unit, and three parallel workers "reserve" $24 of a $30 pool while
    #: actually being entitled to $1.6 each.
    #:
    #: Measured 2026-10-27 from ``cost-state`` records — the CLI's own
    #: ``totalCostUSD`` against the same record's ``modelUsage`` priced at the
    #: gateway's rates, one ratio per session:
    #:   ``deepseek-v4-pro``    0.92–0.94 (n=7)
    #:   ``deepseek-v4-flash``  4.56–5.38 (n=24)
    #: Flash is the larger number because a flash session is 91% cache-read and
    #: the CLI bills cache at 0.2 where the gateway charges 0.02. A model with no
    #: entry is assumed 1.0, which is what a session priced the same by both
    #: parties would be.
    cli_cost_factor: dict[str, float] = field(default_factory=dict)

    def cli_factor(self, model: str) -> float:
        """CLI dollars per gateway dollar for ``model``; 1.0 when unmeasured."""
        try:
            value = float(self.cli_cost_factor.get(model) or 0.0)
        except (TypeError, ValueError):
            return 1.0
        return value if value > 0 else 1.0
    #: Reasoning effort per role: ``low`` / ``medium`` / ``high`` / ``xhigh`` / ``max``.
    #:
    #: The single biggest cost lever on this host. Measured over 54 sessions:
    #: thinking is **65% of everything the model reads back** (14.6M of 22.4M
    #: characters), and it is billed twice — once as output at the highest rate,
    #: then again on every later turn because the transcript carries it. Prompt
    #: caching is already at 94.7%, so there is nothing left to win there.
    #:
    #: This CLI does **not** accept ``--thinking`` / ``--max-thinking-tokens``
    #: (verified against the installed build); ``--effort`` is the only knob it
    #: exposes. Empty means "leave it to the CLI", which is what every run so far
    #: has done. Lowering it trades reasoning depth for money, so measure it
    #: against the benchmark before making it the default — do not set it blind.
    effort: dict[str, str] = field(default_factory=dict)

    def effort_for(self, role: str) -> str:
        """The effort level a role should run at, or "" to leave it to the CLI."""
        level = str(self.effort.get(role) or "").strip().lower()
        return level if level in ("low", "medium", "high", "xhigh", "max") else ""

    #: Thinking budget per role, in tokens. ``0`` disables thinking entirely;
    #: a positive number caps it; absent leaves the decision to the CLI.
    #:
    #: Thinking is 65% of everything a session reads back, and the transcript
    #: carries it to every later turn — so it is the one thing that shrinks the
    #: *input* side of the bill, not just the output side. The installed CLI
    #: accepts ``--thinking`` / ``--max-thinking-tokens`` even though neither
    #: appears in ``--help`` (verified: both flags are parsed and the session
    #: runs), which is why this is wired through the SDK's ``thinking`` field.
    #:
    #: Same caveat as ``effort``: this trades reasoning depth for money. Measure
    #: it before making it the default.
    thinking: dict[str, int] = field(default_factory=dict)

    def thinking_for(self, role: str) -> int | None:
        """Token budget for a role's thinking, 0 for off, None to leave it alone."""
        raw = self.thinking.get(role)
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return max(0, value)
    #: "anthropic" (default) or "openai" (a translation proxy must sit in front).
    api_format: str = "anthropic"
    #: Where the credentials came from: ``env``, ``toml``, ``settings`` or ``""``.
    #: Set by :func:`load_config`; the UI shows it so an operator can tell which
    #: file to edit when runs come back "Not logged in".
    credential_source: str = ""


@dataclass
class LimitsConfig:
    #: Max concurrent Claude sessions across ALL runs. Each session is a `claude`
    #: subprocess (~150-250MB), so this is a memory knob as much as a rate knob.
    max_sessions: int = 6
    #: Max concurrently running worker tasks inside one run.
    workers_per_run: int = 3
    #: Max runs executing at once. Queued runs wait.
    max_active_runs: int = 3
    #: Wall-clock ceiling for a whole run, seconds.
    run_timeout_s: int = 5400
    #: Hard dollar ceiling for a whole run (sum of session costs).
    #:
    #: A ceiling is not a target. It exists so a runaway session cannot bill
    #: without bound; the work itself should end when the lead has an answer or
    #: has exhausted its ideas, not when it runs out of room mid-investigation.
    #: Observed: a $5 ceiling cut five workers off at $1.00 each while they were
    #: still making progress.
    run_budget_usd: float = 30.0
    #: The lead agent's own ceiling. Without this it inherits the whole run
    #: budget per turn, which lets one session spend the run several times over.
    #: Also CLI dollars — see ``ModelsConfig.cli_cost_factor``.
    main_budget_usd: float = 6.0
    #: Per-worker-session ceilings.
    task_max_turns: int = 150
    #: Default per-worker ceiling, CLI dollars, for a role with no entry in
    #: ``role_budget_usd``.
    task_budget_usd: float = 4.0
    #: Per-role turn ceilings, the counterpart of ``role_budget_usd``. A reverse
    #: task that unpacks a VM and traces it needs more turns than a web task that
    #: reads one endpoint. Observed: two reverse workers in one run stopped at
    #: exactly 151 turns while still making progress, and their conclusions were
    #: never written down.
    role_max_turns: dict[str, int] = field(
        default_factory=lambda: {
            "reverse": 250,
            "pwn": 250,
            "malware": 250,
            "forensics": 200,
            "crypto": 200,
        }
    )
    #: Turns granted to a worker that just hit its turn ceiling, so it can write
    #: down what it established instead of stopping mid-sentence. Cheap next to
    #: re-dispatching the same direction from a clean transcript.
    task_wrapup_turns: int = 8
    task_wrapup_budget_usd: float = 0.75
    #: Per-role overrides. A reverse task that must unpack and trace a binary
    #: needs more room than a web task that reads one endpoint; one shared
    #: ceiling makes one of the two wrong.
    #:
    #: **These numbers are CLI dollars, and the CLI's dollar is not the
    #: gateway's.** The value reaches the SDK as ``max_budget_usd``; the CLI
    #: compares it against its own running total, which is a price-table lookup
    #: by model name (tokens x a fixed table), while ``run_budget_usd`` below is
    #: filled by ``_priced_cost`` from the rates in ``[models.pricing]`` — that
    #: is, real gateway money. The two ceilings are in different units.
    #:
    #: The CLI's table changed on 2026-10-23, and with it the conversion. Fitting
    #: every cost-state record on this account (residual 0.0000):
    #:   until 10-22  sessions named ``claude-sonnet-5-5`` billed 2 / 10 / 0.1
    #:                per million tokens (input / output / cache-read); sessions
    #:                named ``deepseek-v4-*`` billed 5 / 25 / 0.5
    #:   10-23 on     both bill 4 / 20 / 0.2 — 27 of 27 sessions on 10-27
    #: Gateway, for comparison: pro 4.5 / 13.5 / 0.15, flash 1 / 4 / 0.02.
    #:
    #: What one CLI dollar buys therefore depends on the role's model, because
    #: the two models differ 7.5x on cache-read and a session is mostly
    #: cache-read:
    #:   pro   (reverse/pwn/crypto/malware/verifier)  1 CLI$ = $1.07 real
    #:   flash (web/misc/forensics/triage/writer)     1 CLI$ = $0.20 real
    #: So a shared ceiling of 8 is $8.6 of reversing but $1.6 of web work. The
    #: flash ceilings below are deliberately the larger numbers in CLI dollars
    #: even though flash is the cheap model.
    #:
    #: Those two factors live in ``[models.cli_cost_factor]``, and ``_task_budget``
    #: divides the ceiling by them before reserving it against the run's
    #: gateway-dollar budget, so the reservation is in the budget's unit. Change
    #: the factors when the CLI's table changes; these numbers are calibrated
    #: against them.
    #:
    #: Sized off measured spend, not intuition — heaviest real session per role
    #: (every result event's ``usage`` priced at the gateway's rate for the model
    #: that session actually ran), converted at the factor above, rounded up:
    #:   role       heaviest    task   -> CLI$   set
    #:   reverse    $13.13      28        14.0   15
    #:   verifier   $4.22       137        4.5    8
    #:   crypto     $5.22       25         5.6    8
    #:   web        $1.49       47         7.5    8
    #:   misc       $1.45       53         7.3    8
    #:   forensics  $1.16       23         5.8    8
    #:   pwn        $0.74       86         0.8    8
    #:   writer     $0.60       84         3.0    4
    #:   triage     $0.45       21         2.3    3
    #: The pro roles are fitted on real samples. pwn and malware have no sample
    #: on pro yet (pwn's two sessions ran on flash, before ``[models.roles]``
    #: moved it), so 8 and 15 are headroom at pro's rate — $8.6 and $16 of tokens,
    #: which is a reversing-sized allowance for a role that has never spent it.
    #:
    #: Under the old tables 8 CLI$ was worth $12.9 of reversing (table A), so the
    #: real allowance for the pro roles barely moves; what changes is that the
    #: intent is now written in the unit the CLI judges. Tasks 22, 24, 27, 28, 71,
    #: 86, 90, 91, 92, 93 all stopped at the old ceiling while still making
    #: progress — 22 and 24 at exactly 151 turns, 28 at 226 — so the direction is
    #: up: a reverse session should now be cut by the $30 run budget, not by this.
    #:
    #: One consequence of the unit split worth knowing: ``_task_budget`` caps
    #: these CLI-dollar ceilings by what is left of the gateway-dollar run
    #: budget, so a flash role reserves ~5x the real money it can spend. That
    #: direction is safe (the run ceiling binds early rather than late) and is
    #: why the flash numbers can afford to be generous.
    role_budget_usd: dict[str, float] = field(
        default_factory=lambda: {
            "triage": 3.0,
            "reverse": 15.0,
            "pwn": 8.0,
            "malware": 15.0,
            "forensics": 8.0,
            "crypto": 8.0,
            "web": 8.0,
            "misc": 8.0,
            "ai_ml": 8.0,
            "osint": 8.0,
            "verifier": 8.0,
            "writer": 4.0,
        }
    )
    #: Whether confirming a flag also dispatches a writer to produce the report.
    #:
    #: Off by default. In a competition the next challenge matters more than the
    #: writeup, and a writer session started automatically costs time and money
    #: before anyone asked for it. The report stays available on request —
    #: ``Run.write_report()``, the run page's 写报告 button, or
    #: ``ctfagent writeup <run>`` — against the same finished run.
    writeup_on_confirm: bool = False
    #: Whether confirming a flag also distils the run into a technique note
    #: (``Run.distill_technique``), which is what makes the platform get better
    #: at a direction instead of just remembering individual challenges.
    #:
    #: Off by default for the same reason as ``writeup_on_confirm``: it spends a
    #: writer session at the one moment a competition cares least. The note is
    #: only reachable afterwards through the button, ``ctfagent distill <run>``,
    #: or a batch over the solves that proved something — and a failed
    #: distillation never blocks the close.
    distill_on_confirm: bool = False
    task_timeout_s: int = 1800
    #: How many times the *same* tool call may be repeated before a worker is
    #: stopped. 0 disables it.
    #:
    #: This replaced an earlier guard that counted tool calls since the last
    #: ``record_fact``. That one was wrong, and the data says so plainly: the two
    #: most expensive sessions made 225 and 157 tool calls, but **188 and 129 of
    #: them were distinct** (16-18% repetition). They were not spinning — they
    #: were unpacking and tracing an obfuscated binary, and simply did not write
    #: facts down along the way. Cutting them at 40 calls would have thrown away
    #: real analysis, so the signal had to change: repetition is waste, silence
    #: is not.
    repeat_call_limit: int = 6
    #: Tool calls a worker may make without recording a fact. **Off by default.**
    #:
    #: Kept only as an opt-in for a run that is known to be spinning; the
    #: measurement above is why it is not on. A worker doing deep analysis
    #: legitimately runs dozens of distinct commands between conclusions.
    #:
    #: Re-measured over the whole database (2026-10-27) before trying to turn it
    #: on, and every threshold failed the same way:
    #:
    #: - this count: 15/20/30/40/60 calls kills 80/59/45/33/16 tasks, including
    #:   ones that solved (#21, #25, #23). Task 28 ran 225 calls with no fact;
    #:   task 94 solved the challenge on its first fact, after 66 calls.
    #: - windowed distinct-signature ratio (raw, digit/path-normalised, and a
    #:   content-blind "tool + first path" shape): either catches nothing or
    #:   cuts solved work, because real reversing *is* forty near-identical
    #:   ``read``/``objdump`` calls. Shape fingerprints are worse than content
    #:   ones here, not better.
    #: - spend since the last fact: the solved task with the most expensive
    #:   single fact-free segment (#94, flash) spent $1.03 in one 66-call run.
    #: - "no fact at all for the whole session": only distinguishes roles that
    #:   never call ``record_fact`` (``CLOSING_ROLES``), not spinning.
    #:
    #: The one signal that does separate is the **tool-error rate**: ten
    #: consecutive tool results at >=50% error (``is_error`` or a known
    #: not-found/usage/traceback string) during a fact-free stretch fires on
    #: exactly the two reverse sessions that burned their whole ceiling with no
    #: output (#22 at call 116/157, #28 at 209/225) and one stuck pwn worker
    #: (#86 at 96/105) — and on no completed task once ``triage``/verifier/
    #: writer are exempt. It is **not implemented**: it fires only in the last
    #: 7-26% of those sessions, so it saves little, and a guard that cannot be
    #: shown to fire early is not worth a new knob. Revisit with more failing
    #: runs, not with a lower threshold.
    no_progress_tools: int = 0
    #: How many facts a worker's blackboard digest carries. Runs produce 51-74
    #: facts; the digest used to take the newest 40, which silently dropped the
    #: oldest 11-34 — and the oldest records are the foundational ones ("this is
    #: a PE32+ with no PIE", "the encoding is byte-swapped"). A worker that
    #: cannot see them re-derives them.
    blackboard_facts: int = 80
    #: A session that emits no event for this long is considered stuck.
    #: Only applies while the model is thinking: a running tool emits nothing
    #: either, and that silence is not a hang.
    stall_timeout_s: int = 300
    #: How many times a session that went silent mid-thought is resumed before
    #: the task is closed. A model stall is a gateway hiccup, not a bad
    #: transcript: the session has already done the work, and killing it throws
    #: that away. Measured on run 15 — four reverse tasks were killed at exactly
    #: 300 s after 16-66 tool calls each, 1.91 h of analysis discarded, and each
    #: was recorded as ``turns=0, cost=$0`` so the dashboard showed nothing had
    #: happened. Resuming costs one turn; re-dispatching costs the whole session.
    stall_retries: int = 1
    #: How long a session may stay silent while one of its tools is executing.
    #: A worker running gdb under WSL, a docker pull or a 10-minute packet
    #: replay produces no events until the tool returns. Must stay above the
    #: CLI's own tool ceiling (600 s) so a genuinely hung tool is still caught
    #: by this clock rather than waiting forever.
    tool_wait_timeout_s: int = 900


@dataclass
class SandboxConfig:
    #: "none" = run on the host; "docker" = the prompt tells the agent commands
    #: run in a container. Note that the second value is currently advisory: the
    #: shell tool is not actually wrapped, so set it only if the container
    #: wrapper has been implemented.
    mode: str = "none"
    image: str = "ctfagent/kali:latest"
    network: str = "bridge"


@dataclass
class AppConfig:
    data_dir: Path = REPO_ROOT / "workspaces"
    db_path: Path = REPO_ROOT / "ctfagent.db"
    challenges_dir: Path = REPO_ROOT / "challenges"
    skills_dir: Path = REPO_ROOT / "plugins" / "ctf-skills" / "skills"
    host: str = "127.0.0.1"
    port: int = 8788
    models: ModelConfig = field(default_factory=ModelConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    #: Optional explicit path to the `claude` CLI. Empty = SDK's bundled CLI.
    cli_path: str = ""

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)
        self.challenges_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def _apply_env(cfg: AppConfig) -> AppConfig:
    # The operator's Claude Code settings are the last resort, not an override:
    # an explicit environment or config.toml value always wins. ``from_toml`` is
    # what the file already supplied, so the provenance reported to the UI names
    # the layer that actually won.
    settings_env = _claude_settings_env()
    from_toml = (cfg.models.auth_token, cfg.models.api_key)
    cfg.models.base_url = (
        _env("CTFAGENT_BASE_URL", cfg.models.base_url)
        or _env("ANTHROPIC_BASE_URL", "")
        or settings_env.get("ANTHROPIC_BASE_URL", "")
    )
    cfg.models.auth_token = (
        _env("CTFAGENT_AUTH_TOKEN", cfg.models.auth_token)
        or _env("ANTHROPIC_AUTH_TOKEN", "")
        or settings_env.get("ANTHROPIC_AUTH_TOKEN", "")
    )
    cfg.models.api_key = (
        _env("CTFAGENT_API_KEY", cfg.models.api_key)
        or _env("ANTHROPIC_API_KEY", "")
        or settings_env.get("ANTHROPIC_API_KEY", "")
    )
    env_creds = any(
        os.environ.get(name)
        for name in (
            "CTFAGENT_AUTH_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
            "CTFAGENT_API_KEY",
            "ANTHROPIC_API_KEY",
        )
    )
    if cfg.models.auth_token or cfg.models.api_key:
        cfg.models.credential_source = (
            "env" if env_creds
            else "toml" if any(from_toml)
            else "settings"
        )

    cfg.cli_path = _env("CTFAGENT_CLI_PATH", cfg.cli_path)
    if v := _env("CTFAGENT_MODEL_MAIN"):
        cfg.models.main = v
    if v := _env("CTFAGENT_MODEL_WORKER"):
        cfg.models.worker = v
    if v := _env("CTFAGENT_MAX_SESSIONS"):
        cfg.limits.max_sessions = int(v)
    if v := _env("CTFAGENT_WORKERS_PER_RUN"):
        cfg.limits.workers_per_run = int(v)
    if v := _env("CTFAGENT_DATA_DIR"):
        cfg.data_dir = Path(v)
    if v := _env("CTFAGENT_DB_PATH"):
        cfg.db_path = Path(v)
    if v := _env("CTFAGENT_PORT"):
        cfg.port = int(v)
    return cfg


def _from_toml(path: Path) -> AppConfig:
    cfg = AppConfig()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    for section, target in (
        ("models", cfg.models),
        ("limits", cfg.limits),
        ("sandbox", cfg.sandbox),
    ):
        for key, value in (raw.get(section) or {}).items():
            if hasattr(target, key):
                setattr(target, key, value)
    for key in ("data_dir", "db_path", "challenges_dir", "skills_dir", "host", "port", "cli_path"):
        if key in raw:
            value = raw[key]
            if key.endswith("_dir") or key == "db_path":
                value = Path(value)
                if not value.is_absolute():
                    value = path.parent / value
            setattr(cfg, key, value)
    return cfg


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load config.toml if present, then overlay environment variables."""
    candidate = Path(path) if path else REPO_ROOT / "config.toml"
    cfg = _from_toml(candidate) if candidate.exists() else AppConfig()
    cfg = _apply_env(cfg)
    cfg.ensure_dirs()
    return cfg


def default_config_toml() -> str:
    return """# CTFAGENT configuration
# Credentials come from the environment by default:
#   ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY)
[models]
main = "claude-opus-4-8"
worker = "claude-sonnet-5-5"
verifier = "claude-sonnet-5-5"
writer = "claude-sonnet-5-5"
fallback = ""
# base_url = "http://127.0.0.1:15721"
# auth_token = "..."      # prefer the environment over the file
api_format = "anthropic"
# 按 token 自算成本（网关模型必需：CLI 的成本是按 Anthropic 价目表查模型名算的，
# 对 deepseek-* 之类的模型是虚构数字）。单位：美元 / 百万 token。
# 配了表的模型按每次调用上报的 usage 记账；没配表的继续用 CLI 报告值。
# 单价不要凭印象填：先跑 scripts/calibrate_price.py，它从网关自己公布的计费公式
# 推出系数，再用账单计数器核对。下面的空表只是格式示例：
# [models.pricing]
# "your-model" = { input = 0.0, cache_read = 0.0, output = 0.0 }
# CLI 的美金 ÷ 网关的真美金，按模型。role_budget_usd 等上限由 CLI 按它自己的
# 价目表判定，run_budget_usd 由 _priced_cost 按上面的真价记，两者单位不同；
# 这张表把它们换算到同一个单位。没测过的模型按 1.0 处理。
# [models.cli_cost_factor]
# "your-model" = 1.0

# 推理深度（low/medium/high/xhigh/max）。这是本机最大的成本杠杆：thinking 占模型
# 每轮读回内容的 65%，且按最高价计一次输出、再在之后每一轮里被重复读取。
# 这个 CLI 不支持 --thinking，只有 --effort。不填 = 交给 CLI 默认（现有行为）。
# 降它等于拿推理深度换钱，必须先用基准测过再动，别盲设。
# [models.effort]
# triage = "low"
# writer = "low"
# osint = "low"
# web = "medium"
# misc = "medium"
# forensics = "medium"
# reverse = "high"
# crypto = "high"
# pwn = "high"

# 思考预算（token）：0 = 关闭，正整数 = 上限，不填 = 交给 CLI。
# thinking 占会话内容的 65%，且被 transcript 带到之后每一轮 —— 唯一能压输入侧的旋钮。
# CLI 的 --help 里没有 --thinking，但实测接受。
# [models.thinking]
# triage = 0
# writer = 0
# web = 2000
# reverse = 8000

[limits]
max_sessions = 6
workers_per_run = 3
max_active_runs = 3
run_timeout_s = 5400
# A ceiling, not a target: it exists to stop a runaway session, not to cut a
# worker off while it is still making progress.
run_budget_usd = 30.0
main_budget_usd = 6.0
task_max_turns = 150
task_budget_usd = 4.0
# 同一条工具调用连续重复多少次就停手（0 = 关闭）。只拦一模一样地连发。
repeat_call_limit = 6
# 连续多少次调用没有新结论就停手。默认关闭：深度分析会跑几十条不同的命令。
no_progress_tools = 0
# 每个角色的会话上限。**单位是 CLI 美元，不是网关美元**：这个数作为
# max_budget_usd 交给 SDK，CLI 拿它和自己按模型名查价目表累加的数字比；而
# run_budget_usd 是 _priced_cost 按 [models.pricing] 记的真实网关钱。两者不同单位。
# 2026-10-23 前 CLI 表是 2/10/0.1（名字 claude-sonnet-5-5）和 5/25/0.5（deepseek-v4-*），
# 之后统一 4/20/0.2。按 4/20/0.2 换算：pro 的 1 CLI$ ≈ $1.07 真实，flash 的 1 CLI$ ≈ $0.20。
# 所以 flash 角色的数字故意更大（按真实花费反推），不是写错。
# 逐条来源与实测见 ctfagent/config.py 里 LimitsConfig.role_budget_usd 的注释。
role_budget_usd = { triage = 3.0, reverse = 15.0, pwn = 8.0, malware = 15.0, forensics = 8.0, crypto = 8.0, web = 8.0, misc = 8.0, ai_ml = 8.0, osint = 8.0, verifier = 8.0, writer = 4.0 }
# Directions that need many turns (unpacking, tracing, replaying) get more of
# them. The ceiling says "stop investigating", not "stop mid-sentence": a worker
# that hits it is resumed for a few turns to write down what it established.
role_max_turns = { reverse = 250, pwn = 250, malware = 250, forensics = 200, crypto = 200 }
task_wrapup_turns = 8
task_wrapup_budget_usd = 0.75
# 确认 flag 后不自动写报告：比赛里确认完就该走下一题。
# 报告改成显式动作（运行页的「写报告」按钮，或 ctfagent writeup <run>）。
writeup_on_confirm = false
# 确认 flag 后也不自动沉淀技术笔记：同样要起一个 writer 会话。
# 笔记是「越用越强」的另一半（卡记题目、笔记记方法），随时可补：
# 运行页的「沉淀笔记」按钮，或 ctfagent distill <run_id>。赛后批量做更划算。
distill_on_confirm = false
task_timeout_s = 1800
stall_timeout_s = 300
# Silence while a tool runs is not a hang; this clock is longer on purpose.
tool_wait_timeout_s = 900
# A model stall is a transport hiccup, not a bad transcript: resume once before
# closing the task.
stall_retries = 1

[sandbox]
mode = "none"             # "docker" is advisory: the shell tool is not wrapped yet
image = "ctfagent/kali:latest"
network = "bridge"
"""
