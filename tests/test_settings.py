"""The credentials page, and the file editing behind it.

Two things are being pinned here. First, that a saved token actually reaches the
running config — sessions read ``cfg.models`` per dispatch, so no restart is
needed, and a page that only writes a file would be a lie. Second, that editing
``config.toml`` does not eat the comments around it: they are where this project
keeps the reasons for its defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ctfagent import settings as model_settings
from ctfagent.config import AppConfig
from ctfagent.server import create_app

SAMPLE = """\
# 顶部注释要保留
[models]
main = "deepseek-v4-pro"
# 这一行注释也要保留
worker = "deepseek-v4-flash"
base_url = "https://old.example.com"
auth_token = "old-token-value"

[limits]
max_sessions = 6
"""


@pytest.fixture()
def client(cfg, monkeypatch, tmp_path: Path):
    monkeypatch.setattr("ctfagent.server.REPO_ROOT", tmp_path)
    app = create_app(cfg, scripted=True)
    with TestClient(app) as test_client:
        yield test_client


def test_the_page_renders_and_masks_the_token(client, cfg) -> None:
    cfg.models.auth_token = "sk-super-secret-value"
    response = client.get("/settings")
    assert response.status_code == 200
    assert "模型凭据" in response.text
    # The masked form is shown; the full value never reaches the page.
    assert "…alue" in response.text
    assert "sk-super-secret-value" not in response.text


def test_saving_writes_the_file_and_the_running_config(
    client, cfg, tmp_path: Path
) -> None:
    (tmp_path / "config.toml").write_text(SAMPLE, encoding="utf-8")

    response = client.post(
        "/api/settings/credentials",
        data={
            "base_url": "https://new.example.com",
            "auth_token": "brand-new-token",
            "api_key": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'base_url = "https://new.example.com"' in text
    assert 'auth_token = "brand-new-token"' in text
    assert "# 这一行注释也要保留" in text
    assert "max_sessions = 6" in text

    # Applied in memory too, so the next dispatch uses it without a restart.
    assert cfg.models.base_url == "https://new.example.com"
    assert cfg.models.auth_token == "brand-new-token"


def test_a_blank_secret_leaves_it_alone(client, cfg, tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(SAMPLE, encoding="utf-8")
    cfg.models.auth_token = "keep-me"

    client.post(
        "/api/settings/credentials",
        data={"base_url": "https://moved.example.com", "auth_token": "", "api_key": ""},
        follow_redirects=False,
    )

    assert cfg.models.auth_token == "keep-me"
    assert 'auth_token = "old-token-value"' in (tmp_path / "config.toml").read_text(
        encoding="utf-8"
    )


def test_clearing_is_its_own_action(client, cfg, tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(SAMPLE, encoding="utf-8")
    cfg.models.auth_token = "gone-soon"
    cfg.models.api_key = "also-gone"

    client.post(
        "/api/settings/credentials",
        data={"base_url": "https://old.example.com", "auth_token": "", "api_key": "",
              "clear_secrets": "1"},
        follow_redirects=False,
    )

    assert cfg.models.auth_token == ""
    assert cfg.models.api_key == ""
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'auth_token = ""' in text


def test_writing_escapes_quotes_and_backslashes(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[models]\nmain = \"x\"\n", encoding="utf-8")

    model_settings.write_credentials(path, auth_token='a"b\\c')

    text = path.read_text(encoding="utf-8")
    assert 'auth_token = "a\\"b\\\\c"' in text
    # And it still parses as TOML.
    import tomllib

    parsed = tomllib.loads(text)
    assert parsed["models"]["auth_token"] == 'a"b\\c'


def test_a_missing_models_section_is_created(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[limits]\nmax_sessions = 6\n", encoding="utf-8")

    model_settings.write_credentials(path, base_url="https://x.example.com")

    text = path.read_text(encoding="utf-8")
    assert "[models]" in text
    assert 'base_url = "https://x.example.com"' in text
    import tomllib

    parsed = tomllib.loads(text)
    assert parsed["limits"]["max_sessions"] == 6
    assert parsed["models"]["base_url"] == "https://x.example.com"


def test_mask_keeps_only_a_hint() -> None:
    assert model_settings.mask("") == ""
    # Short values reveal nothing: two characters of an eight-character token is
    # a quarter of it.
    assert model_settings.mask("abc") == "…"
    assert model_settings.mask("abcdefghij") == "…"
    assert model_settings.mask("sk-super-secret-value") == "…alue"


def test_the_probe_says_what_is_missing() -> None:
    ok, detail = model_settings.probe("", "")
    assert ok is False
    assert "base_url" in detail

    ok, detail = model_settings.probe("https://example.com", "")
    assert ok is False
    assert "token" in detail


def test_state_reports_the_environment_as_the_winner() -> None:
    cfg = AppConfig()
    cfg.models.auth_token = "abc"
    cfg.models.credential_source = "env"
    state = model_settings.state(cfg)
    assert state["env_wins"] is True
    assert state["source_label"] == "环境变量"
