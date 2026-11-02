"""巡检哨兵的纯函数测试：不启动 run，不调用模型。"""

from __future__ import annotations

from ctfagent.patrol import (
    MAX_PATROLS,
    PATROL_TOOLS,
    REPEAT_DIRECTION,
    SLOW,
    STALLED,
    UNWORKED,
    PatrolState,
    TaskTrace,
    build_prompt,
    category_baseline,
    evaluate,
    is_patrol,
    mark,
    parse_verdict,
    percentile,
    stalled_window,
)


def trace(task_id: int, role: str = "web", **kw) -> TaskTrace:
    return TaskTrace(task_id=task_id, role=role, status=kw.pop("status", "done"), **kw)


class TestScore:
    def test_facts_weigh_more_than_outputs(self):
        a = trace(1, facts_added=1, new_outputs=0)
        b = trace(2, facts_added=0, new_outputs=2)
        assert a.score == 2 and b.score == 2

    def test_empty_task_did_not_move(self):
        assert trace(1).moved is False
        assert trace(2, facts_added=1).moved is True


class TestStalledWindow:
    def test_three_quiet_tasks_trip(self):
        traces = [trace(1, facts_added=2), trace(2), trace(3), trace(4)]
        assert [t.task_id for t in stalled_window(traces)] == [2, 3, 4]

    def test_a_moving_task_resets_the_tail(self):
        traces = [trace(1), trace(2), trace(3, facts_added=1), trace(4)]
        assert stalled_window(traces) == []


class TestEvaluate:
    def test_stalled_beats_everything(self):
        traces = [trace(1), trace(2), trace(3)]
        trig = evaluate(traces=traces)
        assert trig is not None and trig.reason == STALLED
        assert trig.task_ids == (1, 2, 3)

    def test_candidate_flag_suppresses_patrol(self):
        assert evaluate(traces=[trace(1), trace(2), trace(3)], has_candidate_flag=True) is None

    def test_budget_is_enforced(self):
        state = PatrolState(dispatched=MAX_PATROLS)
        assert evaluate(traces=[trace(1), trace(2), trace(3)], state=state) is None

    def test_covered_tasks_do_not_retrigger(self):
        state = PatrolState(covered={1, 2, 3})
        assert evaluate(traces=[trace(1), trace(2), trace(3)], state=state) is None

    def test_slow_task_triggers_when_above_baseline(self):
        trig = evaluate(
            traces=[trace(1, facts_added=1)],
            live=[(9, "reverse", 3600.0)],
            baseline_s=600.0,
        )
        assert trig is not None and trig.reason == SLOW and trig.task_ids == (9,)

    def test_normal_duration_does_not_trigger(self):
        trig = evaluate(
            traces=[trace(1, facts_added=1)],
            live=[(9, "reverse", 700.0)],
            baseline_s=600.0,
        )
        assert trig is None

    def test_failed_direction_dispatched_again(self):
        traces = [
            trace(1, role="pwn", status="failed"),
            trace(2, role="pwn", status="done"),
        ]
        trig = evaluate(traces=traces)
        assert trig is not None and trig.reason == REPEAT_DIRECTION
        assert trig.task_ids == (1, 2)

    def test_productive_direction_is_not_repeat(self):
        traces = [
            trace(1, role="pwn", status="failed", facts_added=1),
            trace(2, role="pwn", status="done"),
        ]
        assert evaluate(traces=traces) is None


