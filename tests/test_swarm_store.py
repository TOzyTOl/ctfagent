"""蜂群接线（A 阶段）的存储层测试：黑板表的读写、认领、租约、绑定。

假设表的 ``run_id`` 是普通整数列（不挂外键）：它是"哪个 run 在思考"，而工单
表才需要真实的外键目标。所以这里大部分用例直接用 RID=1，只有涉及 tasks 的
用例才建真实的 challenge/run。
"""

from __future__ import annotations

import pytest

from ctfagent import swarm
from ctfagent.db import Store

RID = 1


@pytest.fixture()
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


def make(family="web", statement="登录页存在注入", **kw) -> swarm.Hypothesis:
    kw.setdefault("criterion", "引号报错或布尔条件改变返回内容")
    kw.setdefault("evidence", "登录参数 id 直接拼进查询")
    return swarm.Hypothesis(family=family, statement=statement, **kw)


def real_run(store: Store) -> int:
    from ctfagent.models import Challenge

    challenge_id = store.add_challenge(
        Challenge(id=0, name="t", category="web", description="")
    )
    return store.create_run(challenge_id, objective="t").id


class TestSchema:
    def test_hypotheses_table_exists(self, store):
        row = store._row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hypotheses'"
        )
        assert row is not None

    def test_tasks_gained_hypothesis_column(self, store):
        cols = {r["name"] for r in store._rows("PRAGMA table_info(tasks)")}
        assert "hypothesis_id" in cols

    def test_old_task_rows_still_read(self, store):
        """A task without a hypothesis is the centrally dispatched shape."""
        task = store.create_task(real_run(store), "web", "看这个", "")
        assert store.get_task(task.id).hypothesis_id is None


class TestPosting:
    def test_post_then_read(self, store):
        row, created = store.add_hypothesis(RID, make())
        assert created and row.id > 0 and row.status == swarm.OPEN
        assert store.get_hypothesis(row.id).statement == "登录页存在注入"

    def test_reworded_post_merges(self, store):
        first, _ = store.add_hypothesis(RID, make())
        second, created = store.add_hypothesis(RID, make(statement="登录页 存在注入"))
        assert not created and second.id == first.id
        assert len(store.list_hypotheses(RID)) == 1

    def test_invalid_post_is_rejected(self, store):
        with pytest.raises(ValueError):
            store.add_hypothesis(RID, make(criterion=""))

    def test_merge_keeps_refute_history(self, store):
        row, _ = store.add_hypothesis(RID, make())
        store.bump_hypothesis(row.id, refutes=2)
        store.add_hypothesis(RID, make(statement="登录页存在注入!"))
        assert store.get_hypothesis(row.id).refutes == 2


class TestClaim:
    def test_claim_leases_to_the_task(self, store):
        row, _ = store.add_hypothesis(RID, make())
        got = store.claim_hypothesis(RID, role="web", task_id=11, now=100.0, ttl_s=50.0)
        assert got.id == row.id and got.claimed_by == 11
        assert got.lease_until == pytest.approx(150.0)

    def test_second_claim_finds_nothing(self, store):
        store.add_hypothesis(RID, make())
        store.claim_hypothesis(RID, role="web", task_id=11, now=100.0, ttl_s=50.0)
        assert store.claim_hypothesis(RID, role="web", task_id=12, now=100.0) is None

    def test_expiry_returns_it_to_the_board(self, store):
        row, _ = store.add_hypothesis(RID, make())
        store.claim_hypothesis(RID, role="web", task_id=11, now=100.0, ttl_s=50.0)
        assert store.expire_hypotheses(200.0) == [row.id]
        assert store.get_hypothesis(row.id).status == swarm.OPEN

    def test_renew_extends(self, store):
        row, _ = store.add_hypothesis(RID, make())
        store.claim_hypothesis(RID, role="web", task_id=11, now=100.0, ttl_s=50.0)
        store.renew_hypothesis(row.id, 140.0, ttl_s=50.0)
        assert store.expire_hypotheses(160.0) == []

    def test_explicit_lease_is_idempotent_for_the_same_task(self, store):
        row, _ = store.add_hypothesis(RID, make())
        assert store.lease_hypothesis(row.id, 11, 100.0)
        assert store.lease_hypothesis(row.id, 11, 120.0)

    def test_explicit_lease_refuses_a_foreign_task(self, store):
        row, _ = store.add_hypothesis(RID, make())
        store.lease_hypothesis(row.id, 11, 100.0)
        assert not store.lease_hypothesis(row.id, 12, 100.0)

    def test_matching_family_is_preferred(self, store):
        store.add_hypothesis(RID, make(family="crypto", statement="误差可做格归约"))
        web, _ = store.add_hypothesis(RID, make())
        got = store.claim_hypothesis(
            RID, role="web", task_id=11, now=0.0, families={"web": "web"}
        )
        assert got.id == web.id

    def test_foreign_family_is_not_claimed(self, store):
        store.add_hypothesis(RID, make(family="crypto", statement="误差可做格归约"))
        got = store.claim_hypothesis(
            RID, role="web", task_id=11, now=0.0, families={"web": "web"}
        )
        assert got is None


class TestDescribe:
    def test_carries_claim_and_criterion(self):
        text = swarm.describe(make())
        assert "登录页存在注入" in text
        assert "证伪判据" in text and "引号报错" in text

    def test_mentions_prior_refutations(self):
        assert "已被证伪 2 次" in swarm.describe(make(refutes=2))


class TestParallelism:
    """run 级并行度：worker 秒数 / 墙钟，以及峰值并发。"""

    def _task(self, store, run_id, start, end, role="web"):
        task = store.create_task(run_id, role, "x", "")
        store._exec(
            "UPDATE tasks SET started_at=?, finished_at=? WHERE id=?",
            (start, end, task.id),
        )
        return task.id

    def test_serial_run_has_ratio_below_one_and_peak_one(self, store):
        rid = real_run(store)
        self._task(store, rid, "2026-11-01 10:00:00", "2026-11-01 10:10:00")
        self._task(store, rid, "2026-11-01 10:10:00", "2026-11-01 10:20:00")
        total, count, peak = store.task_seconds(rid)
        assert count == 2 and peak == 1
        assert total == pytest.approx(1200.0)

    def test_overlapping_tasks_raise_the_peak(self, store):
        rid = real_run(store)
        self._task(store, rid, "2026-11-01 10:00:00", "2026-11-01 10:10:00")
        self._task(store, rid, "2026-11-01 10:05:00", "2026-11-01 10:15:00")
        _total, _count, peak = store.task_seconds(rid)
        assert peak == 2

    def test_unfinished_tasks_are_ignored(self, store):
        rid = real_run(store)
        task = store.create_task(rid, "web", "x", "")
        store._exec(
            "UPDATE tasks SET started_at=? WHERE id=?", ("2026-11-01 10:00:00", task.id)
        )
        total, count, peak = store.task_seconds(rid)
        assert (total, count, peak) == (0.0, 0, 0)

    def test_empty_run_is_zero_not_an_error(self, store):
        assert store.task_seconds(real_run(store)) == (0.0, 0, 0)
