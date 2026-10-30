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


# --------------------------------------------------- switching moves the models


def test_pick_served_model_keeps_the_role_on_the_same_tier() -> None:
    """A reasoning role stays strong, a bulk role stays fast, across gateways."""
    served = ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert model_settings.pick_served_model(served, "spark-x2.6", strong=True) == "deepseek-v4-pro"
    assert model_settings.pick_served_model(served, "spark-x2.6", strong=False) == "deepseek-v4-flash"
    # A name the endpoint does serve is never second-guessed.
    assert model_settings.pick_served_model(served, "deepseek-v4-flash", strong=True) == "deepseek-v4-flash"
    # No recognisable tier: any model the endpoint serves beats a 503.
    assert model_settings.pick_served_model(["only-model"], "spark-x2.6", strong=True) == "only-model"
    # No list at all means nothing to move to.
    assert model_settings.pick_served_model([], "spark-x2.6", strong=True) == "spark-x2.6"


def test_remap_reports_every_change_and_leaves_served_names_alone() -> None:
    roles = {"main": "spark-x2.6", "worker": "spark-x2.6",
             "verifier": "spark-x2.6", "writer": "spark-x2.6"}
    directions = {"reverse": "spark-x2.6", "web": "deepseek-v4-flash"}

    new_roles, new_dirs, changes = model_settings.remap_to_served(
        ["deepseek-v4-flash", "deepseek-v4-pro"], roles, directions
    )

    assert new_roles["main"] == "deepseek-v4-pro"
    assert new_roles["worker"] == "deepseek-v4-flash"
    assert new_dirs["reverse"] == "deepseek-v4-pro"
    assert new_dirs["web"] == "deepseek-v4-flash"
    assert [c[0] for c in changes] == ["main", "worker", "verifier", "writer", "reverse"]
    assert ("worker", "spark-x2.6", "deepseek-v4-flash") in changes


def test_remap_without_a_model_list_changes_nothing() -> None:
    roles = {"main": "spark-x2.6", "worker": "spark-x2.6",
             "verifier": "spark-x2.6", "writer": "spark-x2.6"}
    new_roles, new_dirs, changes = model_settings.remap_to_served([], roles, {"pwn": "spark-x2.6"})
    assert new_roles == roles
    assert new_dirs == {"pwn": "spark-x2.6"}
    assert changes == []


def test_switching_a_provider_moves_the_assignment_and_the_file(
    client, cfg, tmp_path: Path, monkeypatch
) -> None:
    """The bug this pins: the endpoint changed, the models did not.

    The new gateway answered ``503 model_not_found`` for every session because
    the assignment still named the old gateway's model, and the dropdowns kept
    showing the old list because the switch never probed the new row.
    """
    (tmp_path / "config.toml").write_text(SAMPLE, encoding="utf-8")
    cfg.models.main = cfg.models.worker = "spark-x2.6"
    cfg.models.verifier = cfg.models.writer = "spark-x2.6"
    cfg.models.roles = {"reverse": "spark-x2.6"}

    provider_id = client.app.state.store.add_provider(
        __import__("ctfagent.models", fromlist=["Provider"]).Provider(
            id=0, name="fastmodel", base_url="https://fastmodel.top",
            auth_token="new-token", models=[],
        )
    )
    monkeypatch.setattr(
        model_settings, "probe_models",
        lambda base, token, timeout=8.0: (True, "HTTP 200 · ok",
                                          ["deepseek-v4-flash", "deepseek-v4-pro"]),
    )

    response = client.post(
        f"/api/settings/providers/{provider_id}/switch", follow_redirects=False
    )
    assert response.status_code == 303
    assert "error=" not in response.headers["location"]

    assert cfg.models.main == "deepseek-v4-pro"
    assert cfg.models.worker == "deepseek-v4-flash"
    assert cfg.models.roles["reverse"] == "deepseek-v4-pro"
    # The file is what a restart reads, so it has to carry the same assignment.
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'main = "deepseek-v4-pro"' in text
    assert 'worker = "deepseek-v4-flash"' in text
    assert "spark-x2.6" not in text
    # And the row now carries a model list, which is what the dropdowns read.
    assert client.app.state.store.get_provider(provider_id).models == [
        "deepseek-v4-flash", "deepseek-v4-pro"
    ]


