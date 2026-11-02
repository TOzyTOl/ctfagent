"""蜂群核心的纯函数测试：不启动 run，不调用模型。"""

from __future__ import annotations

import pytest

from ctfagent.swarm import (
    ABANDONED,
    SETTLE_ABANDONED,
    SETTLE_DONE,
    settle_for,
    CLAIMED,
    CLOSING_RESERVE_FRACTION,
    OPEN,
    REFUTED,
    SUPPORTED,
    Budget,
    Hypothesis,
    claim,
    convergence,
    dedupe,
    is_expired,
    normalize,
    post,
    reclaim,
    renew,
)


def hyp(family="web", statement="登录页存在注入", **kw) -> Hypothesis:
    kw.setdefault("criterion", "引号报错或布尔条件改变返回")
    kw.setdefault("evidence", "登录参数 id 直接拼接进查询")
    return Hypothesis(family=family, statement=statement, **kw)


class TestPosting:
    def test_criterion_is_required(self):
        with pytest.raises(ValueError):
            post(hyp(criterion=""))

    def test_evidence_is_required(self):
        with pytest.raises(ValueError):
            post(hyp(evidence=""))

    def test_family_is_explicit_not_inferred(self):
        with pytest.raises(ValueError):
            post(hyp(family=""))

    def test_valid_hypothesis_posts(self):
        post(hyp())


class TestDedupe:
    def test_reworded_hypotheses_merge(self):
        a = hyp(statement="登录页 存在 SQLi")
        b = hyp(statement="登录页存在 sqli")
        board, dropped = dedupe([a, b])
        assert len(board) == 1 and len(dropped) == 1

    def test_different_families_never_merge(self):
        board, _ = dedupe([hyp(family="web"), hyp(family="crypto")])
        assert len(board) == 2

    def test_history_survives_merge(self):
        board, _ = dedupe([hyp(refutes=2), hyp(priority=9)])
        assert board[0].refutes == 2 and board[0].priority == 9

    def test_missing_evidence_is_filled_in(self):
        board, _ = dedupe([hyp(evidence="凭感觉"), hyp()])
        assert "登录参数" in board[0].evidence


class TestNormalize:
    def test_punctuation_and_case_collapse(self):
        assert normalize("A, B  C") == "abc"
        assert normalize("  登录页  ") == "登录页"

    def test_chinese_spacing_does_not_split_a_key(self):
        assert normalize("登录页 存在 SQLi") == normalize("登录页存在 sqli")


class TestClaim:
    def test_claim_leases_and_marks_owner(self):
        h = hyp()
        got = claim([h], role="web", task_id=7, now=100.0)
        assert got is h and h.status == CLAIMED
        assert h.claimed_by == 7 and h.lease_until > 100.0

    def test_matching_family_wins(self):
        web = hyp(family="web")
        other = hyp(family="crypto", statement="样本可做格归约")
        got = claim([web, other], role="web", task_id=7, now=0.0, families={"web": "web"})
        assert got is web

    def test_direction_worker_never_takes_another_family(self):
        """A web worker with only a crypto entry left takes nothing.

        The old rule merely *preferred* a match, so the last worker alive in a
        run could be handed a direction it has neither the skill nor the tools
        for. An empty answer is the honest one: the board stays postable and
        the coordinator starts the right specialist (or nobody).
        """
        other = hyp(family="crypto", statement="样本可做格归约")
        assert claim([other], role="web", task_id=7, now=0.0, families={"web": "web"}) is None

    def test_triage_is_general_and_takes_any_family(self):
        other = hyp(family="crypto", statement="样本可做格归约")
        assert claim([other], role="triage", task_id=7, now=0.0) is other

    def test_claimed_item_cannot_be_claimed_twice(self):
        h = hyp()
        claim([h], role="web", task_id=7, now=0.0)
        assert claim([h], role="web", task_id=8, now=0.0) is None

    def test_refuted_direction_is_penalised(self):
        dead = hyp(statement="A", refutes=2)
        alive = hyp(statement="B")
        got = claim([dead, alive], role="web", task_id=7, now=0.0)
        assert got is alive

    def test_terminal_states_are_not_claimable(self):
        assert claim([hyp(status=SUPPORTED)], role="web", task_id=1, now=0.0) is None
        assert claim([hyp(status=REFUTED)], role="web", task_id=1, now=0.0) is None
        assert claim([hyp(status=ABANDONED)], role="web", task_id=1, now=0.0) is None

    def test_empty_board_returns_none(self):
        assert claim([], role="web", task_id=1, now=0.0) is None


