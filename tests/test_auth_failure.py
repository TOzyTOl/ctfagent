"""An unauthenticated session must not read as the lead's reply.

The child CLI runs with ``setting_sources=["project"]``, so it never reads
``~/.claude/settings.json``; whenever the server process has no token it answers
every turn with a *synthetic* assistant message whose text is the interactive
prompt ``Not logged in · Please run /login``. Stored as ordinary text, that
prompt showed up in the chat pane as though the main agent had said it, and the
run sat in ``running`` waiting for a session that could never answer.

These tests pin both halves: the marker never becomes a message, and the
operator gets a line naming the file to edit.
"""

from __future__ import annotations

import json
from pathlib import Path

from ctfagent import backend as backend_module
from ctfagent.backend import AgentEvent, ScriptedBackend, auth_error_message
from ctfagent.challenge import load_challenge
from ctfagent.config import AppConfig, _apply_env
from ctfagent.models import RunStatus, TaskStatus
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


# --------------------------------------------------------------- config fallback


def test_settings_json_env_is_forwarded_when_process_has_none(tmp_path, monkeypatch) -> None:
    """The operator's settings file is the last resort, not an override."""
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://gateway.example",
                    "ANTHROPIC_AUTH_TOKEN": "from-settings",
                    "ANTHROPIC_MODEL": "one-channel-model",
                    # Not a key this module consumes: it must not leak through.
                    "UNRELATED": "nope",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    for name in (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "CTFAGENT_BASE_URL",
        "CTFAGENT_AUTH_TOKEN",
        "CTFAGENT_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    cfg = _apply_env(AppConfig())
    assert cfg.models.auth_token == "from-settings"
    assert cfg.models.base_url == "https://gateway.example"
    assert cfg.models.credential_source == "settings"
    assert "UNRELATED" not in backend_module._claude_settings_env()


def test_environment_wins_over_settings_file(tmp_path, monkeypatch) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "from-settings"}}), encoding="utf-8"
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "from-env")

    cfg = _apply_env(AppConfig())
    assert cfg.models.auth_token == "from-env"
    assert cfg.models.credential_source == "env"


def test_config_toml_wins_over_the_ambient_environment(monkeypatch) -> None:
    """A provider switched on the page survives a restart.

    The credentials page writes ``config.toml`` and says the switch is saved. It
    only stays saved if the file outranks an ``ANTHROPIC_*`` exported in the
    terminal that started the server — otherwise every restart silently went back
    to the old endpoint while the page still showed the new one.
    """
    monkeypatch.delenv("CTFAGENT_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "from-env")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.example")

    base = AppConfig()
    base.models.auth_token = "from-toml"
    base.models.base_url = "https://toml.example"

    cfg = _apply_env(base)
    assert cfg.models.auth_token == "from-toml"
    assert cfg.models.base_url == "https://toml.example"
    assert cfg.models.credential_source == "toml"


def test_ctfagent_env_still_outranks_config_toml(monkeypatch) -> None:
    """The app's own variables stay the explicit override."""
    monkeypatch.setenv("CTFAGENT_AUTH_TOKEN", "explicit")

    base = AppConfig()
    base.models.auth_token = "from-toml"

    cfg = _apply_env(base)
    assert cfg.models.auth_token == "explicit"
    assert cfg.models.credential_source == "env"


def test_broken_settings_file_is_not_an_error(tmp_path, monkeypatch) -> None:
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert backend_module._claude_settings_env() == {}


# ------------------------------------------------------------ operator-facing text


