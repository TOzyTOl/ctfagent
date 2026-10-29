"""CLI: ``ctfagent serve | run | list | show | confirm | doctor``.

The CLI is the second surface on purpose: a competition run is often driven from
one terminal, and the web UI is for watching several runs at once. Both share
the same Store and RunManager.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .backend import ClaudeSDKBackend, resolve_cli_path
from .challenge import load_challenge_dir
from .config import default_config_toml, load_config
from . import costing
from .db import Store
from .events import EventBus
from .models import (
    BASIS_LABEL,
    CONFIDENCE_LABEL,
    EVENT_LABEL,
    FACT_KIND_LABEL,
    FLAG_STATUS_LABEL,
    ROLE_LABEL,
    RUN_STATUS_LABEL,
    TASK_STATUS_LABEL,
    FlagStatus,
    RunStatus,
)
from .orchestrator import RunManager
from .workspace import probe_sandbox


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ctfagent", description="多智能体 CTF 解题系统")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动网页界面")
    p_serve.add_argument("--host", default="")
    p_serve.add_argument("--port", type=int, default=0)
    p_serve.add_argument("--scripted", action="store_true", help="离线演示后端，不调用大模型")

    p_run = sub.add_parser("run", help="解一道题（给题目目录）")
    p_run.add_argument("directory", help="包含 challenge.yaml 的题目目录")
    p_run.add_argument("--objective", default="")
    p_run.add_argument("--quiet", action="store_true", help="只打印最终结果")

    sub.add_parser("list", help="列出题目和运行")

    p_show = sub.add_parser("show", help="查看一次运行的工单、黑板和 flag")
    p_show.add_argument("run_id", type=int)

    p_confirm = sub.add_parser("confirm", help="确认一个候选 flag")
    p_confirm.add_argument("flag_id", type=int)
    p_confirm.add_argument("--note", default="")
    p_writeup = sub.add_parser("writeup", help="为已确认 flag 的运行写解题报告")
    p_writeup.add_argument("run_id", type=int)
    p_distill = sub.add_parser(
        "distill", help="把已解出的运行蒸馏成技术笔记，装入对应方向的技能库"
    )
    p_distill.add_argument("run_id", type=int)

    p_delete = sub.add_parser("delete", help="删除一次运行（记录、黑板、工作区）")
    p_delete.add_argument("run_id", type=int)
    p_delete.add_argument("--keep-workspace", action="store_true", help="保留工作区目录")

    p_cost = sub.add_parser("cost", help="成本构成：token、钱、按角色与会话")
    p_cost.add_argument("--run", type=int, default=None, dest="run_id", help="只看这次运行")
    p_cost.add_argument("--json", action="store_true", help="输出 JSON")

    sub.add_parser("memory", help="看记忆闭环：战例卡、方向落库、实战笔记")

    p_recount = sub.add_parser(
        "recount",
        help="按「用了才算」重新判一遍历史运行的卡片记账（旧的 help/harm 是曝光数）",
    )
    p_recount.add_argument(
        "--apply", action="store_true", help="真的清零并重记；不加只打印会改什么"
    )

    p_bench = sub.add_parser("bench", help="跑基准集，衡量平台是不是真的变强了")
    p_bench.add_argument("directory", help="包含 bench.yaml 的基准目录")
    p_bench.add_argument(
        "--library", default="", help="从另一个数据库复制战例卡作为训练成果（train/test 分离）"
    )
    p_bench.add_argument(
        "--notes",
        default="",
        help="用哪个技能目录里的实战笔记（默认不用：基准默认隔离 learned-*.md，"
        "否则测的是训练集）",
    )
    p_bench.add_argument(
        "--baseline", default="", help="与一份旧结果对比，只打印差异"
    )
    p_bench.add_argument(
        "--out", default="", help="结果写到哪个 json（默认基准目录 results/ 下按时间命名）"
    )
    p_bench.add_argument("--keep-db", action="store_true", help="保留基准用的临时数据库")
    p_bench.add_argument("--only", default="", help="只跑名字匹配的条目（逗号分隔）")


    sub.add_parser("doctor", help="检查运行环境")

    p_init = sub.add_parser("init", help="生成 config.toml 和技能库说明")
    p_init.add_argument("--force", action="store_true")
    return parser


def _make_output_never_raise() -> None:
    """Stop a console code page from turning a finished run into a traceback.

    The report is written before anything is printed, so a print failure only
    costs the operator the summary — but it cost exactly that twice, and looked
    like a crash (exit 1, traceback) for a benchmark that had run fine. Chinese
    Windows consoles default to GBK, which has no ``⚠``, ``✓``, ``✗`` or ``◂``.
    The glyphs used here are all GBK-safe now; this is the belt for the braces,
    so a future one cannot do it again.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # closed or wrapped stream
            pass


