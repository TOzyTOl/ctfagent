"""Budget: a ceiling is reserved at dispatch, not only spent.

Measured on run 18: three workers dispatched in the same tick each read
``run_budget_usd - cost_usd`` as the whole remainder and took a full
``role_budget_usd`` apiece. The run committed 3 x $8 + 2 x $6 = $36 of ceilings
against a $30 budget and closed at $35.78 — the overshoot was exactly the last
worker's spend, because cost only arrives when a session emits its result.

These tests pin the three things that made that possible: no reservation, no
soft stop before the hard ceiling, and a worker that was never told its ceiling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ctfagent.backend import AgentEvent, ScriptedBackend
from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.events import EventBus
from ctfagent.models import CLOSING_ROLES
from ctfagent.orchestrator import RunManager
from ctfagent.prompts import budget_block
from ctfagent.workspace import SandboxStatus


@pytest.fixture()
def run(cfg, fixtures_dir: Path, store_and_bus):
    store, bus = store_and_bus
    manager = RunManager(
        store, bus, cfg, ScriptedBackend(), SandboxStatus("none", False, "test"), cfg.skills_dir
    )
    return manager.prepare(store.add_challenge(load_challenge(fixtures_dir / "easy_crypto")))


def test_a_second_dispatch_sees_the_first_reservation(run) -> None:
    """Two workers in flight must not each be promised the whole remainder."""
    run.cfg.limits.run_budget_usd = 10.0
    run.cfg.limits.role_budget_usd["reverse"] = 8.0
    assert run._task_budget("reverse") == pytest.approx(8.0)

    run._committed[1] = 8.0
    # $10 budget, $8 already promised to a running worker: $2 is all that is left.
    assert run._task_budget("reverse") == pytest.approx(2.0)

    run._committed[2] = 2.0
    assert run._task_budget("reverse") == 0.0


def test_a_task_does_not_compete_with_its_own_ceiling(run) -> None:
    """A wrap-up or resume re-asks for its own task, and must not be starved."""
    run.cfg.limits.run_budget_usd = 10.0
    run.cfg.limits.role_budget_usd["reverse"] = 8.0
    run._committed[1] = 8.0
    assert run._task_budget("reverse", exclude=1) == pytest.approx(8.0)


def test_a_resumed_transcript_carries_its_spend_into_the_ceiling(run) -> None:
    """The CLI charges a resumed session from the transcript's running total."""
    run.cfg.limits.run_budget_usd = 30.0
    run.cfg.limits.role_budget_usd["reverse"] = 8.0
    assert run._task_budget("reverse", resume_spent=3.0) == pytest.approx(11.0)


def test_a_flash_ceiling_reserves_the_real_money_it_can_spend(run) -> None:
    """The ceiling is CLI dollars; the run budget is gateway dollars.

    Measured on this account: a flash session's CLI total runs 4.9x its real
    gateway cost, because the CLI bills cache-read at 0.2 where the gateway
    charges 0.02 and a flash session is 91% cache-read. Before the conversion,
    three parallel web workers on an $8 ceiling reserved $24 of a $30 run while
    being entitled to $1.6 each.
    """
    run.cfg.limits.run_budget_usd = 30.0
    run.cfg.models.roles["web"] = "deepseek-v4-flash"
    run.cfg.models.cli_cost_factor["deepseek-v4-flash"] = 4.9
    run.cfg.limits.role_budget_usd["web"] = 8.0

    # The SDK still gets the configured CLI-dollar ceiling.
    assert run._task_budget("web") == pytest.approx(8.0)
    # The reservation is what that is worth in gateway money.
    ceiling, reservation = run._budget_parts("web")
    assert ceiling == pytest.approx(8.0)
    assert reservation == pytest.approx(8.0 / 4.9)


def test_the_run_budget_caps_the_reservation_not_the_cli_number(run) -> None:
    """A $4 gateway remainder buys $19.6 of CLI dollars at flash's factor, so a
    flash role is not cut to $4 of CLI ceiling by it."""
    run.cfg.limits.run_budget_usd = 4.0
    run.cfg.models.roles["web"] = "deepseek-v4-flash"
    run.cfg.models.cli_cost_factor["deepseek-v4-flash"] = 4.9
    run.cfg.limits.role_budget_usd["web"] = 8.0
    assert run._task_budget("web") == pytest.approx(8.0)

    run._committed[1] = 3.5
    # $0.50 of gateway money left is $2.45 of CLI dollars; the role ceiling no
    # longer binds, the run budget does.
    assert run._task_budget("web") == pytest.approx(0.5 * 4.9)


def test_an_unmeasured_model_is_assumed_one_to_one(run) -> None:
    """No factor is not a zero factor: without an entry the two units are used
    as if the CLI and the gateway agreed, which is what they did before the
    gateway models were introduced."""
    run.cfg.limits.run_budget_usd = 30.0
    run.cfg.limits.role_budget_usd["reverse"] = 8.0
    assert run.cfg.models.cli_factor("deepseek-v4-pro") == 1.0
    assert run._budget_parts("reverse")[1] == pytest.approx(8.0)


