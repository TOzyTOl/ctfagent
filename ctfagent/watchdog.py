"""Out-of-process watchdog: reap zombie runs the in-process clocks cannot see.

The run 82/88 incidents proved the in-process watchdog dies with the run's own
task tree — run 82 sat ``running`` for half a day, run 88's verifier froze for
18 minutes, and in both cases every per-run clock was as dead as the run. This
module reads the DATABASE, not any process memory: a run whose event stream
went silent past every in-process clock is a zombie whoever killed it, and the
fix is the same whoever applies it.

Single pass, exit code reports what happened. Designed to be driven by a
scheduler (ZCode cron / Task Scheduler) every few minutes:

    python -m ctfagent.watchdog --api http://127.0.0.1:8788

Per running run whose last event is older than ``--silence-minutes``:

1. Non-terminal task rows are reaped (stopped, marked as watchdog reapings) —
   the same thing ``Run.reopen`` does for restart ghosts, because after the
   fix they are ghosts too.
2. The run is marked interrupted with an honest reason.
3. If the API is reachable, the run's session is alive and the budget is not
   exhausted, one operator-voice nudge is posted: the server revives the run
   and the lead continues from its own transcript. At most
   ``--max-revives`` nudges per run per silence episode (state file), so a
   permanently broken gateway cannot turn into an endless revive loop.

``needs_input`` and ``queued`` are skipped: waiting for a human and waiting
for a session slot are legal states, not hangs.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config
from .db import Store
from .models import TaskStatus

#: Silence past this means the run's own task tree is dead. Must stay well
#: above every in-process clock (tool wait 900 s, task timeout 1800 s, the
#: zombie reaper's 1200 s) so the external reaper only ever fires on runs the
#: process has already given up on — or on processes that died outright.
DEFAULT_SILENCE_S = 2700

REAP_REASON = "看门狗(外部)：进程退出或事件静默"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_db_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def last_event_age_s(store: Store, run_id: int, now: datetime) -> float | None:
    """Seconds since the run's last event, or None if it never emitted one."""
    row = store._row(  # noqa: SLF001 - the watchdog is infrastructure, same tier
        "SELECT created_at FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1",
        (run_id,),
    )
    created = _parse_db_time(row["created_at"]) if row else None
    if created is None:
        return None
    return (now - created).total_seconds()


def reap_run(store: Store, run_id: int) -> int:
    """Close a zombie run and its ghost tasks. Returns the reaped task count.

    Same surgery ``Run.reopen`` performs on restart ghosts, applied from
    outside the process: non-terminal task rows become stopped with an honest
    error, the run becomes interrupted, and both land in the event stream so
    the operator and the revived lead read the same story.
    """
    reaped = 0
    for task in store.list_tasks(run_id):
        if task.terminal:
            continue
        store.finish_task(
            task.id,
            TaskStatus.STOPPED,
            {"summary": "看门狗(外部)收割：进程退出或事件静默"},
            REAP_REASON,
        )
        store.add_event(
            run_id,
            "task.finished",
            {"task_id": task.id, "role": task.role, "status": TaskStatus.STOPPED,
             "error": REAP_REASON},
            task_id=task.id,
        )
        reaped += 1
    store.set_run_status(
        run_id, "interrupted",
        summary="看门狗(外部)：事件流静默，运行已收割并中断",
        reason=REAP_REASON,
    )
    store.add_event(
        run_id,
        "run.finished",
        {"run_id": run_id, "status": "interrupted", "reason": REAP_REASON,
         "reaped_tasks": reaped, "by": "external-watchdog"},
    )
    return reaped


def post_chat(api_base: str, run_id: int, text: str, timeout: float = 30.0) -> bool:
    """Post one operator-voice message; the server revives the run for it."""
    from urllib.parse import urlencode

    data = urlencode({"text": text}).encode()
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/api/runs/{run_id}/chat", data=data, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def api_alive(api_base: str, timeout: float = 5.0) -> bool:
    try:
        urllib.request.urlopen(f"{api_base.rstrip('/')}/api/health", timeout=timeout)
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def run_once(
    db_path: Path,
    api_base: str,
    *,
    silence_s: int = DEFAULT_SILENCE_S,
    max_budget: float = 30.0,
    state_path: Path | None = None,
    revive: bool = True,
) -> dict:
    """One pass. Returns a report dict for the caller to print or log."""
    store = Store(db_path)
    now = _utcnow()
    state_path = state_path or Path(str(db_path) + ".watchdog.json")
    state = load_state(state_path)
    report: dict = {"checked": 0, "reaped": [], "revived": [], "skipped": []}

    for run in store.list_runs(limit=200):
        if run.status not in ("running", "queued"):
            continue
        report["checked"] += 1
        age = last_event_age_s(store, run.id, now)
        if age is None or age <= silence_s:
            continue
        # A queued run going silent means the whole queue is stalled; reaping
        # its (probably zero) tasks is safe but reviving it is meaningless.
        reaped = reap_run(store, run.id)
        report["reaped"].append({"run_id": run.id, "silent_s": round(age), "reaped_tasks": reaped})
        if run.status != "running" or not revive:
            continue

        entry = state.setdefault(str(run.id), {"revives": 0})
        if entry["revives"] >= 2:
            report["skipped"].append({"run_id": run.id, "why": "复活次数用尽"})
            continue
        if run.cost_usd >= max_budget:
            report["skipped"].append({"run_id": run.id, "why": "预算耗尽，复活只会立刻再关"})
            continue
        if not api_alive(api_base):
            report["skipped"].append({"run_id": run.id, "why": "服务不可达，无法复活"})
            continue
        ok = post_chat(
            api_base, run.id,
            "（看门狗自动消息）这次运行的事件流已静默超时，遗留任务已收割，会话已恢复。"
            "请先读黑板确认地面：有可推进的线索就继续；没有就调用 finish_run 收尾，"
            "不要为同样的目标重新派工。",
        )
        if ok:
            entry["revives"] += 1
            entry["last_revive"] = _utcnow().isoformat(timespec="seconds")
            report["revived"].append({"run_id": run.id})
    save_state(state_path, state)
    store.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="运行数据库（默认读 config.toml）")
    parser.add_argument("--api", default="http://127.0.0.1:8788", help="CTFAGENT 服务地址")
    parser.add_argument("--silence-minutes", type=int, default=DEFAULT_SILENCE_S // 60)
    parser.add_argument("--max-budget", type=float, default=30.0)
    parser.add_argument("--state", type=Path, default=None)
    args = parser.parse_args(argv)

    db_path = args.db
    if db_path is None:
        db_path = load_config().db_path
    report = run_once(
        db_path, args.api,
        silence_s=args.silence_minutes * 60,
        max_budget=args.max_budget,
        state_path=args.state or Path(str(db_path) + ".watchdog.json"),
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