class TestUnworkedHypotheses:
    """The board's own failure mode: a direction nothing will start.

    The patrol used to look only at finished tasks, so this state — the one
    board mode can create and star mode cannot — had no trigger and no checker.
    """

    def test_a_fresh_open_entry_does_not_trigger(self):
        assert evaluate(traces=[trace(1, facts_added=1)],
                        unworked=((7, "crypto", 30.0, "模数可分解"),)) is None

    def test_an_old_open_entry_triggers(self):
        trig = evaluate(
            traces=[trace(1, facts_added=1)],
            unworked=((7, "crypto", 1200.0, "模数可分解"),),
        )
        assert trig is not None and trig.reason == UNWORKED
        assert trig.task_ids == (7,)

    def test_a_stalled_tail_outranks_an_unworked_entry(self):
        """Measured work that produced nothing is more urgent than a queue."""
        trig = evaluate(
            traces=[trace(1), trace(2), trace(3)],
            unworked=((7, "crypto", 1200.0, "模数可分解"),),
        )
        assert trig is not None and trig.reason == STALLED

    def test_an_unworked_entry_outranks_a_slow_task(self):
        """A slow worker is at least doing the work; an open entry is not."""
        trig = evaluate(
            traces=[trace(1, facts_added=1)],
            live=[(9, "reverse", 3600.0)],
            baseline_s=600.0,
            unworked=((7, "crypto", 1200.0, "模数可分解"),),
        )
        assert trig is not None and trig.reason == UNWORKED
        assert trig.task_ids == (7,)

    def test_the_oldest_entry_is_reported(self):
        trig = evaluate(
            traces=[trace(1, facts_added=1)],
            unworked=(
                (7, "crypto", 700.0, "旧的"),
                (8, "web", 2400.0, "更旧的"),
            ),
        )
        assert trig is not None and trig.task_ids == (8,)
        assert "更旧的" in trig.detail

    def test_one_patrol_per_entry(self):
        """Covered by hypothesis id, so the same entry cannot trigger twice."""
        state = PatrolState(covered_hypotheses={7})
        assert evaluate(
            traces=[trace(1, facts_added=1)],
            unworked=((7, "crypto", 1200.0, "模数可分解"),),
            state=state,
        ) is None

    def test_hypothesis_ids_do_not_cover_tasks(self):
        """Two id namespaces. A covered task must not hide a stuck entry."""
        state = PatrolState(covered={7})
        trig = evaluate(
            traces=[trace(1, facts_added=1)],
            unworked=((7, "crypto", 1200.0, "模数可分解"),),
            state=state,
        )
        assert trig is not None and trig.reason == UNWORKED


class TestBaseline:
    def test_percentile_nearest_rank(self):
        assert percentile([1, 2, 3, 4, 5], 90) == 5
        assert percentile([], 90) == 0.0

    def test_short_history_falls_back(self):
        assert category_baseline([100.0, 200.0], 42.0) == 42.0

    def test_long_history_uses_p90(self):
        values = [float(v) for v in range(1, 101)]
        assert category_baseline(values, 0.0) == 90.0


class TestVerdict:
    def test_structured_lines(self):
        assert parse_verdict("CONTINUE 方向没问题")[0] == "CONTINUE"
        assert parse_verdict("`REPIVOT` 应该改试 CRC")[0] == "REPIVOT"
        action, tid, reason = parse_verdict("STOP 12 这个工单在重跑")
        assert (action, tid) == ("STOP", 12)
        assert "重跑" in reason

    def test_stop_without_id_degrades_to_continue(self):
        assert parse_verdict("STOP 该停了")[0] == "CONTINUE"

    def test_unparseable_reply_is_continue(self):
        assert parse_verdict("我觉得可以再看看")[0] == "CONTINUE"
        assert parse_verdict("")[0] == "CONTINUE"


class TestPrompt:
    def test_prompt_carries_reason_and_digest(self):
        trig = evaluate(traces=[trace(1), trace(2), trace(3)])
        assert trig is not None
        text = build_prompt(trig, "## 黑板记录\n- #1 观察到 p=q", [trace(1), trace(2), trace(3)])
        assert STALLED in text
        assert "p=q" in text
        assert "CONTINUE" in text and "REPIVOT" in text

    def test_empty_digest_is_explicit(self):
        trig = evaluate(traces=[trace(1), trace(2), trace(3)])
        assert trig is not None
        assert "（空）" in build_prompt(trig, "", [])


class TestClosingRoles:
    def test_verifier_and_writer_do_not_count_as_stall(self):
        # 两个安静的 worker 不足以判定停滞；如果收尾角色被算进去就会误报。
        traces = [trace(1), trace(2), trace(3, role="verifier"), trace(4, role="writer")]
        assert stalled_window(traces) == []

    def test_real_stall_still_detected_after_closing_tasks(self):
        traces = [trace(1), trace(2), trace(3), trace(4, role="verifier")]
        assert [t.task_id for t in stalled_window(traces)] == [1, 2, 3]