class TestLease:
    def test_expiry_and_reclaim(self):
        h = hyp()
        claim([h], role="web", task_id=7, now=0.0, ttl_s=100.0)
        assert not is_expired(h, 50.0)
        assert is_expired(h, 150.0)
        freed = reclaim([h], 150.0)
        assert freed == [h] and h.status == OPEN and h.claimed_by is None

    def test_renew_extends_the_lease(self):
        h = hyp()
        claim([h], role="web", task_id=7, now=0.0, ttl_s=100.0)
        renew(h, 90.0, ttl_s=100.0)
        assert not is_expired(h, 150.0)


class TestBudget:
    def test_free_pool_excludes_closing_reserve(self):
        b = Budget(ceiling=100.0)
        assert b.free() == pytest.approx(100.0 * (1 - CLOSING_RESERVE_FRACTION))

    def test_reserve_and_settle_returns_residue(self):
        b = Budget(ceiling=100.0)
        assert b.reserve(1, 20.0)
        residue = b.settle(1, 8.0)
        assert residue == pytest.approx(12.0)
        assert b.spent == pytest.approx(8.0)
        assert b.reserved == 0.0

    def test_pool_refuses_overcommit(self):
        b = Budget(ceiling=100.0)
        assert not b.reserve(1, 90.0)

    def test_closing_role_may_use_the_reserve(self):
        b = Budget(ceiling=100.0)
        b.reserve(1, 80.0)
        assert b.reserve(2, 10.0, closing=True)

    def test_closing_role_still_cannot_exceed_ceiling(self):
        b = Budget(ceiling=100.0)
        b.reserve(1, 80.0)
        assert not b.reserve(2, 30.0, closing=True)

    def test_zero_amount_is_free(self):
        assert Budget(ceiling=10.0).reserve(1, 0.0)


class TestConvergence:
    def test_working_while_anything_is_claimed(self):
        h = hyp()
        claim([h], role="web", task_id=1, now=0.0)
        assert convergence([h]) == "working"

    def test_postable_when_open_items_remain(self):
        assert convergence([hyp()]) == "postable"

    def test_flag_parks_the_run(self):
        assert convergence([hyp(status=SUPPORTED)], has_candidate_flag=True) == "needs_input"

    def test_empty_board_is_idle_not_more_work(self):
        assert convergence([hyp(status=REFUTED)]) == "idle"


class TestSettlement:
    """``settle_for`` is the default, and its two refusals are the point.

    ``_settle_hypothesis`` lets what the worker wrote outrank this, so the only
    cases that reach it are the ones with nothing to read: a status that says
    "stopped" and a ``done`` with an empty board behind it.
    """

    def test_a_ceiling_or_crash_returns_the_entry(self):
        for status in ("exhausted", "stopped", "failed"):
            assert settle_for(status) == SETTLE_ABANDONED

    def test_done_with_evidence_supports(self):
        assert settle_for("done", evidence=True) == SETTLE_DONE

    def test_done_without_evidence_does_not_close_the_direction(self):
        # task_complete accepts only done/blocked, so "I gave up" arrives as
        # done. Counting that as a conclusion retires a route nobody disproved.
        assert settle_for("done", evidence=False) == SETTLE_ABANDONED