def main(argv: list[str] | None = None) -> int:
    _make_output_never_raise()
    args = _build_parser().parse_args(argv)
    cfg = load_config()
    if args.cmd == "serve":
        return _serve(cfg, args)
    if args.cmd == "run":
        return asyncio.run(_run(cfg, args))
    if args.cmd == "list":
        return _list(cfg)
    if args.cmd == "show":
        return _show(cfg, args.run_id)
    if args.cmd == "confirm":
        return asyncio.run(_confirm(cfg, args.flag_id, args.note))
    if args.cmd == "writeup":
        return asyncio.run(_writeup(cfg, args.run_id))
    if args.cmd == "distill":
        return asyncio.run(_distill(cfg, args.run_id))
    if args.cmd == "cost":
        return _cost(cfg, args.run_id, args.json)
    if args.cmd == "memory":
        return _memory(cfg)
    if args.cmd == "recount":
        return _recount(cfg, args.apply)
    if args.cmd == "delete":
        return asyncio.run(_delete(cfg, args.run_id, args.keep_workspace))
    if args.cmd == "bench":
        return asyncio.run(_bench(cfg, args))
    if args.cmd == "doctor":
        return _doctor(cfg)
    if args.cmd == "init":
        return _init(cfg, args.force)
    return 2


def _serve(cfg, args) -> int:
    import uvicorn  # noqa: PLC0415

    from . import instance  # noqa: PLC0415
    from .server import create_app  # noqa: PLC0415

    host = args.host or cfg.host
    port = args.port or cfg.port

    existing = instance.acquire(cfg, host, port)
    if existing:
        print(
            instance.already_running_message(int(existing["pid"]), host, int(existing["port"])),
            file=sys.stderr,
        )
        return 3

    app = create_app(cfg, scripted=args.scripted)
    print(f"CTFAGENT 已启动：http://{host}:{port}")
    if not args.scripted:
        # Said once at startup, where the operator is still looking at the
        # terminal. Otherwise the first symptom is a run whose every turn
        # returns the CLI's interactive login prompt, which reads like a lost
        # session rather than a missing token.
        from .backend import credentials_missing  # noqa: PLC0415

        if credentials_missing(cfg):
            print(
                "警告：模型凭据缺失。子会话以 setting_sources=[\"project\"] 运行，"
                "不会读 ~/.claude/settings.json。开跑前请设置 ANTHROPIC_AUTH_TOKEN"
                "（或 ANTHROPIC_API_KEY），或在 config.toml 写 auth_token。",
                file=sys.stderr,
            )
        else:
            where = {
                "env": "环境变量",
                "toml": "config.toml",
                "settings": "~/.claude/settings.json",
            }.get(cfg.models.credential_source, cfg.models.credential_source)
            print(f"模型凭据来源：{where}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        instance.release(cfg)
    return 0


async def _run(cfg, args) -> int:
    directory = Path(args.directory).expanduser()
    if not directory.is_dir():
        print(f"不是目录：{directory}", file=sys.stderr)
        return 2
    store = Store(cfg.db_path)
    bus = EventBus(store)
    sandbox = probe_sandbox(cfg)
    if cfg.sandbox.mode == "docker" and not sandbox.available:
        print(f"提示：{sandbox.note}", file=sys.stderr)
    manager = RunManager(store, bus, cfg, ClaudeSDKBackend(cfg), sandbox, cfg.skills_dir)
    challenge = load_challenge_dir(directory)
    challenge_id = store.add_challenge(challenge)
    run = await manager.start(challenge_id, args.objective)

    # Follow the event stream in the terminal until the run finishes or pauses.
    cursor = 0
    last_status = ""
    paused = False
    while True:
        await asyncio.sleep(0.5)
        for event in store.events_since(run.id, cursor, limit=500):
            cursor = event.id
            if not args.quiet:
                _print_event(event)
        fresh = store.get_run(run.id)
        if fresh is None:
            break
        last_status = fresh.status
        if last_status in {RunStatus.SOLVED, RunStatus.FAILED, RunStatus.STOPPED}:
            # Give trailing events a moment to land.
            await asyncio.sleep(0.3)
            for event in store.events_since(run.id, cursor, limit=500):
                if not args.quiet:
                    _print_event(event)
            break
        if last_status == RunStatus.NEEDS_INPUT:
            paused = True
            break

    final = store.get_run(run.id)
    print()
    print(f"运行 #{run.id}：{RUN_STATUS_LABEL.get(str(final.status if final else last_status), final.status if final else last_status)}")
    if final and final.summary:
        print(final.summary)
    for flag in store.list_flags(run.id):
        print(f"  flag [{FLAG_STATUS_LABEL.get(flag.status, flag.status)}] {flag.value}  (工单#{flag.task_id})")

    # A paused run is the operator's turn. Ask inline when a human is attached;
    # otherwise leave the run open and tell them how to confirm it later.
    if paused:
        candidates = [f for f in store.list_flags(run.id) if f.status in ("candidate", "verified")]
        if candidates and sys.stdin.isatty():
            try:
                answer = input(
                    f"\n确认 flag {candidates[-1].value}？[y/N] "
                ).strip().lower()
            except (EOFError, OSError):
                # isatty() is not enough: a launch with a closed stdin (a
                # background/redirected shell on Windows) still reports a tty
                # and then raises here. Crashing the launcher loses the live
                # run object, which is what holds the confirmation dispatch.
                answer = ""
            if answer in ("y", "yes"):
                await run.confirm_flag(candidates[-1].id, "在命令行确认")
                final = store.get_run(run.id)
                print(f"已确认；运行 #{run.id} 状态为 {final.status}")
                print(f"报告：{run.workspace.root / 'writeup.md'}")
                await manager.stop_all()
                return 0
        # Stay alive: the run object lives in this process, so closing the
        # process would lose the writer dispatch that confirmation triggers.
        # The operator confirms from another terminal; this one follows along.
        print(
            f"\n等待确认 —— 在另一个终端执行：\n"
            f"  ctfagent confirm <flag_id>\n"
            f"这次运行会一直开着（Ctrl+C 放弃）"
        )
        try:
            while True:
                await asyncio.sleep(1.0)
                for event in store.events_since(run.id, cursor, limit=500):
                    cursor = event.id
                    if not args.quiet:
                        _print_event(event)
                fresh = store.get_run(run.id)
                if fresh is None or fresh.status in {
                    RunStatus.SOLVED, RunStatus.FAILED, RunStatus.STOPPED
                }:
                    break
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        final = store.get_run(run.id)
        if final:
            print(f"运行 #{run.id}：{RUN_STATUS_LABEL.get(final.status, final.status)}  {final.summary}")
            print(f"报告：{run.workspace.root / 'writeup.md'}")
        await manager.stop_all()
        return 0 if final and final.status == RunStatus.SOLVED else 1

    await manager.stop_all()
    return 0 if final and final.status == RunStatus.SOLVED else 1


def _print_event(event) -> None:
    payload = event.payload
    kind = event.kind
    if kind == "agent.event":
        inner = payload.get("kind")
        role = payload.get("role", "")
        if inner == "tool_use":
            text = f"{ROLE_LABEL.get(role, role)} → {payload.get('tool')} {json.dumps(payload.get('input'), ensure_ascii=False)[:220]}"
        elif inner in ("text", "thinking"):
            text = f"{ROLE_LABEL.get(role, role)} {'说' if inner == 'text' else '想'}：{str(payload.get('text', ''))[:300]}"
        elif inner == "tool_result":
            text = f"{ROLE_LABEL.get(role, role)} ← 返回 {payload.get('id', '')}"
        elif inner == "result":
            text = f"{ROLE_LABEL.get(role, role)} √ 结束 (${payload.get('cost_usd', 0)})"
        else:
            return
        print(f"  {text}")
        return
    if kind in ("task.dispatched", "task.finished", "task.started", "fact.added",
                "fact.merged", "fact.downgraded",
                "flag.proposed", "flag.verdict", "run.started", "run.finished",
                "run.needs_input", "chat.user", "main.message", "run.error", "run.warning"):
        summary = payload.get("summary") or payload.get("objective") or payload.get("content") \
            or payload.get("value") or payload.get("text") or payload.get("warning") or ""
        if kind == "fact.downgraded":
            summary = f"{payload.get('reason', '')}：{summary}"
        print(f"[{EVENT_LABEL.get(kind, kind)}] {str(summary)[:300]}")


def _list(cfg) -> int:
    store = Store(cfg.db_path)
    challenges = store.list_challenges()
    runs = store.list_runs(50)
    if challenges:
        print("题目：")
        for c in challenges:
            print(f"  #{c.id:<4} {c.name:<24} {ROLE_LABEL.get(c.category, c.category):<10} {c.remote}")
    if runs:
        print("运行：")
        for r in runs:
            print(f"  #{r.id:<4} {RUN_STATUS_LABEL.get(r.status, r.status):<12} ${r.cost_usd:<8.3f} {r.summary[:70] or r.objective[:70]}")
    if not challenges and not runs:
        print("还没有任何记录 —— 用 `ctfagent run <目录>` 导入一道题")
    return 0


def _show(cfg, run_id: int) -> int:
    store = Store(cfg.db_path)
    run = store.get_run(run_id)
    if run is None:
        print(f"没有这次运行：{run_id}", file=sys.stderr)
        return 2
    print(f"运行 #{run.id}  {RUN_STATUS_LABEL.get(run.status, run.status)}  ${run.cost_usd:.3f}  {run.workspace}")
    if run.summary:
        print(f"总结：{run.summary}")
    print("\n工单：")
    for t in store.list_tasks(run_id):
        print(f"  #{t.id:<4} {ROLE_LABEL.get(t.role, t.role):<10} {TASK_STATUS_LABEL.get(t.status, t.status):<10} ${t.cost_usd:<7.3f} {t.objective[:80]}")
        summary = str(t.result.get("summary", "")).replace("\n", " ")[:200]
        if summary:
            print(f"        {summary}")
    flags = store.list_flags(run_id)
    if flags:
        print("\nflag：")
        for f in flags:
            print(f"  #{f.id:<4} [{FLAG_STATUS_LABEL.get(f.status, f.status)}] {f.value}  可信度 {CONFIDENCE_LABEL.get(f.confidence, f.confidence)}  来源 {f.source}")
    facts = store.list_facts(run_id)
    if facts:
        print("\n黑板：")
        for f in facts:
            basis = f" {BASIS_LABEL[f.basis]}" if f.basis in BASIS_LABEL and f.basis else ""
            closed = f" → 解决 #{f.resolves}" if f.resolves else ""
            print(f"  #{f.id:<4} ({FACT_KIND_LABEL.get(f.kind, f.kind)}{basis}) 工单#{f.task_id or '主 agent'} {f.content[:200]}{closed}")
    return 0


async def _confirm(cfg, flag_id: int, note: str) -> int:
    store = Store(cfg.db_path)
    bus = EventBus(store)
    sandbox = probe_sandbox(cfg)
    manager = RunManager(store, bus, cfg, ClaudeSDKBackend(cfg), sandbox, cfg.skills_dir)
    flag = store.get_flag(flag_id)
    if flag is None:
        print(f"没有这个 flag：{flag_id}", file=sys.stderr)
        return 2
    run = manager.attach(flag.run_id)
    if run is None:
        # The challenge is gone from the database; record the decision only.
        store.set_flag_status(flag_id, "confirmed", note or "操作员确认")
        store.set_run_status(flag.run_id, RunStatus.SOLVED, summary=f"flag 已确认：{flag.value}")
        print(f"已确认 flag #{flag_id}：{flag.value}")
        store.close()
        return 0
    await run.confirm_flag(flag_id, note)
    print(f"已确认 flag #{flag_id}：{flag.value}")
    if cfg.limits.writeup_on_confirm:
        writeup = run.workspace.root / "writeup.md"
        print(f"报告：{writeup if writeup.exists() else '（writer 没有产出）'}")
    else:
        print(f"未写报告（writeup_on_confirm=false）。需要时：ctfagent writeup {flag.run_id}")
    await manager.stop_all()
    store.close()
    return 0


async def _writeup(cfg, run_id: int) -> int:
    """Write the report for a finished run, on request."""
    store = Store(cfg.db_path)
    bus = EventBus(store)
    sandbox = probe_sandbox(cfg)
    manager = RunManager(store, bus, cfg, ClaudeSDKBackend(cfg), sandbox, cfg.skills_dir)
    run = manager.attach(run_id)
    if run is None:
        print(f"没有这次运行：{run_id}", file=sys.stderr)
        store.close()
        return 2
    out = await run.write_report()
    if out.get("error"):
        print(out["error"], file=sys.stderr)
        await manager.stop_all()
        store.close()
        return 2
    if out.get("written"):
        print(f"报告已写入 {out.get('path')}")
    else:
        print("报告任务已结束，但没有产出 writeup.md")
    await manager.stop_all()
    store.close()
    return 0


async def _distill(cfg, run_id: int) -> int:
    """Distil a solved run into a technique note in the skill library."""
    store = Store(cfg.db_path)
    bus = EventBus(store)
    sandbox = probe_sandbox(cfg)
    manager = RunManager(store, bus, cfg, ClaudeSDKBackend(cfg), sandbox, cfg.skills_dir)
    run = manager.attach(run_id)
    if run is None:
        print(f"没有这次运行：{run_id}", file=sys.stderr)
        store.close()
        return 2
    out = await run.distill_technique()
    if out.get("error"):
        print(out["error"], file=sys.stderr)
        await manager.stop_all()
        store.close()
        return 2
    print(f"技术笔记已装入技能库：{out.get('skill')} → {out.get('path')}")
    print("该方向的后续 worker 会在派发时看到笔记索引。")
    await manager.stop_all()
    store.close()
    return 0


def _cost(cfg, run_id: int | None, as_json: bool) -> int:
    """Token and money breakdown — where the spend actually goes."""
    store = Store(cfg.db_path)
    try:
        data = costing.report(store, cfg, run_id)
    finally:
        store.close()
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(costing.render(data))
    return 0


async def _delete(cfg, run_id: int, keep_workspace: bool) -> int:
    store = Store(cfg.db_path)
    run = store.get_run(run_id)
    if run is None:
        print(f"没有这次运行：{run_id}", file=sys.stderr)
        store.close()
        return 2
    bus = EventBus(store)
    sandbox = probe_sandbox(cfg)
    manager = RunManager(store, bus, cfg, ClaudeSDKBackend(cfg), sandbox, cfg.skills_dir)
    if keep_workspace:
        store.delete_run(run_id)
    else:
        await manager.delete(run_id)
    print(f"已删除运行 #{run_id}（{run.status}，${run.cost_usd:.3f}）")
    if keep_workspace and run.workspace:
        print(f"工作区保留：{run.workspace}")
    store.close()
    return 0


def _memory(cfg) -> int:
    """The learning loop, in numbers — the health check the loop never had.

    Every mechanism of the loop can fail silently: a card is written but never
    retrieved, a card is retrieved but credited for exposure, triage never names
    a direction so every card is filed ``unknown``, a note is distilled and then
    invisible. The live database had all four at once and no surface said so.
    Nothing here calls a model.
    """
    from . import caselib, notes  # noqa: PLC0415 - only this command needs them

    store = Store(cfg.db_path)
    try:
        cases = store.list_cases(limit=400)
        challenges = store.list_challenges()
        runs = store.list_runs(limit=100000)
    finally:
        store.close()
    unknown_challenges = [c for c in challenges if (c.category or "unknown") in ("", "unknown")]
    unknown_cases = [c for c in cases if (c.category or "unknown") in ("", "unknown")]
    boilerplate = caselib.boilerplate_tokens(cases)
    print(f"战例卡          {len(cases)} 张，其中方向 unknown 的 {len(unknown_cases)} 张")
    print(f"题目方向        {len(challenges)} 道，其中 unknown/空 的 {len(unknown_challenges)} 道")
    if boilerplate:
        print(f"库内样板词      {', '.join(sorted(boilerplate))}")
        print("                这些词在检索时被剔除；它们出现在多数卡片里，不说明任何事。")
    attributed = [r for r in runs if r.cases_credited]
    notes_runs = [r for r in runs if r.notes_credited]
    print(f"已裁决的运行    {len(attributed)} 次卡片记账 / {len(notes_runs)} 次笔记记账")
    used = [(c.id, c.help_count, c.harm_count) for c in cases if c.help_count or c.harm_count]
    if used:
        print("有记账的卡：")
        for case_id, help_count, harm_count in used:
            print(f"  #{case_id:<4} +{help_count} / -{harm_count}")
    elif cases:
        # The live database was here: 5/0 on a card that had only ever been
        # *shown*. That number was exposure. Now zero means "no run echoed the
        # card's own vocabulary", which is a retrieval problem, not a credit one.
        print("提示            没有任何卡被记为用过：卡被执行过不等于被执行者采用，")
        print("                看下面「未分类题目的战例检索」判断是没命中还是没被用。")

    roots = sorted(p for p in Path(cfg.skills_dir).iterdir() if p.is_dir()) if Path(cfg.skills_dir).is_dir() else []
    total_notes = 0
    for root in roots:
        note_list = notes.index(root)
        if not note_list:
            continue
        total_notes += len(note_list)
        print(f"实战笔记        {root.name}：{len(note_list)} 篇")
        for note in sorted(note_list, key=lambda n: (-(n.helped + n.harmed), n.name))[:5]:
            print(f"  {note.name}  用 {note.used} 帮 {note.helped} 拖 {note.harmed}  {note.title[:30]}")
    if not total_notes:
        print("实战笔记        0 篇（`ctfagent distill <run_id>`、运行页「沉淀笔记」，"
              "或 config.toml 的 distill_on_confirm = true）")

    # Retrieval is the part that fails without a symptom: a card can exist and
    # still never be shown. Report it per unclassified challenge, where the card
    # index used to find nothing at all.
    if unknown_challenges and cases:
        print("\n未分类题目的战例检索（前 5 道）：")
        store = Store(cfg.db_path)
        try:
            for challenge in unknown_challenges[:5]:
                picked = caselib.relevant(store, challenge, limit=3)
                ids = ", ".join(f"#{c.id}({c.category})" for c in picked) or "无命中"
                print(f"  #{challenge.id:<4} {challenge.name[:28]:<28} → {ids}")
        finally:
            store.close()
    return 0


def _recount(cfg, apply: bool) -> int:
    """Rejudge every historical run under the "used, not shown" rule.

    The counters already in the database were written by the exposure rule, and
    the live library carries them: card #15 reads 5-0 for having been *in the
    prompt* of five runs, including four that lost. A recount cannot be done by
    arithmetic on those numbers — the evidence is the runs' own records, which
    are still there — so it zeroes the counters and replays the attribution.

    Dry by default: this rewrites the ranking signal for every future run, and
    the operator should see the diff before it lands.
    """
    from . import caselib  # noqa: PLC0415 - only this command needs it

    store = Store(cfg.db_path)
    try:
        before = {c.id: (c.help_count, c.harm_count) for c in store.list_cases(limit=400)}
        runs = store.list_runs(limit=100000)
        chargeable = []
        for run in runs:
            solved = any(
                f.status in (FlagStatus.VERIFIED, FlagStatus.CONFIRMED)
                for f in store.list_flags(run.id)
            )
            # The close's rule, verbatim: a stopped run with no verdict says
            # nothing about the cards it was shown.
            if run.status in (RunStatus.SOLVED, RunStatus.FAILED) or solved:
                chargeable.append((run, solved))
        if not chargeable:
            print("没有可重新裁决的运行")
            return 0
        if apply:
            reset = store.reset_case_counts()
            for run, _ in chargeable:
                store.clear_case_credit(run.id)
            credited_runs = 0
            for run, solved in chargeable:
                if caselib.attribute(store, run.id, solved):
                    credited_runs += 1
        else:
            reset = sum(1 for pair in before.values() if pair != (0, 0))
            # A dry run must not claim to have replayed anything it did not.
            credited_runs = 0
        after = {c.id: (c.help_count, c.harm_count) for c in store.list_cases(limit=400)}
    finally:
        store.close()

    if not apply:
        print(f"将清零 {reset} 张卡的计数，并按 {len(chargeable)} 次运行重判（加 --apply 执行）")
        print("现在有记账的卡：")
        for case_id, (help_count, harm_count) in sorted(before.items()):
            if (help_count, harm_count) != (0, 0):
                print(f"  #{case_id:<4} +{help_count} / -{harm_count}")
        return 0

    changed = [
        (case_id, before.get(case_id, (0, 0)), pair)
        for case_id, pair in sorted(after.items())
        if pair != before.get(case_id, (0, 0))
    ]
    print(f"已重判 {len(chargeable)} 次运行，{credited_runs} 次确实用到了某张卡")
    if not changed:
        print("没有任何卡的记账发生变化")
    for case_id, (old_h, old_d), (new_h, new_d) in changed:
        print(f"  #{case_id:<4} +{old_h}/-{old_d} → +{new_h}/-{new_d}")
    return 0


def _skill_dirs(root: Path) -> int:
    """Directories under ``root`` that carry a SKILL.md.

    ``install_skills`` links nothing else, so this is the number of skills a new
    run would actually get. Checking ``root.is_dir()`` alone reported 已找到 for
    a tree of eleven empty directories, and every run after that installed zero
    skills while the self-check stayed green.
    """
    try:
        return sum(1 for d in root.iterdir() if (d / "SKILL.md").is_file())
    except OSError:
        return 0


def _doctor(cfg) -> int:
    sandbox = probe_sandbox(cfg)
    cli = resolve_cli_path(cfg.cli_path)
    skills = _skill_dirs(cfg.skills_dir)
    print(f"python          {sys.version.split()[0]}")
    print(f"数据库          {cfg.db_path}")
    print(f"工作区          {cfg.data_dir}")
    print(f"技能库          {cfg.skills_dir} ({f'{skills} 个' if skills else '缺失'})")
    print(f"claude cli      {cli or 'SDK 自带 CLI'}")
    print(f"执行环境        {sandbox.mode} 可用={sandbox.available}（{sandbox.detail}）")
    # pwn is the one direction whose workflow dies without a real Linux
    # runtime: the host cannot execute ELF. Report what is inside the distro
    # so an empty toolbox is visible before a competition, not during one.
    if sandbox.wsl:
        from .workspace import wsl_pwn_toolkit

        tools = wsl_pwn_toolkit(sandbox.wsl)
        print(
            f"WSL pwn 环境     {sandbox.wsl}：{tools or '（pwntools/patchelf/ROPgadget/GEF 一个都没探到，pwn worker 将拿不到 Linux 工作流）'}"
        )
    print(f"最大会话数      {cfg.limits.max_sessions}")
    print(f"模型 base_url   {cfg.models.base_url or '（取自环境变量或 CLI 默认值）'}")
    # Which model each role actually gets. The failure this reports on is a
    # config that looks right and is not: every role pinned to one model for
    # months because a comment in config.toml said the gateway had nothing else.
    print(
        f"模型分配        main={cfg.models.main}  worker={cfg.models.worker}  "
        f"verifier={cfg.models.verifier}  writer={cfg.models.writer}"
    )
    if cfg.models.roles:
        overrides = "  ".join(f"{k}={v}" for k, v in sorted(cfg.models.roles.items()))
        print(f"                方向覆盖：{overrides}")
    # The accounting mode decides whether the budget watchdog watches real
    # money. CLI numbers are an Anthropic price-table lookup by model name —
    # fiction for gateway models — so an unpriced gateway config is told so.
    if cfg.models.pricing:
        priced = "  ".join(sorted(cfg.models.pricing))
        print(f"成本核算        按 token 自算（已配价格表：{priced}）")
    else:
        print("成本核算        CLI 估值（按 Anthropic 价目表查模型名；网关模型建议配 [models.pricing]")
    source = {
        "env": "环境变量",
        "toml": "config.toml",
        "settings": "~/.claude/settings.json（已由服务转发给子会话）",
    }.get(cfg.models.credential_source)
    print(f"模型鉴权        {'已配置（' + source + '）' if source else '缺失'}")
    if not source:
        print(
            "                子会话以 setting_sources=[\"project\"] 运行：环境变量、"
            "config.toml 都没有凭据，~/.claude/settings.json 的 env 段也没有被转发。"
            "启动前设置 ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY，或在 config.toml 写 auth_token。"
        )
    try:
        import claude_agent_sdk  # noqa: F401, PLC0415
        print("claude-agent-sdk 已安装")
    except ImportError:
        print("claude-agent-sdk 未安装（pip install -e .）")
    missing = []
    for module in ("pwn", "z3", "angr", "gmpy2", "Crypto", "capstone", "frida", "volatility3"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    print(f"缺失的 CTF python 包：{', '.join(missing) if missing else '无'}")
    return 0


def _init(cfg, force: bool) -> int:
    path = Path(__file__).resolve().parent.parent / "config.toml"
    if path.exists() and not force:
        print(f"{path} 已存在；要覆盖请加 --force")
        return 1
    path.write_text(default_config_toml(), encoding="utf-8")
    print(f"已写入 {path}")
    return 0


def _duration(seconds) -> str:
    """Render a wall-clock figure as ``N分SS秒``.

    ``elapsed_s`` is always a float — the bench rounds the seconds it computed —
    and ``seconds // 60`` on a float is a float, which ``:02d`` refuses. That
    made the benchmark's print loop raise on *every* item, so no report was ever
    written. The conversion lives here and takes anything numeric.
    """
    secs = int(float(seconds))
    return f"{secs // 60}分{secs % 60:02d}秒"


def _bench_lines(results, summary) -> list[str]:
    """Render the benchmark table and its summary as plain lines.

    Separate from the command so it can be exercised without a model: the one
    time this loop was inline it raised on a float and took a finished, solved,
    paid-for run down with it.

    The check marks are ``√`` / ``×``, not ``✓`` / ``✗``: the latter two are
    outside the GBK code page, and a Windows console at the default code page
    raises ``UnicodeEncodeError`` on ``print``. That is what killed the second
    run of this very table (2026-10-27, easyrs): the report had been written,
    the summary was lost to a traceback.
    """
    lines = [f"{'题目':<20} {'结果':<6} {'成本':>9} {'用时':>8}  说明"]
    for result in results:
        if result.solved:
            why = result.matched_value
        elif result.error:
            why = result.error
        else:
            why = "没有提交匹配的 flag" if result.flag_statuses else "没有提交任何 flag"
        lines.append(
            f"{result.name:<20} {'√ 解出' if result.solved else '× 未解':<6} "
            f"${result.cost_usd:>8.2f} {_duration(result.elapsed_s):>8}  {why[:60]}"
        )
    lines.append(
        f"\n解出 {summary['solved']}/{summary['items']}，总成本 ${summary['cost_usd']:.2f}，"
        f"总用时 {int(summary['elapsed_s']) // 60} 分钟"
    )
    if summary.get("peak_hours"):
        lines.append("（本次跑在网关高峰时段：工作日 09–12 / 14–18 点价格翻倍，"
                     "和别的时段比成本要打折看）")
    return lines


async def _bench(cfg, args) -> int:
    """Run an evaluation set and print one line per challenge plus a summary.

    This is the only command whose output is meant to be compared across weeks
    of work, so it does not narrate progress: it writes a JSON report and, with
    ``--baseline``, the diff an operator actually reads.
    """
    from . import bench  # noqa: PLC0415

    directory = Path(args.directory).expanduser()
    if not directory.is_dir():
        print(f"不是目录：{directory}", file=sys.stderr)
        return 2
    try:
        items = bench.load_manifest(directory)
    except (FileNotFoundError, ValueError) as exc:
        print(f"基准集读取失败：{exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        items = [item for item in items if item.name in wanted]
        if not items:
            print(f"没有匹配的条目：{args.only}", file=sys.stderr)
            return 2
    library = Path(args.library).expanduser() if args.library else None
    if library is not None and not library.exists():
        print(f"战例库不存在：{library}", file=sys.stderr)
        return 2
    notes_dir = Path(args.notes).expanduser() if args.notes else None
    if notes_dir is not None and not notes_dir.is_dir():
        print(f"笔记目录不存在：{notes_dir}", file=sys.stderr)
        return 2

    # Every worker of the benchmark is barred from the answer key and from the
    # databases beside it: the training database holds verified flags for
    # challenges already solved here — including one in this manifest — so a
    # worker that read it would have the answer without solving anything. The
    # path is sealed as a file, not as its folder: sealing the repo root would
    # deny a worker every path it might legitimately read.
    sealed = [directory.resolve(), Path(cfg.db_path).resolve()]
    sealed += [p.with_name(p.name + s) for p in list(sealed[1:]) for s in ("-wal", "-shm")]
    if library is not None:
        sealed.append(library.resolve())

    # The report is written as each item finishes, not once at the end. A
    # benchmark costs real money and a crash after the last run — printing a
    # float with ``:02d`` did exactly that — used to leave nothing behind: the
    # work directory is deleted in a ``finally``, so the only evidence of a
    # finished, solved run was its transcripts.
    out = Path(args.out).expanduser() if args.out else bench.default_report_path(directory)
    done: list[bench.BenchResult] = []

    def _flush(result: bench.BenchResult) -> None:
        done.append(result)
        try:
            bench.save_report(out, done, bench.summarise(done))
        except OSError:
            pass  # the final save below raises; a partial file is a bonus

    results, summary = await bench.run_benchmark(
        items, cfg, library=library, notes_dir=notes_dir, keep_db=args.keep_db,
        sealed_roots=sealed, on_result=_flush,
    )
    db_path = summary.get("bench_db") or "（已丢弃）"
    bench.save_report(out, results, summary)

    for line in _bench_lines(results, summary):
        print(line)
    print(f"结果：{out}")
    if not args.keep_db:
        print(f"（基准数据库已丢弃；要保留加 --keep-db，路径 {db_path}）")

    if args.baseline:
        old = bench.load_report(Path(args.baseline).expanduser())
        print("\n对比：" + args.baseline)
        for line in bench.compare(old, {"summary": summary, "results": [r.__dict__ for r in results]}):
            print(line)
    return 0 if summary["solved"] == summary["items"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