class TestMarking:
    def test_marked_objective_is_recognized(self):
        assert is_patrol(mark("看看这个 run"))
        assert not is_patrol("看看这个 run")
        assert not is_patrol("")

    def test_mark_keeps_the_body(self):
        text = mark("触发原因：stalled_tasks")
        assert is_patrol(text)
        assert "stalled_tasks" in text


class TestWiring:
    """_score_progress is the only place the orchestration layer touches patrol.

    Driven through the unbound method with a stub: the real ``Run`` needs a
    workspace, a backend and a challenge, and none of those are what this
    asserts. What it asserts is that three quiet tasks produce three scores and
    one trigger, and that observe mode stops there.
    """

    def _stub(self, cfg, store):
        from ctfagent.orchestrator import Run

        events: list[tuple[str, dict]] = []

        class Stub:
            def __init__(self) -> None:
                self.cfg = cfg
                self.store = store
                self.id = 1
                self._traces = []
                self._outputs = {}
                self._patrol = PatrolState()
                self._patrol_tasks = set()
                self.accepting_work = True

            def _emit(self, kind, payload, task_id=None):
                events.append((kind, payload))

            def _sweep_hypotheses(self):
                # The real Run sweeps lapsed board leases here; the scoring path
                # must not depend on it succeeding.
                return None

            def _unworked_hypotheses(self):
                # Off-mode runs have no board; the scoring path must not depend
                # on the store being able to age rows either.
                return ()

        return Run, Stub(), events

    def test_quiet_tasks_are_scored_then_flagged(self, cfg, store_and_bus):
        store, _bus = store_and_bus
        Run, stub, events = self._stub(cfg, store)
        for offset in range(3):
            Run._score_progress(
                stub, task_id=100 + offset, role="web", status="done", result={"findings": []}
            )
        kinds = [kind for kind, _ in events]
        assert kinds.count("patrol.score") == 3
        assert "patrol.trigger" in kinds
        trigger = dict(events)["patrol.trigger"]
        assert trigger["reason"] == STALLED and trigger["mode"] == "observe"

    def test_productive_tasks_never_trigger(self, cfg, store_and_bus):
        store, _bus = store_and_bus
        Run, stub, events = self._stub(cfg, store)
        for offset in range(4):
            Run._score_progress(
                stub,
                task_id=200 + offset,
                role="web",
                status="done",
                result={"findings": ["找到密钥长度是 16"]},
            )
        assert "patrol.trigger" not in [kind for kind, _ in events]

    def test_off_mode_records_nothing(self, cfg, store_and_bus):
        store, _bus = store_and_bus
        cfg.limits.patrol = "off"
        Run, stub, events = self._stub(cfg, store)
        for offset in range(3):
            Run._score_progress(
                stub, task_id=300 + offset, role="web", status="done", result={"findings": []}
            )
        assert events == []

    def test_a_stuck_board_entry_becomes_a_patrol_target(self, cfg):
        """The end-to-end wiring of the last unimplemented trigger, board only.

        ``_score_progress`` asks the run for its open entries; a board mode run
        answering with one old entry must put ``unworked_hypothesis`` on the
        wire. This is the seam that used to be missing: the trigger existed in
        ``evaluate`` and nothing ever fed it.
        """
        from ctfagent.db import Store
        from ctfagent.challenge import Challenge
        from ctfagent.orchestrator import RunManager
        from ctfagent.backend import ScriptedBackend
        from ctfagent.events import EventBus
        from ctfagent.workspace import SandboxStatus
        from ctfagent import swarm
        from pathlib import Path

        cfg.limits.swarm = "board"
        store = Store(cfg.db_path)
        bus = EventBus(store)
        manager = RunManager(
            store, bus, cfg, ScriptedBackend(),
            SandboxStatus("none", False, "test"), cfg.skills_dir,
        )
        challenge = Challenge(id=0, name="c", description="", category="crypto")
        run = manager.prepare(store.add_challenge(challenge))
        row, _ = store.add_hypothesis(
            run.id,
            swarm.Hypothesis(
                family="crypto",
                statement="模数可分解",
                criterion="分解成功",
                evidence="n 很小",
            ),
        )
        # Backdate past the ten-minute line.
        store._conn.execute(
            "UPDATE hypotheses SET created_at=datetime('now','-20 minutes') WHERE id=?",
            (row.id,),
        )
        store._conn.commit()
        assert [h[0] for h in run._unworked_hypotheses()] == [row.id]

        events: list[tuple[str, dict]] = []
        run._emit = lambda kind, payload, task_id=None: events.append((kind, payload))
        run._traces = []
        run._outputs = {}
        run._score_progress(
            task_id=901, role="crypto", status="done", result={"findings": ["有进展"]}
        )
        trigger = dict(events).get("patrol.trigger")
        assert trigger is not None and trigger["reason"] == UNWORKED
        assert trigger["task_ids"] == [row.id]
        # A trigger is only a proposal: ``mode="observe"`` (the default) spends
        # nothing and dispatches nothing, and "covered" exists to stop a second
        # patrol for the same target — which never starts in this mode.
        assert trigger["mode"] == "observe"
        assert run._patrol.dispatched == 0
        store.close()

    def test_a_patrol_close_abandons_the_entry_it_named(self, cfg):
        """The verdict path end to end, without a session.

        ``_finish_task`` is where a patrol's text becomes an action. The failure
        this pins: ``CLOSE`` parsed correctly but nothing acted on it, so the
        only way out of a stuck board entry was the lead's next turn.
        """
        from ctfagent.db import Store
        from ctfagent.challenge import Challenge
        from ctfagent.orchestrator import RunManager
        from ctfagent.backend import ScriptedBackend
        from ctfagent.events import EventBus
        from ctfagent.workspace import SandboxStatus
        from ctfagent import swarm

        cfg.limits.swarm = "board"
        store = Store(cfg.db_path)
        bus = EventBus(store)
        manager = RunManager(
            store, bus, cfg, ScriptedBackend(),
            SandboxStatus("none", False, "test"), cfg.skills_dir,
        )
        challenge = Challenge(id=0, name="c", description="", category="crypto")
        run = manager.prepare(store.add_challenge(challenge))
        row, _ = store.add_hypothesis(
            run.id,
            swarm.Hypothesis(
                family="crypto",
                statement="模数可分解",
                criterion="分解成功",
                evidence="n 很小",
            ),
        )
        events: list[tuple[str, dict]] = []
        run._emit = lambda kind, payload, task_id=None: events.append((kind, payload))

        patrol_task = store.create_task(run.id, "triage", "[巡检]\n体检", "")
        run._patrol_tasks.add(patrol_task.id)
        # The verdict arrives as the task's last text (that is where the CLI
        # report lands), not as a structured field.
        run._finish_task(
            patrol_task.id, "triage", "done", "", {},
            f"CLOSE {row.id} 该方向不可证伪",
        )
        assert store.get_hypothesis(row.id).status == swarm.ABANDONED
        kinds = [kind for kind, _ in events]
        assert "hypothesis.abandoned" in kinds
        assert dict(events)["patrol.verdict"]["refused"] == ""
        store.close()

    def test_a_patrol_cannot_close_a_claimed_entry(self, cfg):
        """The guard is load-bearing: a live worker must keep its direction."""
        from ctfagent.db import Store
        from ctfagent.challenge import Challenge
        from ctfagent.orchestrator import RunManager
        from ctfagent.backend import ScriptedBackend
        from ctfagent.events import EventBus
        from ctfagent.workspace import SandboxStatus
        from ctfagent import swarm

        cfg.limits.swarm = "board"
        store = Store(cfg.db_path)
        bus = EventBus(store)
        manager = RunManager(
            store, bus, cfg, ScriptedBackend(),
            SandboxStatus("none", False, "test"), cfg.skills_dir,
        )
        challenge = Challenge(id=0, name="c", description="", category="crypto")
        run = manager.prepare(store.add_challenge(challenge))
        row, _ = store.add_hypothesis(
            run.id,
            swarm.Hypothesis(
                family="crypto",
                statement="模数可分解",
                criterion="分解成功",
                evidence="n 很小",
            ),
        )
        store.set_hypothesis_status(row.id, swarm.CLAIMED)
        events: list[tuple[str, dict]] = []
        run._emit = lambda kind, payload, task_id=None: events.append((kind, payload))
        patrol_task = store.create_task(run.id, "triage", "[巡检]\n体检", "")
        run._patrol_tasks.add(patrol_task.id)
        run._finish_task(
            patrol_task.id, "triage", "done", "", {},
            f"CLOSE {row.id} 换方向",
        )
        assert store.get_hypothesis(row.id).status == swarm.CLAIMED
        assert dict(events)["patrol.verdict"]["refused"]
        assert "hypothesis.abandoned" not in [kind for kind, _ in events]
        store.close()


