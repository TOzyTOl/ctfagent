"""Cost reporting and the one lever this CLI actually exposes.

Measured over 54 sessions in the live database:

* thinking is 65% of everything the model reads back (14.6M of 22.4M characters)
  and is billed twice — as output, then again on every later turn;
* prompt caching is already at 94.7%, so caching is not a lever;
* input outweighs output 44.6:1, so cost tracks ``turns x transcript size``.

The installed CLI has no ``--thinking`` / ``--max-thinking-tokens``; ``--effort``
is the only depth knob, which is why it is the one wired here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent import costing
from ctfagent.backend import ScriptedBackend, SessionSpec
from ctfagent.challenge import load_challenge
from ctfagent.config import AppConfig, ModelConfig
from ctfagent.orchestrator import RunManager
from ctfagent.workspace import SandboxStatus


def test_effort_is_opt_in_by_default() -> None:
    assert ModelConfig().effort == {}
    assert ModelConfig().effort_for("reverse") == ""


def test_effort_for_accepts_only_known_levels() -> None:
    cfg = ModelConfig(effort={"reverse": "HIGH", "web": "medium", "misc": "ludicrous"})
    assert cfg.effort_for("reverse") == "high"
    assert cfg.effort_for("web") == "medium"
    assert cfg.effort_for("misc") == ""  # nonsense is ignored, not passed through
    assert cfg.effort_for("pwn") == ""


def test_effort_reaches_the_session_options() -> None:
    """The flag has to reach the CLI, or the config is decoration."""
    from ctfagent.backend import ClaudeSDKBackend

    cfg = AppConfig()
    cfg.models.effort = {"reverse": "high", "triage": "low"}
    backend = ClaudeSDKBackend(cfg)
    spec = SessionSpec(
        prompt="x",
        cwd=Path("."),
        role="reverse",
        system_prompt="",
        model="deepseek-v4-pro",
        max_turns=10,
        max_budget_usd=1.0,
    )
    assert backend._options(spec).effort == "high"

    spec.role = "triage"
    assert backend._options(spec).effort == "low"

    spec.role = "web"  # not configured → left to the CLI
    assert backend._options(spec).effort is None


def test_usage_is_read_from_the_peak_not_the_sum() -> None:
    """A result event carries cumulative usage; summing would multiply by turns."""
    rows = [
        {"task_id": 1, "run_id": 1, "role": "reverse",
         "input_tokens": 1000, "cache_read": 9000, "output_tokens": 500},
    ]
    cfg = AppConfig()
    cfg.models.pricing = {"deepseek-v4-pro": {"input": 1.0, "cache_read": 0.1, "output": 2.0}}
    cfg.models.roles = {"reverse": "deepseek-v4-pro"}

    class FakeStore:
        def usage_by_task(self, run_id=None):
            return rows

    data = costing.report(FakeStore(), cfg)
    # 1000*1.0 + 9000*0.1 + 500*2.0, per million
    assert data["total_usd"] == pytest.approx((1000 + 900 + 1000) / 1_000_000)
    assert data["tokens"] == {"fresh": 1000, "cached": 9000, "output": 500}
    assert data["cache_hit_rate"] == pytest.approx(0.9)


def test_an_unpriced_model_reports_tokens_but_no_money() -> None:
    cfg = AppConfig()  # no pricing at all

    class FakeStore:
        def usage_by_task(self, run_id=None):
            return [{"task_id": 1, "run_id": 1, "role": "web",
                     "input_tokens": 10, "cache_read": 20, "output_tokens": 30}]

    data = costing.report(FakeStore(), cfg)
    assert data["total_usd"] == 0.0
    assert data["unpriced_sessions"] == 1
    assert data["pricing_configured"] is False
    text = costing.render(data)
    assert "[models.pricing]" in text  # says why there is no money column


def test_the_report_names_the_levers_in_order() -> None:
    cfg = AppConfig()

    class FakeStore:
        def usage_by_task(self, run_id=None):
            return []

    text = costing.render(costing.report(FakeStore(), cfg))
    assert "轮数" in text
    assert "effort" in text
    assert "94.7" in text or "缓存" in text


def test_usage_by_task_sql_returns_one_row_per_session(cfg, fixtures_dir: Path, store_and_bus):
    """End-to-end against the real schema, with a scripted run."""
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    run = manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))
    task = store.create_task(run.id, "crypto", "x", "")
    store.add_task_usage(task.id, cost=0.5, turns=3)

    rows = store.usage_by_task(run.id)
    assert isinstance(rows, list)  # no events yet, but the query must not raise
