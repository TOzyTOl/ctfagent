"""The case library: experience that makes the next run cheaper.

Every test here names the live observation it protects. The library is a
memory, and the failure modes of a memory are specific: it can replay an old
answer, it can drown the prompt in irrelevant cards, and it can grow stale in
ways nobody notices until a run is already paying for it.
"""

from __future__ import annotations

from pathlib import Path

from ctfagent import caselib
from ctfagent.challenge import load_challenge
from ctfagent.db import Store
from ctfagent.models import Case, FlagStatus, TaskStatus

from test_run_quality import _manager, store_and_bus  # noqa: F401 - fixture reuse


class _Stub:
    """Just enough of a Run for ``distill``: an id, a store, a challenge."""

    def __init__(self, store: Store, run_id: int, challenge, cost: float = 0.0) -> None:
        self.store = store
        self.id = run_id
        self.challenge = challenge
        self.cost_usd = cost

    def open_unknowns(self):
        """The same closure rule ``Run.open_unknowns`` applies."""
        facts = self.store.list_facts(self.id, limit=200)
        closed = {f.resolves for f in facts if f.resolves}
        return [f for f in facts if f.kind == "unknown" and f.id not in closed]


def test_a_card_never_carries_a_flag_literal(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """A model shown an old flag will eventually submit it.

    This is the one failure mode a memory of past solves introduces, and it is
    unrecoverable once it happens: the value goes to the platform. The card
    keeps the derivation and replaces every flag-shaped string with ``<flag>``.
    """
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    run_id = store.create_run(challenge_id, "solve").id
    task = store.create_task(run_id, "reverse", "unpack", "")
    store.finish_task(task.id, TaskStatus.DONE, {"summary": "got the key"}, "")
    flag = store.add_flag(
        run_id, task.id, "flag{old_answer}", "out.bin",
        "decrypt(out.bin) with key = sha256(salt) printed flag{old_answer}",
        "high",
    )
    assert flag is not None
    store.set_flag_status(flag.id, FlagStatus.CONFIRMED)
    run = _Stub(store, run_id, store.get_challenge(challenge_id), cost=1.5)

    case = caselib.distill(run)
    assert case is not None
    blob = case.derivation + " " + " ".join(case.route) + " " + " ".join(case.dead_ends)
    assert "old_answer" not in blob
    assert "<flag>" in case.derivation
    assert case.outcome == "solved"

    store.upsert_case(case)
    stored = store.list_cases()[0]
    assert "old_answer" not in stored.derivation


def test_a_failed_run_without_dead_ends_teaches_nothing(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A card that says only "it did not work" costs tokens and says nothing."""
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    run_id = store.create_run(challenge_id, "solve").id
    task = store.create_task(run_id, "reverse", "unpack", "")
    store.finish_task(task.id, TaskStatus.EXHAUSTED, {"summary": "ran out"}, "")
    run = _Stub(store, run_id, store.get_challenge(challenge_id))
    assert caselib.distill(run) is None

    # The same run with a recorded dead end is worth keeping.
    store.add_fact(run_id, task.id, "dead_end", "静态 patch 校验分支无效，因为后续还有一次 MAC 比对", {}, "observed")
    case = caselib.distill(run)
    assert case is not None
    assert case.outcome == "failed"
    assert case.dead_ends and "MAC" in case.dead_ends[0]


def test_relevance_prefers_the_same_category_and_keeps_the_prompt_small(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Three cards, same category first.

    A run that injects every past challenge into every dispatch pays for the
    whole history on each turn. Observed shape: the lead was already spending
    83 minutes inside ``wait_tasks``; a prompt that grows per run makes each of
    those turns slower.
    """
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    for index in range(6):
        run_id = store.create_run(challenge_id, "solve").id
        store.upsert_case(
            Case(
                id=0, run_id=run_id, challenge_id=challenge_id,
                name=f"pcap {index}", category="forensics" if index < 3 else "web",
                outcome="solved", route=[f"forensics[done] parse beacon {index}"],
                derivation=f"decode beacon with scapy {index}", techniques=["pcap", "scapy"],
                cost_usd=1.0,
            )
        )
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = "forensics"
    challenge.name = "beacon capture"
    challenge.description = "parse a pcap"

    picked = caselib.relevant(store, challenge, limit=3)
    assert len(picked) == 3
    assert all(c.category == "forensics" for c in picked), [c.category for c in picked]

    block = caselib.render(picked)
    assert "战例库" in block
    assert block.count("## ") == 3


def test_one_coincidental_word_does_not_admit_an_unrelated_card(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A substring match is not a resemblance.

    Live symptom: on the forensics challenge, the reverse card's technique
    ``pie`` matched inside ``pier-beacon`` and a web card's ``http`` matched
    inside every URL, so both were admitted to a block the operator pays for on
    every dispatch. Relevance is now compared on whole tokens, and a card from
    another category needs real vocabulary overlap.
    """
    store, _ = store_and_bus
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = "forensics"
    challenge.name = "pier-beacon"
    challenge.description = "firmware bundle"
    challenge_id = store.add_challenge(challenge)
    other_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))

    reverse_run = store.create_run(other_id, "solve").id
    store.upsert_case(
        Case(
            id=0, run_id=reverse_run, challenge_id=other_id,
            name="ooops", category="reverse", outcome="failed",
            route=["reverse[exhausted] unpacked the bundle with pie"],
            techniques=["pie", "bundle"],
        )
    )
    picked = caselib.relevant(store, store.get_challenge(challenge_id), limit=3)
    assert not [c for c in picked if c.category == "reverse"]


def test_a_retry_of_the_same_challenge_reads_its_own_earlier_card(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The best card for challenge X is the one from the last attempt at X."""
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    other_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    same = store.create_run(challenge_id, "solve").id
    other = store.create_run(other_id, "solve").id
    store.upsert_case(
        Case(
            id=0, run_id=other, challenge_id=other_id,
            name="other", category="reverse", outcome="solved",
            route=["reverse[done] solved it another way"],
        )
    )
    store.upsert_case(
        Case(
            id=0, run_id=same, challenge_id=challenge_id,
            name="retry", category="reverse", outcome="failed",
            route=["reverse[exhausted] this exact artifact fights back"],
        )
    )
    picked = caselib.relevant(store, store.get_challenge(challenge_id), limit=1)
    assert [c.name for c in picked] == ["retry"]


async def test_a_finished_run_writes_its_card_and_the_next_run_reads_it(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """End to end: close a run, then start another of the same category.

    The card has to be written before ``run.finished`` is announced, because the
    operator's next action is usually to launch the next challenge.
    """
    store, bus = store_and_bus
    cfg.limits.workers_per_run = 1
    run = _manager(store, bus, cfg).prepare(
        store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    )
    task = store.create_task(run.id, "reverse", "unpack", "")
    store.finish_task(task.id, TaskStatus.EXHAUSTED, {"summary": "found the VM at 0x401000"}, "")
    store.add_fact(run.id, task.id, "dead_end", "直接 patch memcmp 无效", {}, "observed")

    await run.shutdown("failed", "时间用尽", "clock")
    cards = store.list_cases()
    assert len(cards) == 1
    assert cards[0].run_id == run.id
    kinds = [e.kind for e in store.events_since(run.id)]
    assert "cases.loaded" not in kinds  # nothing existed when this run started

    # A second run of the same category sees the first one's card.
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = cards[0].category
    second = _manager(store, bus, cfg).prepare(store.add_challenge(challenge))
    spec = second._main_spec()
    assert "战例库" in spec.system_prompt
    assert "直接 patch memcmp 无效" in spec.system_prompt
    assert store.get_case(cards[0].id).used_count >= 1


def test_a_card_keeps_the_questions_its_run_could_not_answer(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The open unknowns are the cheapest part of a failed run to reuse.

    Live observation (easyrs, run 6): both reverse workers recorded "what does
    GetTickCount return at runtime" and "is the 48-byte constant really the
    comparison target", then exhausted their budget. The run closed and the
    questions went with it, so a retry would have re-derived them from zero.
    """
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    run_id = store.create_run(challenge_id, "solve").id
    task = store.create_task(run_id, "reverse", "unpack", "")
    store.finish_task(task.id, TaskStatus.EXHAUSTED, {"summary": "stuck"}, "")
    store.add_fact(
        run_id, task.id, "unknown", "运行时 GetTickCount 的返回值序列是什么？", {}, "inferred"
    )
    closed = store.add_fact(
        run_id, task.id, "unknown", "这是不是 AES？", {}, "inferred"
    )
    store.add_fact(run_id, task.id, "insight", "确实是 AES-128-ECB", {}, "observed", closed)

    run = _Stub(store, run_id, store.get_challenge(challenge_id))
    case = caselib.distill(run)
    assert case is not None
    assert len(case.open_unknowns) == 1
    assert "GetTickCount" in case.open_unknowns[0]

    store.upsert_case(case)
    block = caselib.render(store.list_cases())
    assert "仍未解的问题" in block
    assert "GetTickCount" in block
    # A question the run answered is not carried forward as if it were open.
    assert "这是不是 AES" not in block


def test_a_losing_run_costs_its_cards_their_place(cfg, fixtures_dir: Path, store_and_bus) -> None:
    """Exposure alone cannot tell a good card from a misleading one.

    ``used_count`` only counts loads. A card that rode along on three losing
    runs and none winning is worse than a card nobody tried; without the
    outcome the library can only get louder.
    """
    store, _ = store_and_bus
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = "reverse"
    challenge_id = store.add_challenge(challenge)
    other = store.add_challenge(challenge)

    def card(run: int, name: str, helped: int, harmed: int) -> None:
        store.upsert_case(
            Case(
                id=0, run_id=run, challenge_id=challenge_id, name=name,
                category="reverse", outcome="solved", route=[f"reverse[done] {name}"],
                help_count=helped, harm_count=harmed,
            )
        )

    card(store.create_run(other, "solve").id, "lucky", 3, 0)
    card(store.create_run(other, "solve").id, "liar", 0, 3)
    picked = caselib.relevant(store, store.get_challenge(challenge_id), limit=1)
    assert [c.name for c in picked] == ["lucky"]
    # One failure is noise, not a verdict: a card needs two decided runs.
    card(store.create_run(other, "solve").id, "fresh", 0, 1)
    picked = caselib.relevant(store, store.get_challenge(challenge_id), limit=2)
    assert "fresh" in [c.name for c in picked]


def test_an_outcome_is_charged_to_a_card_once(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A run can be closed twice (reopen, late confirmation); it has one verdict.

    The card must also have been *used*, not merely loaded: its techniques have
    to show up in what the run wrote. See
    ``test_a_card_in_the_prompt_but_never_applied_earns_nothing``.
    """
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    produced = store.create_run(challenge_id, "solve").id
    case_id = store.upsert_case(
        Case(
            id=0, run_id=produced, challenge_id=challenge_id, name="c",
            category="reverse", outcome="solved", route=["reverse[done]"],
            techniques=["memcmp", "xordump"],
        )
    )
    consumer = store.create_run(challenge_id, "solve").id
    store.set_run_cases(consumer, [case_id])
    store.add_fact(consumer, None, "observation", "xordump 的输出与 memcmp 尾比较一致", {}, "observed")

    assert caselib.attribute(store, consumer, True) == 1
    assert caselib.attribute(store, consumer, True) == 0
    stored = store.get_case(case_id)
    assert (stored.help_count, stored.harm_count) == (1, 0)


async def test_a_run_charges_its_verdict_to_every_card_it_used(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The close credits help/harm once the outcome is known *and* the card was used.

    The card is loaded lazily, on the first dispatch — which is exactly why the
    run id set is recorded at load time rather than rebuilt at close: a run that
    never dispatched was never influenced by the library and must not credit it.
    Being in the prompt is still not enough; the run has to echo the card's
    techniques back on its blackboard or in its flag derivation.
    """
    store, bus = store_and_bus
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = "reverse"
    other = store.add_challenge(challenge)
    case_id = store.upsert_case(
        Case(
            id=0, run_id=store.create_run(other, "solve").id, challenge_id=other,
            name="prior", category="reverse", outcome="failed",
            route=["reverse[exhausted] tried the wrapper first"],
            techniques=["pyinstaller", "onefile"],
        )
    )

    run = _manager(store, bus, cfg).prepare(store.add_challenge(challenge))
    assert store.get_run_cases(run.id) == []  # nothing loaded yet
    assert "战例库" in run._main_spec().system_prompt
    assert store.get_run_cases(run.id) == [case_id]

    flag = store.add_flag(
        run.id, None, "flag{x}", "out.bin",
        "pyinstaller onefile 解包后从 _MEI 目录取到明文", "high",
    )
    store.set_flag_status(flag.id, FlagStatus.CONFIRMED)
    await run.shutdown("solved", "done", "test")

    card = store.get_case(case_id)
    assert (card.help_count, card.harm_count) == (1, 0)

    # A second close (a reopen that ends the same way) must not credit twice.
    await run.shutdown("solved", "done", "test")
    assert store.get_case(case_id).help_count == 1


def test_a_card_in_the_prompt_but_never_applied_earns_nothing(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Exposure is not use — the credit bug that made wrong cards louder.

    Measured on the live database: a failed card read ``help_count=5`` because
    it had been injected into five runs that later won, and the retrieval score
    then rewarded it on every future challenge. A card whose techniques never
    appear in the run's own records is not credited, win or lose.
    """
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    produced = store.create_run(challenge_id, "solve").id
    case_id = store.upsert_case(
        Case(
            id=0, run_id=produced, challenge_id=challenge_id, name="noise",
            category="reverse", outcome="failed", route=["reverse[done] 毫无关系"],
            techniques=["frida", "unicorn"],
        )
    )
    consumer = store.create_run(challenge_id, "solve").id
    store.set_run_cases(consumer, [case_id])
    store.add_fact(consumer, None, "observation", "反汇编里只看到一次 memcmp", {}, "observed")

    assert caselib.used_case_ids(store, consumer) == []
    assert caselib.attribute(store, consumer, True) == 0
    assert (store.get_case(case_id).help_count, store.get_case(case_id).harm_count) == (0, 0)


def test_a_card_carries_the_unverified_marker_into_the_next_run(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """An unverified negative must not reach the next run as "已证实的死路".

    The card is the longest-lived artifact of a run: it is distilled once and
    injected into every later challenge of the same shape. A negative that never
    stated its search space therefore outlives the run that could have corrected
    it — which is exactly how "2^24 has no solution" survived into the second
    solve of the same challenge and argued against the correct sweep.
    """
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    run_id = store.create_run(challenge_id, "solve").id
    task = store.create_task(run_id, "crypto", "sweep", "")

    store.add_fact(
        run_id, task.id, "dead_end", "seed 低 24 位扫完无解",
        {"command": "python bsgs.py", "output": "HITS: []", "negative_gap": "没跑阳性对照"},
        "observed",
    )
    store.add_fact(
        run_id, task.id, "dead_end", "对整条 ZIP 链做字典攻击无命中",
        {"scope": "8 位数字口令", "control": "自造密码 TestPass123 命中"}, "observed",
    )
    case = caselib.distill(_Stub(store, run_id, store.get_challenge(challenge_id)))
    assert case is not None
    marked, plain = case.dead_ends
    assert "未验证的否定" in marked and "没跑阳性对照" in marked
    assert "未验证" not in plain
    block = caselib.render([case])
    assert "先看清楚它有没有做过阳性对照" in block


async def test_triage_category_reaches_the_row_and_the_card(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """Triage's conclusion must land on ``challenges.category`` and on the run.

    Live observation: 12 of the 14 cards in the library carry
    ``category='unknown'`` because triage's ``category=<cat>`` finding never
    left the blackboard of the run that produced it. The category is the
    library's strongest retrieval signal (+3 and an admission gate), so a
    conclusion that dies with its run makes every card distilled from it hard
    to retrieve and impossible to credit.
    """
    store, bus = store_and_bus
    cfg.limits.workers_per_run = 1
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = "unknown"  # the shape of a challenge created by pasting
    challenge_id = store.add_challenge(challenge)
    assert store.get_challenge(challenge_id).category == "unknown"

    run = _manager(store, bus, cfg).prepare(challenge_id)
    task = store.create_task(run.id, "triage", "classify", "")
    # The shape live triage runs produce: the prescribed ``category=<cat>``
    # finding with CJK punctuation straight after the token.
    run.complete_task(
        task.id,
        summary="题目是 crypto：nonce 可复用，先恢复 LCG 参数再伪造",
        findings=["category=crypto：nonce 重用，附件里有两个共享 p 的密文"],
        next_steps=["ctf-crypto: nonce reuse"],
    )
    run._finish_task(task.id, "triage", TaskStatus.DONE, "", {}, "")

    assert run.challenge.category == "crypto"  # caselib.distill reads this one
    assert store.get_challenge(challenge_id).category == "crypto"  # the row
    assert "challenge.categorized" in [e.kind for e in store.events_since(run.id)]

    # The card distilled at close carries what the run concluded, and a later
    # triage cannot flip it: the first decision was made on the same evidence.
    store.add_fact(run.id, task.id, "dead_end", "直接暴力扫 2^48 不可行", {}, "observed")
    await run.shutdown("failed", "时间用尽", "clock")
    assert store.list_cases()[0].category == "crypto"

    second = _manager(store, bus, cfg).prepare(challenge_id)
    task2 = store.create_task(second.id, "triage", "classify", "")
    second.complete_task(task2.id, summary="再看一次", findings=["category=web"])
    second._finish_task(task2.id, "triage", TaskStatus.DONE, "", {}, "")
    assert store.get_challenge(challenge_id).category == "crypto"


async def test_a_category_somebody_already_decided_outranks_triage(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The platform's category is not a guess for the model to correct.

    CTFd sync writes ``challenges.category`` from the platform; an operator can
    set it by hand in ``challenge.yaml``. Either way it outranks triage, and a
    run on a challenge someone already classified must not quietly relabel it.
    """
    store, bus = store_and_bus
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = "web"  # what a platform sync would store
    challenge_id = store.add_challenge(challenge)

    run = _manager(store, bus, cfg).prepare(challenge_id)
    task = store.create_task(run.id, "triage", "classify", "")
    run.complete_task(
        task.id, summary="这是 crypto", findings=["category=crypto — 密文结构明显"]
    )
    run._finish_task(task.id, "triage", TaskStatus.DONE, "", {}, "")

    assert store.get_challenge(challenge_id).category == "web"
    assert run.challenge.category == "web"
    assert "challenge.categorized" not in [e.kind for e in store.events_since(run.id)]


async def test_a_triage_that_concluded_nothing_writes_nothing(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """A stopped triage task and a typo'd category both leave the row alone.

    Live observation: 2 of the 4 triage tasks in the library were ``stopped``
    with no findings. A write-back that guessed on fragments would label the
    challenge with whatever the model happened to be saying when it was cut
    off, and the row is what every future run's card retrieval keys on.
    """
    store, bus = store_and_bus
    challenge = load_challenge(fixtures_dir / "easy_rev")
    challenge.category = "unknown"  # pasted in, nobody has classified it yet
    challenge_id = store.add_challenge(challenge)

    run = _manager(store, bus, cfg).prepare(challenge_id)
    stopped = store.create_task(run.id, "triage", "classify", "")
    run._finish_task(stopped.id, "triage", TaskStatus.STOPPED, "", {}, "被打断时的话")

    typo = store.create_task(run.id, "triage", "classify", "")
    run.complete_task(typo.id, summary="像是 reversing 题", findings=["category=reversing"])
    run._finish_task(typo.id, "triage", TaskStatus.DONE, "", {}, "")

    assert store.get_challenge(challenge_id).category == "unknown"
    assert run.challenge.category == "unknown"
    assert "challenge.categorized" not in [e.kind for e in store.events_since(run.id)]


def test_a_recount_replaces_exposure_counts_with_use_counts(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The live library had to be corrected, not just fixed going forward.

    Card #15 read 5-0 because it had been *in the prompt* of five runs, four of
    which lost. Fixing the attribution rule left those numbers in place, and
    they are what the retrieval score reads. A recount zeroes them and replays
    the same rule over the historical runs' own records — which is the only
    evidence that survives.

    The dry run must be honest: it reports the change and makes none.
    """
    from ctfagent.cli import _recount

    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_rev"))
    produced = store.create_run(challenge_id, "solve").id
    case_id = store.upsert_case(
        Case(
            id=0, run_id=produced, challenge_id=challenge_id, name="exposed",
            category="reverse", outcome="failed", route=["reverse[exhausted] tried"],
            techniques=["frida", "unicorn"],
        )
    )
    # Two consumers: one whose records echo the card, one that never used it.
    used = store.create_run(challenge_id, "solve").id
    store.set_run_cases(used, [case_id])
    store.add_fact(used, None, "observation", "用 frida 挂钩后 unicorn 模拟失败", {}, "observed")
    store.set_run_status(used, "failed", summary="没解出")
    ignored = store.create_run(challenge_id, "solve").id
    store.set_run_cases(ignored, [case_id])
    store.add_fact(ignored, None, "observation", "只是 http 请求", {}, "observed")
    store.set_run_status(ignored, "failed", summary="没解出")
    # The legacy exposure numbers.
    store.credit_case(case_id, helped=True)
    store.credit_case(case_id, helped=True)
    store._exec("UPDATE runs SET cases_credited=1")

    assert _recount(cfg, apply=False) == 0
    assert store.get_case(case_id).help_count == 2  # dry run changed nothing

    assert _recount(cfg, apply=True) == 0
    card = store.get_case(case_id)
    assert (card.help_count, card.harm_count) == (0, 1)  # the run that used it lost
    assert store.get_run_cases(used) == [case_id]  # the load record is not evidence


# ------------------------------------------------------------ title normalize
# A pasted challenge's first line — URL plus the whole task prose — became
# challenge.name, and the run header, the task rail and the browser tab all
# rendered it. Triage already knows the direction and the target; the moment
# it concludes, the title is rebuilt as 「方向 · 主机 · 短语」.


def test_auto_title_composes_direction_host_and_phrase() -> None:
    from ctfagent.orchestrator import _auto_title

    title = _auto_title(
        "web",
        "http://hbc2.haobachang.com:24386/login 一套面向企业内部的流程审批平台",
        "一套面向企业内部的流程审批与附件管理平台，主要用于处理日常办公申请。",
        "",
    )
    assert title.startswith("web · hbc2.haobachang.com:24386 · ")
    assert "://" not in title
    assert "一套面向企业内部" in title
    assert len(title) <= 46


def test_auto_title_falls_back_to_remote_and_name() -> None:
    from ctfagent.orchestrator import _auto_title

    # a bare nc target and no prose: host comes from remote, phrase from title
    title = _auto_title("pwn", "一道 pwn 题", "", "nc 1.2.3.4 31337")
    assert title == "pwn · 1.2.3.4:31337 · 一道 pwn 题"

    # nothing at all but the direction: it still gets a clean short title
    assert _auto_title("misc", "", "", "") == "misc"


async def test_a_pasted_title_is_normalized_when_triage_concludes(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """The blob title dies the moment triage names the direction."""
    store, bus = store_and_bus
    challenge = load_challenge(fixtures_dir / "easy_crypto")
    challenge.name = "http://hbc2.haobachang.com:24386/login 一套面向企业内部的流程审批平台"
    challenge.description = "一套流程审批与附件管理平台，用于处理日常办公申请。"
    challenge.category = "unknown"
    challenge_id = store.add_challenge(challenge)

    run = _manager(store, bus, cfg).prepare(challenge_id)
    task = store.create_task(run.id, "triage", "classify", "")
    run.complete_task(task.id, summary="web 题", findings=["category=web"])
    run._finish_task(task.id, "triage", TaskStatus.DONE, "", {}, "")

    renamed = store.get_challenge(challenge_id).name
    assert renamed == run.challenge.name  # row and memory agree
    assert renamed.startswith("web · ")
    assert "://" not in renamed
    assert "challenge.renamed" in [e.kind for e in store.events_since(run.id)]

    # The normalized title is final: another triage cannot churn it.
    task2 = store.create_task(run.id, "triage", "classify", "")
    run.complete_task(task2.id, summary="再看", findings=["category=web"])
    run._finish_task(task2.id, "triage", TaskStatus.DONE, "", {}, "")
    assert store.get_challenge(challenge_id).name == renamed


async def test_a_human_title_is_never_normalized(
    cfg, fixtures_dir: Path, store_and_bus
) -> None:
    """challenge.yaml and operator-chosen names are not the composer's to fix."""
    store, bus = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))

    run = _manager(store, bus, cfg).prepare(challenge_id)
    task = store.create_task(run.id, "triage", "classify", "")
    run.complete_task(task.id, summary="crypto", findings=["category=crypto"])
    run._finish_task(task.id, "triage", TaskStatus.DONE, "", {}, "")

    assert store.get_challenge(challenge_id).name == "easy-crypto"
    assert "challenge.renamed" not in [e.kind for e in store.events_since(run.id)]


def test_rename_challenge_refuses_a_human_name(store_and_bus, fixtures_dir: Path) -> None:
    """The SQL guard: even a stale caller cannot rename a named challenge."""
    store, _ = store_and_bus
    challenge_id = store.add_challenge(load_challenge(fixtures_dir / "easy_crypto"))
    assert store.rename_challenge(challenge_id, "web · x") is False
    assert store.get_challenge(challenge_id).name == "easy-crypto"