def test_missing_credential_text_names_the_fix(cfg: AppConfig, monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg.models.auth_token = ""
    cfg.models.api_key = ""
    cfg.models.credential_source = ""

    text = auth_error_message(cfg, "authentication_failed")
    assert "ANTHROPIC_AUTH_TOKEN" in text
    assert "config.toml" in text
    assert "settings.json" in text  # explains why the machine "looks logged in"
    assert "/login" not in text


def test_rejected_credential_text_names_the_source(cfg: AppConfig) -> None:
    cfg.models.auth_token = "still-set-but-refused"
    cfg.models.base_url = "https://gateway.example"
    cfg.models.credential_source = "toml"

    text = auth_error_message(cfg, "authentication_failed")
    assert "config.toml" in text
    assert "https://gateway.example" in text
    assert "/login" not in text


# ------------------------------------------------------------------ the lead loop


class _AuthBackend:
    """Every turn fails the way an unauthenticated CLI fails."""

    def __init__(self) -> None:
        self.client_turns = 0

    def run(self, spec):  # pragma: no cover - workers unused here
        raise AssertionError("no workers expected")

    def open_client(self, spec):
        return _AuthClient(self)


class _AuthClient:
    def __init__(self, backend: _AuthBackend) -> None:
        self.backend = backend

    async def __aenter__(self) -> "_AuthClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def interrupt(self) -> None:
        return None

    async def send(self, text):
        self.backend.client_turns += 1
        yield AgentEvent("init", {"subtype": "init", "session_id": "s-1"})
        yield AgentEvent("auth_error", {"error": "authentication_failed"})
        yield AgentEvent(
            "result",
            {
                "subtype": "success",
                "is_error": True,
                "result": "Not logged in · Please run /login",
                "session_id": "s-1",
            },
            turns=1,
        )


async def test_auth_failure_closes_the_run_without_storing_the_prompt(
    cfg: AppConfig, fixtures_dir: Path, store_and_bus, monkeypatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg.models.auth_token = ""
    cfg.models.api_key = ""
    cfg.models.credential_source = ""
    store, bus = store_and_bus

    manager = RunManager(
        store, bus, cfg, _AuthBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    await run.start()

    # The loop must end on its own: an unauthenticated session has no turn that
    # could ever answer, so staying "running" is the bug this test pins.
    for _ in range(200):
        if store.get_run(run.id).status != RunStatus.RUNNING:
            break
        import asyncio

        await asyncio.sleep(0.02)
    row = store.get_run(run.id)
    assert row.status == RunStatus.FAILED
    assert "ANTHROPIC_AUTH_TOKEN" in (row.summary or "")

    texts = [m.content for m in store.list_messages(run.id) if m.role == "assistant"]
    assert not any("Not logged in" in t for t in texts)

    events = store.recent_events(run.id, limit=500)
    kinds = [e.kind for e in events]
    assert "session.error" in kinds
    assert not any(
        e.kind == "main.message" and "Not logged in" in (e.payload or "") for e in events
    )


# --------------------------------------------------------------- worker semantics


async def test_worker_auth_failure_is_an_error_not_a_summary(
    cfg: AppConfig, fixtures_dir: Path, store_and_bus, monkeypatch
) -> None:
    """A failed worker must not report the login prompt as its conclusion."""
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg.models.auth_token = ""
    cfg.models.api_key = ""
    cfg.models.credential_source = ""
    store, bus = store_and_bus
    backend = ScriptedBackend(
        {
            "triage": [
                [
                    AgentEvent("auth_error", {"error": "authentication_failed"}),
                    AgentEvent(
                        "result",
                        {
                            "subtype": "success",
                            "is_error": True,
                            "result": "Not logged in · Please run /login",
                        },
                    ),
                ]
            ]
        }
    )
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    task = await run.dispatch("triage", "看一眼附件")
    await run.wait([task.id], timeout_s=10)

    finished = store.get_task(task.id)
    assert finished.status == TaskStatus.FAILED
    assert "ANTHROPIC_AUTH_TOKEN" in finished.error
    assert "Not logged in" not in (finished.error or "")
    result = finished.result or {}
    assert "Not logged in" not in str(result.get("summary") or "")


async def test_a_worker_auth_error_does_not_poison_the_run(
    cfg: AppConfig, fixtures_dir: Path, store_and_bus
) -> None:
    """A gateway 401 on a worker must fail that task, not close the run.

    Live symptom (the bench's first real run, lcg_step): the crypto worker
    solved the challenge in 111 seconds, the verifier then hit a transient
    auth failure mid-session, and the run-level sticky flag closed the whole
    run — a solved challenge scored as failed. With credentials configured
    (the transient-401 shape, unlike the no-credentials tests above), the
    lead's own session is healthy and must stay in business.
    """
    cfg.models.credential_source = "settings"
    store, bus = store_and_bus
    backend = ScriptedBackend(
        {
            "triage": [
                [
                    AgentEvent("auth_error", {"error": "authentication_failed"}),
                    AgentEvent(
                        "result",
                        {"subtype": "success", "is_error": True, "result": "", "session_id": "t-1"},
                        turns=1,
                    ),
                ]
            ]
        }
    )
    manager = RunManager(
        store, bus, cfg, backend, SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))

    task = await run.dispatch("triage", "看一眼附件")
    await run.wait([task.id], timeout_s=10)

    assert store.get_task(task.id).status == TaskStatus.FAILED
    assert run._auth_failed is False  # the run is not poisoned
    # the lead is still in business: the next dispatch goes out normally
    backend.script["triage"] = []
    second = await run.dispatch("crypto", "接着做")
    assert second.status == "queued" or second.status == "running"