class TestClose:
    """The one place a patrol may change the board, and its three guards.

    A patrol reads text and answers in one turn; its verdict is advisory
    everywhere else. ``CLOSE`` is deliberately narrow: abandoning a claimed
    entry would strand a worker, a foreign id is another run's board, and a
    closed entry already has a verdict that a second one would contradict.
    """

    def _board(self, store):
        from ctfagent import swarm
        from ctfagent.challenge import Challenge

        cfg_challenge = store.add_challenge(
            Challenge(id=0, name="x", description="", category="crypto")
        )
        run = store.create_run(cfg_challenge, "obj")
        row, _ = store.add_hypothesis(
            run.id,
            swarm.Hypothesis(
                family="crypto",
                statement="模数可分解",
                criterion="分解成功",
                evidence="n 很小",
            ),
        )
        return run.id, row

    def test_an_open_entry_is_abandoned_without_a_refutation(self, store_and_bus):
        from ctfagent import patrol, swarm

        store, _bus = store_and_bus
        run_id, row = self._board(store)
        assert patrol.apply_close(store, run_id, row.id) == ""
        after = store.get_hypothesis(row.id)
        assert after.status == swarm.ABANDONED
        # Abandoned, not refuted: no worker ever observed anything, and a
        # refutation count here would make the lead read "tried and disproven".
        assert after.refutes == 0

    def test_a_claimed_entry_is_refused(self, store_and_bus):
        from ctfagent import patrol, swarm

        store, _bus = store_and_bus
        run_id, row = self._board(store)
        store.set_hypothesis_status(row.id, swarm.CLAIMED)
        assert "open" in patrol.apply_close(store, run_id, row.id)
        assert store.get_hypothesis(row.id).status == swarm.CLAIMED

    def test_another_runs_entry_is_refused(self, store_and_bus):
        from ctfagent import patrol

        store, _bus = store_and_bus
        run_id, row = self._board(store)
        assert "不属于" in patrol.apply_close(store, run_id + 100, row.id)

    def test_a_missing_entry_is_refused(self, store_and_bus):
        from ctfagent import patrol

        store, _bus = store_and_bus
        run_id, _row = self._board(store)
        assert "不存在" in patrol.apply_close(store, run_id, 9999)

    def test_close_without_an_id_degrades_to_continue(self):
        assert parse_verdict("CLOSE 这条方向不可证伪")[0] == "CONTINUE"

    def test_close_with_an_id_keeps_it(self):
        action, target, reason = parse_verdict("CLOSE 7 同一个方向已经有人在做了")
        assert (action, target) == ("CLOSE", 7)
        assert "同一个方向" in reason


class TestPatrolTools:
    def test_no_shell_and_no_network(self):
        banned = {"Bash", "PowerShell", "WebFetch", "WebSearch", "Write", "Edit"}
        assert banned.isdisjoint(PATROL_TOOLS)

    def test_can_still_answer(self):
        assert "mcp__worker__task_complete" in PATROL_TOOLS
        assert "Read" in PATROL_TOOLS