def test_the_soft_stop_stops_exploration_but_not_the_closing_roles(run) -> None:
    """A run that has a flag but no money to verify it has wasted all of it."""
    run._soft_stopped = True
    assert run.accepting_work is True
    assert run.accepting_exploration is False
    assert "verifier" in CLOSING_ROLES and "writer" in CLOSING_ROLES


async def test_dispatch_is_refused_for_a_new_direction_near_the_ceiling(run) -> None:
    run._soft_stopped = True
    out = await run.tools.dispatch_task(role="web", objective="try the login form")
    assert "error" in out
    assert "预算" in out["error"]


def test_a_resumed_worker_is_charged_the_delta_only(run) -> None:
    """``total_cost_usd`` is cumulative, so re-billing it double-counts."""
    run._worker_base[7] = 5.0  # this session inherited a $5 transcript
    run._record_event(AgentEvent("result", {"subtype": "success"}, cost_usd=5.4), 7, "reverse")
    assert run.cost_usd == pytest.approx(0.4)

    # The next result is cumulative again: 5.4 + 0.6 = 6.0.
    run._record_event(AgentEvent("result", {"subtype": "success"}, cost_usd=6.0), 7, "reverse")
    assert run.cost_usd == pytest.approx(1.0)


def test_the_budget_block_names_the_ceiling_and_the_hand_off() -> None:
    block = budget_block(250, 8.0)
    assert "250 轮" in block
    assert "$8.00" in block
    assert "task_complete" in block
    assert "15 轮" in block


def test_a_flash_worker_is_told_what_its_ceiling_really_buys() -> None:
    """The SDK stops a flash worker at $1.63 of real spend, not $8.

    The number in the prompt is what the worker plans against. Shown only the
    CLI's figure, a web worker budgets $8 of work into a ceiling worth $1.63 —
    and since the CLI cuts it off at that point, the plan is wrong from the
    first turn.
    """
    block = budget_block(150, 8.0, real_usd=8.0 / 4.9)
    assert "$8.00" in block
    assert "$1.63" in block
    # A model priced the same by both accountants gets no caveat.
    assert "折合" not in budget_block(150, 8.0, real_usd=8.0)


def test_a_resumed_worker_is_told_it_is_continuing() -> None:
    assert "接着上一段 transcript" in budget_block(8, 0.75, resumed=True)
    assert "接着上一段 transcript" not in budget_block(8, 0.75)


# --------------------------------------------------------------------------
# Per-role model assignment.
#
# The platform ran every role on deepseek-v4-flash because config.toml said the
# gateway had nothing else. It has 37 models, and six of them answer on the
# Anthropic wire format. Reversing a binary is reasoning-bound; writing the
# report is not, and pro costs about twice the latency per turn.
# --------------------------------------------------------------------------


def test_role_models_fall_back_to_the_family_default() -> None:
    from ctfagent.config import ModelConfig

    models = ModelConfig(worker="w", verifier="v", writer="t")
    assert models.model_for("web") == "w"
    assert models.model_for("verifier") == "v"
    assert models.model_for("writer") == "t"


def test_a_role_override_wins() -> None:
    from ctfagent.config import ModelConfig

    models = ModelConfig(worker="flash", verifier="v", writer="t", roles={"reverse": "pro"})
    assert models.model_for("reverse") == "pro"
    assert models.model_for("web") == "flash"


def test_roles_load_from_the_models_section(tmp_path: Path) -> None:
    from ctfagent.config import _from_toml

    path = tmp_path / "config.toml"
    path.write_text(
        '[models]\nworker = "flash"\n[models.roles]\nreverse = "pro"\n', encoding="utf-8"
    )
    cfg = _from_toml(path)
    assert cfg.models.model_for("reverse") == "pro"
    assert cfg.models.model_for("misc") == "flash"



# --------------------------------------------------------------------- pricing
# The CLI's ``total_cost_usd`` is an Anthropic price-table lookup by model
# name: fiction for a gateway model, and the number the budget watchdog was
# calibrated on. With ``[models.pricing]`` filled in, cost is computed from the
# token usage every result event already carries.


def _result(usage: dict, cli_cost: float = 0.0) -> AgentEvent:
    return AgentEvent("result", {"usage": usage}, cost_usd=cli_cost)


RATES = {"input": 1.0, "cache_read": 0.1, "output": 2.0}


def test_a_priced_model_books_cost_from_usage_not_the_cli_number(run) -> None:
    """1M input + 2M cache-read + 0.5M output at the table's rates is $2.20."""
    task = run.store.create_task(run.id, "crypto", "sweep", "")
    run.cfg.models.pricing["claude-sonnet-5-5"] = dict(RATES)
    run._record_event(
        _result(
            {"input_tokens": 1_000_000, "cache_read_input_tokens": 2_000_000, "output_tokens": 500_000},
            cli_cost=99.0,
        ),
        task.id,
        "crypto",
    )
    assert run.cost_usd == pytest.approx(2.20)
    assert run.store.get_task_cost(task.id) == pytest.approx(2.20)