def test_switching_to_an_endpoint_that_fails_its_probe_does_not_switch(
    client, cfg, monkeypatch
) -> None:
    cfg.models.base_url = "https://old.example.com"
    cfg.models.worker = "spark-x2.6"
    provider_id = client.app.state.store.add_provider(
        __import__("ctfagent.models", fromlist=["Provider"]).Provider(
            id=0, name="dead", base_url="https://dead.example.com", auth_token="x",
        )
    )
    monkeypatch.setattr(
        model_settings, "probe_models",
        lambda base, token, timeout=8.0: (False, "HTTP 401：Unauthorized", []),
    )

    response = client.post(
        f"/api/settings/providers/{provider_id}/switch", follow_redirects=False
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert cfg.models.base_url == "https://old.example.com"
    assert cfg.models.worker == "spark-x2.6"


def test_align_moves_an_adopted_assignment_to_the_active_endpoint(
    client, cfg, tmp_path: Path, monkeypatch
) -> None:
    """The active row was adopted from config.toml, so there is no card to switch.

    Its model list is known but the assignment still names a model from another
    gateway; the align action is what fixes that without a round of hand edits.
    """
    from ctfagent.models import Provider

    (tmp_path / "config.toml").write_text(SAMPLE, encoding="utf-8")
    cfg.models.main = cfg.models.worker = "spark-x2.6"
    cfg.models.verifier = cfg.models.writer = "spark-x2.6"
    cfg.models.roles = {}
    store = client.app.state.store
    provider_id = store.add_provider(
        Provider(id=0, name="fastmodel", base_url="https://fastmodel.top",
                 auth_token="tok", models=["deepseek-v4-flash", "deepseek-v4-pro"])
    )
    store.set_setting("active_provider", str(provider_id))
    monkeypatch.setattr(
        model_settings, "probe_models",
        lambda base, token, timeout=8.0: (True, "ok", ["deepseek-v4-flash", "deepseek-v4-pro"]),
    )

    response = client.post("/api/settings/models/align", follow_redirects=False)
    assert response.status_code == 303
    assert "error=" not in response.headers["location"]

    assert cfg.models.main == "deepseek-v4-pro"
    assert cfg.models.writer == "deepseek-v4-flash"
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert 'main = "deepseek-v4-pro"' in text
    # The comments around the assignment survive the rewrite.
    assert "# 这一行注释也要保留" in text


def test_a_generator_model_is_not_chosen_for_a_role() -> None:
    """A gateway lists its whole catalogue; a role must not land on an image model."""
    served = ["gpt-image-2", "minimax_h3_image_audio_to_video", "deepseek-v4-flash"]
    assert model_settings.pick_served_model(served, "spark-x2.6", strong=False) == "deepseek-v4-flash"
    # With nothing else on offer, the only models it has are still what it has.
    assert model_settings.pick_served_model(["gpt-image-2"], "spark", strong=True) == "gpt-image-2"


def test_the_remap_prefers_a_model_that_has_a_price() -> None:
    """Two names for the same tier: book money on the one the table knows.

    fastmodel.top serves both ``deepseek-flash`` and ``deepseek-v4-flash``.
    Picking by list order lands on the alias, whose sessions fall back to the
    CLI's Anthropic price table — the number the budget watchdog exists to stop
    trusting.
    """
    served = ["deepseek-flash", "deepseek-v4-flash", "deepseek-v4-pro"]
    priced = {"deepseek-v4-flash", "deepseek-v4-pro"}
    assert model_settings.pick_served_model(
        served, "spark-x2.6", strong=False, priced=priced
    ) == "deepseek-v4-flash"
    assert model_settings.pick_served_model(
        served, "spark-x2.6", strong=True, priced=priced
    ) == "deepseek-v4-pro"
    # Nothing priced: the first served model still beats a name that 503s.
    assert model_settings.pick_served_model(
        served, "spark-x2.6", strong=False
    ) == "deepseek-flash"