def test_usage_is_per_call_so_successive_results_add_up(run) -> None:
    """``usage`` is this call's tokens, not the session's running total.

    The CLI's ``total_cost_usd`` *is* cumulative, which is why the two used to
    be treated the same way. They are not: run 9's four lead results reported
    22,773 / 22,739 / 7,439 / 1,727 input tokens (not monotonic), and their sum
    equalled the session's final ``cost-state`` to the token.
    """
    task = run.store.create_task(run.id, "crypto", "sweep", "")
    run.cfg.models.pricing["claude-sonnet-5-5"] = dict(RATES)
    run._record_event(_result({"input_tokens": 1_000_000, "output_tokens": 0}), task.id, "crypto")
    run._record_event(_result({"input_tokens": 1_000_000, "output_tokens": 1_000_000}), task.id, "crypto")
    # 2M input at $1/M plus 1M output at $2/M
    assert run.cost_usd == pytest.approx(4.0)


def test_a_turn_that_reads_back_less_is_not_dropped(run) -> None:
    """The old delta rule booked ``max(0, this - previous)``, so a shorter turn
    after a long one was billed as nothing — most of a multi-turn lead's spend.

    Run 24: six lead results, the delta rule booked $0.50 of the $1.15 the
    tokens actually cost.
    """
    task = run.store.create_task(run.id, "crypto", "sweep", "")
    run.cfg.models.pricing["claude-sonnet-5-5"] = dict(RATES)
    run._record_event(_result({"input_tokens": 2_000_000}), task.id, "crypto")
    run._record_event(_result({"input_tokens": 100_000}), task.id, "crypto")
    assert run.cost_usd == pytest.approx(2.1)


def test_an_unpriced_model_keeps_the_cli_number(run) -> None:
    """No table, no usage-based booking: the CLI figure still flows through."""
    task = run.store.create_task(run.id, "crypto", "sweep", "")
    run._record_event(
        AgentEvent("result", {"usage": {"input_tokens": 900_000}}, cost_usd=4.0),
        task.id,
        "crypto",
    )
    assert run.cost_usd == pytest.approx(4.0)


def test_a_resumed_session_reports_only_its_own_tokens(run) -> None:
    """A resume does not re-report the transcript it inherited, so nothing needs
    subtracting.

    Task 137 hit its budget at 887,940 input tokens; task 140 resumed the same
    session and reported 659,955. Their sum, 1,547,895, is what the session's
    final ``cost-state`` held to within one snapshot. A baseline here would have
    silently dropped the second half.
    """
    task = run.store.create_task(run.id, "crypto", "sweep", "")
    run.cfg.models.pricing["claude-sonnet-5-5"] = dict(RATES)
    run.store.add_event(
        run.id,
        "agent.event",
        {"kind": "result", "usage": {"input_tokens": 3_000_000, "output_tokens": 400_000}},
        task_id=task.id,
    )
    run._record_event(
        _result({"input_tokens": 3_000_000, "output_tokens": 600_000}, cli_cost=123.0),
        task.id,
        "crypto",
    )
    # the resumed call's own tokens: 3M input at $1/M + 600k output at $2/M
    assert run.cost_usd == pytest.approx(4.2)


def test_the_lead_session_is_priced_on_its_own_model(run) -> None:
    """The lead runs on ``models.main``, not on the worker default."""
    run.cfg.models.pricing["claude-opus-4-8"] = dict(RATES)
    run._record_event(_result({"input_tokens": 1_000_000, "output_tokens": 1_000_000}), None, "main")
    assert run.cost_usd == pytest.approx(3.0)


def test_an_attached_run_keeps_its_booked_cost_and_adds_only_new_spend(run) -> None:
    """A restarted service re-attaches the lead's transcript without a token
    baseline: the events already in the log are already booked in ``cost_usd``,
    and the next result carries only its own tokens."""
    run.store.set_main_session(run.id, "sess-1", 0.0)
    run.store.add_run_cost(run.id, 1.25)
    run.store.add_event(
        run.id,
        "agent.event",
        {"role": "main", "kind": "result", "usage": {"input_tokens": 2_000_000, "output_tokens": 1_000}},
        task_id=None,
    )
    run.cfg.models.pricing["claude-opus-4-8"] = dict(RATES)
    manager = RunManager(
        run.store, EventBus(run.store), run.cfg, ScriptedBackend(),
        SandboxStatus("none", False, "test"), run.cfg.skills_dir,
    )
    attached = manager.attach(run.id)
    assert attached is not None
    assert attached.cost_usd == pytest.approx(1.25)
    attached._record_event(_result({"input_tokens": 1_000_000}), None, "main")
    assert attached.cost_usd == pytest.approx(2.25)


def test_pricing_reads_from_the_models_section(tmp_path: Path) -> None:
    from ctfagent.config import _from_toml

    path = tmp_path / "config.toml"
    path.write_text(
        '[models.pricing]\n"deepseek-v4-flash" = { input = 0.27, cache_read = 0.07, output = 1.10 }\n',
        encoding="utf-8",
    )
    cfg = _from_toml(path)
    assert cfg.models.pricing["deepseek-v4-flash"]["output"] == pytest.approx(1.10)
