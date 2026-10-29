"""FastAPI app: server-rendered pages, a JSON API, and the SSE event stream.

The page is deliberately server-rendered with plain forms and a small vanilla
script. A CTF run is a stream of small events that a human watches while it
happens; one SSE connection per run page plus a normal POST/redirect for actions
gives that with no build step and no client state to keep in sync with the
database.

SSE contract (the one part that has to be right):

- every event has a monotonic id from the ``events`` table;
- on connect the client may send ``Last-Event-ID``; the handler replays from
  that id, so a browser reconnect loses nothing;
- the in-process bus only wakes the reader. If the reader falls behind, it
  re-reads from the table by id rather than trusting the queue.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .backend import ClaudeSDKBackend, ScriptedBackend, resolve_cli_path
from .challenge import load_challenge_dir
from .config import REPO_ROOT, AppConfig, load_config
from .ctfd import CTFdClient, PlatformError, RemoteChallenge
from .db import Store
from .events import EventBus
from . import settings as model_settings
from .mdrender import markdown_filter
from .models import (
    BASIS_LABEL,
    CONFIDENCE_LABEL,
    FACT_KIND_LABEL,
    FLAG_STATUS_LABEL,
    ROLE_LABEL,
    RUN_STATUS_LABEL,
    TASK_STATUS_LABEL,
    Challenge,
    Competition,
    FlagStatus,
    RunStatus,
    TaskStatus,
)
from .orchestrator import RunManager
from .workspace import Workspace, probe_sandbox

HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
DOCS = HERE.parent / "docs"

TEMPLATES.env.filters["run_status"] = lambda v: RUN_STATUS_LABEL.get(str(v), str(v))
TEMPLATES.env.filters["task_status"] = lambda v: TASK_STATUS_LABEL.get(str(v), str(v))
TEMPLATES.env.filters["flag_status"] = lambda v: FLAG_STATUS_LABEL.get(str(v), str(v))
TEMPLATES.env.filters["fact_kind"] = lambda v: FACT_KIND_LABEL.get(str(v), str(v))
TEMPLATES.env.filters["confidence"] = lambda v: CONFIDENCE_LABEL.get(str(v), str(v))


#: When this process imported its modules. Anything edited after this is not
#: running, however new it looks in the editor.
_PROCESS_STARTED = time.time()


def _newest_source_mtime() -> float:
    """Newest mtime among the Python modules this process loaded.

    Only ``.py`` files: templates and static assets are read from disk on every
    request, so editing them takes effect on reload and never counts as stale.
    """
    newest = 0.0
    for path in HERE.rglob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _code_staleness() -> str:
    """A sentence for the operator when the running code is out of date.

    This has cost real confusion twice: rounds 9-14 were edited but the server
    had been started the day before, and the new ``/settings`` page answered 404
    while the code that defines it sat on disk. A 404 reads as a bug; the truth
    was a restart.
    """
    newest = _newest_source_mtime()
    if newest <= _PROCESS_STARTED + 1:
        return ""
    changed = time.strftime("%m-%d %H:%M", time.localtime(newest))
    started = time.strftime("%m-%d %H:%M", time.localtime(_PROCESS_STARTED))
    return (
        f"代码比这个进程新：最后一次改动是 {changed}，进程启动于 {started}。"
        f"当前页面和接口跑的都是旧逻辑，新加的页面会 404。"
        f"重启：start.cmd -Restart（Git Bash：./start.sh --restart）。"
    )


TEMPLATES.env.globals["code_staleness"] = _code_staleness

TEMPLATES.env.filters["role_label"] = lambda v: ROLE_LABEL.get(str(v), str(v))
TEMPLATES.env.filters["basis_label"] = lambda v: BASIS_LABEL.get(str(v), str(v))


def _rail_rows(
    store: Store, manager: RunManager, runs: list[Any] | None = None
) -> list[dict[str, Any]]:
    """One row per run for the workspace's task rail.

    The rail is the multi-task window: every run, its state, how much work is
    in flight, and the last thing that happened. Active runs sort first so the
    operator sees what is moving without scrolling past finished work.

    ``runs`` is the slice to show, already chosen by ``Store.rail_groups``.
    Which runs are on the rail is decided there, not here: a plain "newest N"
    would drop an old competition out of the window and its folder would render
    as empty.
    """
    rows: list[dict[str, Any]] = []
    distilled = store.distilled_notes()
    for run in runs if runs is not None else store.list_runs(120):
        challenge = store.get_challenge(run.challenge_id)
        flags = store.list_flags(run.id)
        confirmed = next((f for f in flags if f.status == FlagStatus.CONFIRMED), None)
        verified = next((f for f in flags if f.status == FlagStatus.VERIFIED), None)
        tasks = store.list_tasks(run.id)
        running = sum(1 for t in tasks if t.status == TaskStatus.RUNNING)
        last = store.recent_events(run.id, limit=1)
        activity = ""
        if last:
            payload = last[0].payload
            activity = str(
                payload.get("summary") or payload.get("objective") or payload.get("text")
                or payload.get("question") or payload.get("error") or payload.get("reason")
                or payload.get("value") or ""
            ).replace("\n", " ")
        rows.append(
            {
                "run": run,
                "challenge": challenge,
                "captured_flag": confirmed,
                "verified_flag": verified,
                "running_tasks": running,
                "task_count": len(tasks),
                "activity": activity[:90],
                "live": manager.get(run.id) is not None,
                # Distillation is a file write the run page cannot otherwise
                # show: the note lands in the skill library, not in this run.
                # Without the marker the operator has no way to tell a run
                # that has taught the platform from one that has not.
                "notes": distilled.get(run.id, []),
            }
        )
    order = {RunStatus.RUNNING: 0, RunStatus.NEEDS_INPUT: 1, RunStatus.QUEUED: 2}
    # `status` is a plain string from SQLite; an unknown value (an old row) sorts
    # with the finished work rather than raising on enum construction.
    rows.sort(key=lambda r: (order.get(str(r["run"].status), 3), -r["run"].id))
    return rows


def _rail_view(store: Store, manager: RunManager) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The rail's rows and its folder groups, from one decision about the window.

    Folders come in creation order and unfiled runs last under ``None`` — they
    are not a folder anyone made, and a run without one is where every run
    starts. Within a group rows keep the active-first order.

    An empty folder is still listed. It is something the operator made on
    purpose, and a folder that disappears the moment it is created reads as a
    failed creation; deleting it is one click away.

    Each group carries ``total`` (every run in the folder) next to ``rows``
    (the slice the per-folder window kept), so a heading can say "25 条中的 3
    条" instead of quietly claiming the folder is emptier than it is.
    """
    layout = store.rail_groups()
    rows = _rail_rows(store, manager, [run for _, _, runs in layout for run in runs])
    by_id = {row["run"].id: row for row in rows}

    groups: list[dict[str, Any]] = [
        {
            "folder": folder,
            "total": total,
            "rows": [by_id[run.id] for run in runs if run.id in by_id],
        }
        for folder, total, runs in layout
    ]
    return rows, groups


def _count_skills(skills_dir: Path) -> int:
    if not skills_dir.is_dir():
        return 0
    return sum(1 for d in skills_dir.iterdir() if (d / "SKILL.md").exists())


def _safe_name(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:48]
    return safe or "未命名题目"


def _derive_name(text: str) -> str:
    """A display name from the first non-empty line of pasted challenge text."""
    for line in text.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80]
    return "未命名题目"


class _UploadHolder:
    """Anything with an ``attachments`` directory (a Run or a bare workspace)."""

    def __init__(self, workspace: Any) -> None:
        self.workspace = workspace


def _workspace_context(
    store: Store,
    cfg: AppConfig,
    sandbox: Any,
    manager: RunManager,
    run_id: int | None = None,
    notice: str = "",
) -> dict[str, Any] | None:
    """Everything the workspace page needs, or None when there is no run yet.

    The page is chat-first: the selected run's conversation owns the middle,
    the rail on the left is the multi-task window, and the run's work products
    (tasks, flags, facts, writeup) live in tabs beside the chat.
    """
    rail, rail_groups = _rail_view(store, manager)
    folders = store.list_folders()
    selected = None
    if run_id is not None:
        selected = next((row for row in rail if row["run"].id == run_id), None)
    if selected is None and rail:
        selected = rail[0]
    if selected is None:
        return None

    run = selected["run"]
    challenge = selected["challenge"]
    workspace = Path(run.workspace) if run.workspace else None
    writeup = workspace / "writeup.md" if workspace else None
    counts = store.run_totals()
    live_run = manager.get(run.id)
    # A closed run can still be chatted with: the main session resumes from its
    # transcript (see ``RunManager.revive``). Only a run with no recorded
    # session at all — nothing to resume — is read-only.
    detached = (live_run is None or live_run.closed) and not run.main_session_id
    # A run that closed by blowing its dollar ceiling is the other read-only
    # case: reopening it costs one request and is closed again by the watchdog
    # before the lead can answer. Saying so before the operator types is kinder
    # than accepting the message and answering with a redirect.
    out_of_budget = (
        run.status not in ("running", "needs_input", "queued")
        and run.cost_usd > cfg.limits.run_budget_usd
    )
    return {
        "page": "workspace",
        "cfg": cfg,
        "sandbox": sandbox,
        "sessions_active": manager.pool.active,
        "rail": rail,
        "rail_groups": rail_groups,
        "folders": folders,
        "counts": counts,
        "selected": selected,
        "run": run,
        "challenge": challenge,
        "tasks": store.list_tasks(run.id),
        "facts": store.list_facts(run.id, limit=200),
        "flags": store.list_flags(run.id),
        "submissions": {f.id: store.latest_submission(f.id) for f in store.list_flags(run.id)},
        "competition": (
            store.get_competition(challenge.competition_id)
            if challenge and challenge.competition_id
            else None
        ),
        "messages": store.list_messages(run.id, limit=200),
        "events": store.recent_events(run.id, limit=120),
        "writeup_exists": bool(writeup and writeup.exists()),
        # Technique notes this run installed in the skill library. Same source
        # as the rail row (``skill.note_added``), so the header stamp and the
        # rail chip can never disagree.
        "run_notes": selected.get("notes") or [],
        # Confirming a flag no longer starts a writer (limits.writeup_on_confirm),
        # so the page needs to offer it: a confirmed flag with no report yet is
        # the one state where "写报告" applies.
        "confirmed_flag": next(
            (f for f in store.list_flags(run.id) if f.status == "confirmed"), None
        ),
        "detached": detached,
        "out_of_budget": out_of_budget,
        "notice": notice,
        "challenges": store.list_challenges(),
        "skills_count": _count_skills(cfg.skills_dir),
        "cli_path": resolve_cli_path(cfg.cli_path),
    }


def _new_task_context(
    store: Store, cfg: AppConfig, sandbox: Any, manager: RunManager, notice: str = "",
    folder_id: int | None = None, error: str = "",
) -> dict[str, Any]:
    rail, rail_groups = _rail_view(store, manager)
    folders = store.list_folders()
    return {
        "page": "new",
        "cfg": cfg,
        "sandbox": sandbox,
        "sessions_active": manager.pool.active,
        "rail": rail,
        "rail_groups": rail_groups,
        "folders": folders,
        "folder_id": folder_id,
        "counts": store.run_totals(),
        "challenges": store.list_challenges(),
        "skills_count": _count_skills(cfg.skills_dir),
        "cli_path": resolve_cli_path(cfg.cli_path),
        "notice": notice,
        "error": error,
    }


def _competition_stats(store: Store, competition: Competition) -> dict[str, Any]:
    """Scoreboard numbers for one competition.

    Computed from local state rather than from the platform's scoreboard API:
    the local records are what the operator actually did (how long it took, how
    much it cost), and the platform's numbers would not explain the cost of
    getting them.
    """
    challenges = [
        c for c in store.list_challenges() if c.competition_id == competition.id
    ]
    solved = 0
    cost = 0.0
    elapsed = 0.0
    for challenge in challenges:
        runs = store.list_runs_for_challenge(challenge.id)
        for run in runs:
            cost += run.cost_usd
            if run.started_at and run.finished_at:
                elapsed += _seconds_between(run.started_at, run.finished_at)
            if any(f.status == FlagStatus.CONFIRMED for f in store.list_flags(run.id)):
                solved += 1
                break
    return {
        "challenges": len(challenges),
        "solved": solved,
        "cost": cost,
        "elapsed_s": elapsed,
    }


def _seconds_between(start: str, end: str) -> float:
    """Seconds between two SQLite ``datetime('now')`` strings; 0 when unparsable.

    An empty ``end`` means "still going", so a running run or task measures up
    to now. Templates use that to render a live-looking clock before the
    browser takes over; app.js then re-computes it every second.
    """
    from datetime import datetime, timezone

    try:
        a = datetime.fromisoformat(start.replace("Z", "")).replace(tzinfo=timezone.utc)
        b = (
            datetime.fromisoformat(end.replace("Z", "")).replace(tzinfo=timezone.utc)
            if end
            else datetime.now(timezone.utc)
        )
    except ValueError:
        return 0.0
    return max(0.0, (b - a).total_seconds())


def _fmt_duration(seconds: float) -> str:
    """Compact Chinese duration: ``42秒`` / ``12分05秒`` / ``1小时12分``.

    Kept character-for-character in step with ``fmtDuration`` in app.js: the
    server renders the first value and the browser overwrites it once a second,
    so any difference would show up as a flicker.
    """
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s // 60}分{s % 60:02d}秒"
    return f"{s // 3600}小时{(s % 3600) // 60:02d}分"


def _fmt_clock(value: str) -> str:
    """A stored UTC timestamp as local wall-clock time.

    Today shows ``14:32``; an older date is spelled out, because "when did this
    run start" is unreadable once a second run has opened.
    """
    from datetime import datetime, timezone

    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    dt = dt.astimezone()
    now = datetime.now().astimezone()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_span(start: str, end: str) -> str:
    """``2026-10-20 14:32 → 15:04`` for a hover tooltip.

    Unambiguous where the inline clock is terse: the tooltip is what an operator
    reads when they actually need the exact minute, so both ends are dated and
    a still-running span says so instead of showing a blank right-hand side.
    """
    from datetime import datetime, timezone

    def local(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=timezone.utc).astimezone()
        except ValueError:
            return None

    a, b = local(start), local(end)
    if a is None:
        return ""
    left = a.strftime("%Y-%m-%d %H:%M")
    if b is None:
        return f"{left} 开始，仍在进行"
    if a.date() == b.date():
        return f"{left} → {b.strftime('%H:%M')}"
    return f"{left} → {b.strftime('%Y-%m-%d %H:%M')}"


TEMPLATES.env.globals["seconds_between"] = _seconds_between
TEMPLATES.env.filters["duration"] = _fmt_duration
TEMPLATES.env.filters["clock"] = _fmt_clock
TEMPLATES.env.filters["span"] = _fmt_span
# Stored chat turns carry the same Markdown the live stream renders. Both paths
# go through the same rules (``app.js renderMarkdown`` / ``mdrender``) so a
# reload or a 停止 cannot change the shape of a message the operator is reading.
TEMPLATES.env.filters["markdown"] = markdown_filter


#: Ceiling for one uploaded attachment. CTF files can be large (disk images,
#: PCAPs), but a stream that never ends must not fill the disk.
MAX_UPLOAD_BYTES = 2 << 30
UPLOAD_CHUNK = 1 << 20


async def _store_uploads(run: Any, uploads: list[UploadFile]) -> list[str]:
    """Stream uploads into the run's ``attachments/``; return stored names.

    Names are sanitised by :meth:`Workspace.reserve_upload`; the content is
    untrusted and is never executed here. A part with no filename is skipped —
    a browser sends one when the file picker was left empty. A partial file is
    removed on failure so a half-written attachment never reaches an agent.
    """
    stored: list[str] = []
    for upload in uploads:
        if not upload.filename:
            continue
        target = run.workspace.reserve_upload(upload.filename)
        size = 0
        try:
            with target.open("wb") as fh:
                while chunk := await upload.read(UPLOAD_CHUNK):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"附件过大：{upload.filename}")
                    fh.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()
        stored.append(target.name)
    return stored


def _append_challenge_files(store: Store, challenge_id: int, names: list[str]) -> None:
    """Add uploaded names to the challenge's file list (prompts read it)."""
    challenge = store.get_challenge(challenge_id)
    if challenge is None or not names:
        return
    files = list(challenge.files)
    for name in names:
        if name not in files:
            files.append(name)
    store.set_challenge_files(challenge_id, files)


def _sync_competition(store: Store, cfg: AppConfig, competition: Competition) -> str:
    """Pull every challenge from the platform; returns a one-line summary.

    Blocking (HTTP + disk), so the endpoint runs it in a thread. Solved
    challenges are recorded but their attachments are not downloaded — the
    point of a sync during a competition is to get to the unsolved ones fast.

    Re-running is safe: a challenge is matched by ``(competition_id,
    external_id)`` and updated in place, so a second sync neither duplicates
    rows nor forgets attachments the operator uploaded by hand.
    """
    client = CTFdClient(competition.base_url, competition.token)
    remote: list[RemoteChallenge] = client.list_challenges()
    created = updated = downloaded = 0
    for item in remote:
        existing = store.find_platform_challenge(competition.id, item.external_id)
        if existing is None:
            challenge = Challenge(
                id=0,
                name=item.name,
                category=item.category,
                description=item.description,
                remote=item.remote,
                competition_id=competition.id,
                external_id=item.external_id,
            )
            challenge_id = store.add_challenge(challenge)
            created += 1
        else:
            store.update_challenge_from_platform(
                existing.id,
                description=item.description,
                remote=item.remote,
                hints=[],
                name=item.name,
                category=item.category,
            )
            challenge_id = existing.id
            updated += 1
        if item.solved:
            continue
        # Attachments land next to the challenge, not in a run: the files exist
        # before a run does, and a run's workspace is created from this list.
        dest = cfg.data_dir / "platform" / f"comp-{competition.id}" / str(item.external_id)
        names: list[str] = []
        taken: set[str] = set()
        for file_url in item.files:
            target = client.download(file_url, dest, taken=taken)
            names.append(target.name)
            downloaded += 1
        if names:
            challenge = store.get_challenge(challenge_id)
            if challenge is not None:
                merged = list(challenge.files)
                for name in names:
                    if name not in merged:
                        merged.append(name)
                store.set_challenge_files(challenge_id, merged)
            store.set_challenge_source_dir(challenge_id, str(dest))
    return f"同步 {len(remote)} 道（新增 {created}，更新 {updated}），下载附件 {downloaded} 个"



def create_app(cfg: AppConfig | None = None, *, scripted: bool = False) -> FastAPI:
    cfg = cfg or load_config()
    store = Store(cfg.db_path)
    store.fail_unfinished_runs("服务重启")
    bus = EventBus(store)
    sandbox = probe_sandbox(cfg)
    backend = ScriptedBackend() if scripted else ClaudeSDKBackend(cfg)
    manager = RunManager(store, bus, cfg, backend, sandbox, cfg.skills_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await manager.stop_all()
            store.close()

    app = FastAPI(title="CTFAGENT", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.store = store
    app.state.bus = bus
    app.state.manager = manager
    app.state.sandbox = sandbox
    static_dir = HERE / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """The workspace, with the top run of the rail selected.

        The rail sorts live work first, so "/" lands on whatever is moving —
        which is what an operator wants when they open the tab. ``?run=<id>``
        picks a specific run instead. With no runs at all this is the compose
        page: the operator types the challenge and drops attachments, exactly
        as they would in a chat.
        """
        selected = request.query_params.get("run")
        run_id = int(selected) if selected and selected.isdigit() else None
        context = _workspace_context(store, cfg, sandbox, manager, run_id)
        if context is None:
            return TEMPLATES.TemplateResponse(
                request, "new.html", _new_task_context(store, cfg, sandbox, manager)
            )
        return TEMPLATES.TemplateResponse(request, "workspace.html", context)

    @app.get("/new", response_class=HTMLResponse)
    async def new_task_page(request: Request, folder: str = "", error: str = "") -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "new.html",
            _new_task_context(
                store, cfg, sandbox, manager,
                folder_id=int(folder) if folder.isdigit() else None,
                error=error,
            ),
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: int) -> HTMLResponse:
        """Kept as the canonical URL for one run; it is the workspace page."""
        if store.get_run(run_id) is None:
            raise HTTPException(404, "没有这次运行")
        context = _workspace_context(
            store, cfg, sandbox, manager, run_id, notice=request.query_params.get("notice", "")
        )
        assert context is not None
        return TEMPLATES.TemplateResponse(request, "workspace.html", context)

    @app.get("/docs", response_class=HTMLResponse)
    async def docs_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            "docs.html",
            {
                "page": "docs",
                "cfg": cfg,
                "sandbox": sandbox,
                "sessions_active": manager.pool.active,
                "cli_path": resolve_cli_path(cfg.cli_path),
                "skills_count": _count_skills(cfg.skills_dir),
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request, notice: str = "", error: str = ""
    ) -> HTMLResponse:
        """Model credentials: what is in effect, and where it came from."""
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {
                "page": "settings",
                "cfg": cfg,
                "sandbox": sandbox,
                "sessions_active": manager.pool.active,
                "cli_path": resolve_cli_path(cfg.cli_path),
                "skills_count": _count_skills(cfg.skills_dir),
                "creds": model_settings.state(cfg),
                "config_path": str(REPO_ROOT / "config.toml"),
                "notice": notice,
                "error": error,
            },
        )

    @app.post("/api/settings/credentials")
    async def save_credentials(
        base_url: str = Form(""),
        auth_token: str = Form(""),
        api_key: str = Form(""),
        clear_secrets: str = Form(""),
    ) -> RedirectResponse:
        """Write the endpoint and token to config.toml, then use them at once.

        A blank secret field means "leave it alone" — the page shows the token
        masked, so treating blank as "clear" would wipe it every time the
        operator changed only the base URL. Clearing is its own checkbox.

        The values are also applied to the running config: sessions read
        ``cfg.models`` per dispatch, so a saved token works immediately without a
        restart. On the next start the file is what loads (unless the environment
        sets one, which the page warns about).
        """
        path = REPO_ROOT / "config.toml"
        clearing = bool(clear_secrets)
        try:
            written = model_settings.write_credentials(
                path,
                base_url=base_url,
                auth_token="" if clearing else (auth_token.strip() or None),
                api_key="" if clearing else (api_key.strip() or None),
            )
        except OSError as exc:
            return RedirectResponse(
                f"/settings?error={urllib.parse.quote(f'写入失败：{exc}')}", status_code=303
            )

        cfg.models.base_url = base_url.strip()
        if clearing:
            cfg.models.auth_token = ""
            cfg.models.api_key = ""
        else:
            if auth_token.strip():
                cfg.models.auth_token = auth_token.strip()
            if api_key.strip():
                cfg.models.api_key = api_key.strip()
        cfg.models.credential_source = "toml"

        note = "已保存并生效" if written else "没有改动"
        if clearing:
            note += "（token 已清空）"
        return RedirectResponse(
            f"/settings?notice={urllib.parse.quote(note)}", status_code=303
        )

    @app.post("/api/settings/probe")
    async def probe_credentials() -> RedirectResponse:
        """Check the endpoint and token with a call that costs nothing."""
        ok, detail = await asyncio.to_thread(
            model_settings.probe,
            cfg.models.base_url,
            cfg.models.auth_token or cfg.models.api_key,
        )
        key = "notice" if ok else "error"
        return RedirectResponse(
            f"/settings?{key}={urllib.parse.quote(detail)}", status_code=303
        )

    @app.get("/competitions", response_class=HTMLResponse)
    async def competitions_page(request: Request) -> HTMLResponse:
        """Competition connections, sync controls and the local scoreboard."""
        comps = store.list_competitions()
        rows = []
        runs_by_challenge: dict[int, list[Any]] = {}
        solved_challenges: dict[int, Any] = {}
        for comp in comps:
            challenges = [
                c for c in store.list_challenges() if c.competition_id == comp.id
            ]
            for challenge in challenges:
                runs = store.list_runs_for_challenge(challenge.id)
                runs_by_challenge[challenge.id] = runs
                for run in runs:
                    confirmed = next(
                        (f for f in store.list_flags(run.id) if f.status == FlagStatus.CONFIRMED),
                        None,
                    )
                    if confirmed is not None:
                        solved_challenges[challenge.id] = confirmed
                        break
            rows.append(
                {
                    "competition": comp,
                    "stats": _competition_stats(store, comp),
                    "challenges": challenges,
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "competitions.html",
            {
                "page": "competitions",
                "cfg": cfg,
                "sandbox": sandbox,
                "sessions_active": manager.pool.active,
                "skills_count": _count_skills(cfg.skills_dir),
                "cli_path": resolve_cli_path(cfg.cli_path),
                "rows": rows,
                "runs_by_challenge": runs_by_challenge,
                "solved_challenges": solved_challenges,
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    # ----------------------------------------------------------------- API
    @app.post("/api/challenges")
    async def add_challenge(directory: str = Form(...)) -> RedirectResponse:
        path = Path(directory).expanduser()
        if not path.is_dir():
            raise HTTPException(400, f"不是目录：{path}")
        challenge = load_challenge_dir(path)
        store.add_challenge(challenge)
        return RedirectResponse("/", status_code=303)

    # ----------------------------------------------------------- competitions
    @app.post("/api/competitions")
    async def add_competition(
        name: str = Form(...), base_url: str = Form(...), token: str = Form(...)
    ) -> RedirectResponse:
        """Connect a platform account; the token is checked before it is stored."""
        base_url = base_url.strip().rstrip("/")
        token = token.strip()
        if not base_url or not token:
            return RedirectResponse("/competitions?error=地址和 token 都要填", status_code=303)
        try:
            who = await asyncio.to_thread(CTFdClient(base_url, token).whoami)
        except PlatformError as exc:
            return RedirectResponse(
                f"/competitions?error={urllib.parse.quote(f'连接失败：{exc}')}", status_code=303
            )
        comp = store.add_competition(name.strip() or base_url, "ctfd", base_url, token)
        store.note_sync(comp.id, f"已连接为 {who.get('name') or who.get('id')}")
        return RedirectResponse("/competitions", status_code=303)

    @app.post("/api/competitions/{competition_id}/sync")
    async def sync_competition(competition_id: int) -> RedirectResponse:
        """Pull unsolved challenges and their attachments, then report.

        Runs in a worker thread: one detail request per challenge plus one
        download per file would otherwise block the event loop for the length
        of the whole sync, freezing every open workspace page.
        """
        comp = store.get_competition(competition_id)
        if comp is None:
            raise HTTPException(404, "没有这场比赛")
        try:
            note = await asyncio.to_thread(_sync_competition, store, cfg, comp)
        except PlatformError as exc:
            store.note_sync(competition_id, f"同步失败：{exc}")
            return RedirectResponse(
                f"/competitions?error={urllib.parse.quote(f'同步失败：{exc}')}", status_code=303
            )
        store.note_sync(competition_id, note)
        return RedirectResponse(
            f"/competitions?notice={urllib.parse.quote(note)}", status_code=303
        )

    @app.post("/api/competitions/{competition_id}/delete")
    async def delete_competition(competition_id: int) -> RedirectResponse:
        """Forget a connection. Synced challenges and their runs are kept."""
        store.delete_competition(competition_id)
        return RedirectResponse("/competitions", status_code=303)

    @app.post("/api/competitions/{competition_id}/start")
    async def start_competition_runs(
        competition_id: int,
        challenge_id: int = Form(0),
    ) -> RedirectResponse:
        """Start runs for synced challenges that have none yet.

        A challenge that already has a run is skipped, so this is safe to press
        again after a partial sync. ``challenge_id`` starts exactly one
        challenge (the per-row 开跑 button).
        """
        comp = store.get_competition(competition_id)
        if comp is None:
            raise HTTPException(404, "没有这场比赛")
        started = 0
        skipped = 0
        for challenge in store.list_challenges():
            if challenge.competition_id != competition_id:
                continue
            if challenge_id and challenge.id != challenge_id:
                continue
            runs = store.list_runs_for_challenge(challenge.id)
            if runs:
                skipped += 1
                continue
            await manager.start(challenge.id, challenge.description[:200])
            started += 1
        if challenge_id and started == 0:
            note = "这道题已经开跑过，从列表里打开即可"
        else:
            note = f"已开跑 {started} 道，跳过 {skipped} 道"
        return RedirectResponse(
            f"/competitions?notice={urllib.parse.quote(note)}", status_code=303
        )

    @app.post("/api/tasks")
    async def create_task(
        description: str = Form(...),
        title: str = Form(""),
        category: str = Form(""),
        remote: str = Form(""),
        flag_format: str = Form(""),
        objective: str = Form(""),
        folder_id: str = Form(""),
        files: list[UploadFile] = File(default=[]),
    ) -> RedirectResponse:
        """Start a run from pasted challenge text plus uploaded files.

        This is the chat-first path: no directory, no manifest. The text *is*
        the challenge description, and the attachments are streamed into the
        run's workspace before the main loop starts, so the kickoff prompt
        already lists them.
        """
        text = description.strip()
        if not text:
            raise HTTPException(400, "题目内容不能为空")
        name = title.strip() or _derive_name(text)
        challenge = Challenge(
            id=0,
            name=name,
            category=(category.strip() or "unknown"),
            description=text,
            files=[],
            remote=remote.strip(),
            flag_format=flag_format.strip(),
            hints=[],
            time_limit_s=cfg.limits.run_timeout_s,
            source_dir="",
        )
        target = int(folder_id) if folder_id.strip().isdigit() else None
        if target is not None and store.get_folder(target) is None:
            # The picker only offers real folders, so this is a stale page: the
            # folder was deleted in another tab. Filing the run under a dead id
            # would make it invisible on the rail, where no group claims it.
            target = None
        run = manager.prepare(challenge, objective.strip(), folder_id=target)
        stored = await _store_uploads(run, files)
        _append_challenge_files(store, challenge.id, stored)
        # The operator's first message is what opened this run. Recording it
        # before launch means the conversation starts with their own words
        # instead of the agent answering an invisible prompt.
        run.note_operator_message(text, attachments=stored)
        manager.launch(run)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    @app.post("/api/runs/{run_id}/attachments")
    async def add_attachments(run_id: int, files: list[UploadFile] = File(...)) -> RedirectResponse:
        """Attach more files to a run that is already going.

        The worker sessions see the file on disk immediately; the main agent is
        told in its chat so it can dispatch against it rather than guessing
        from the old file list in its prompt.
        """
        row = store.get_run(run_id)
        if row is None:
            raise HTTPException(404, "没有这次运行")
        challenge = store.get_challenge(row.challenge_id)
        if challenge is None:
            raise HTTPException(404, "没有这道题")
        live = manager.get(run_id)
        workspace = live.workspace if live is not None else Workspace.existing(
            cfg.data_dir, run_id, challenge.name
        )
        holder = _UploadHolder(workspace)
        stored = await _store_uploads(holder, files)
        if not stored:
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        _append_challenge_files(store, challenge.id, stored)
        bus.publish(run_id, "run.attachments", {"files": stored})
        if live is not None and not live.closed:
            await live.chat(
                "操作员给这道题追加了附件："
                + "、".join(stored)
                + f"，放在 {live.workspace.attachments}，请一并考虑。"
            )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/api/runs/{run_id}/attachments")
    async def list_attachments(run_id: int) -> JSONResponse:
        row = store.get_run(run_id)
        if row is None:
            raise HTTPException(404, "没有这次运行")
        challenge = store.get_challenge(row.challenge_id)
        live = manager.get(run_id)
        root = (
            live.workspace.attachments if live is not None
            else cfg.data_dir / f"run-{run_id}" / _safe_name(challenge.name if challenge else "") / "attachments"
        )
        items = []
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if path.is_file():
                    items.append({"name": path.name, "size": path.stat().st_size})
        return _json({"attachments": items})

    @app.get("/api/runs/{run_id}/attachments/{name}")
    async def download_attachment(run_id: int, name: str) -> FileResponse:
        row = store.get_run(run_id)
        if row is None:
            raise HTTPException(404, "没有这次运行")
        challenge = store.get_challenge(row.challenge_id)
        live = manager.get(run_id)
        root = (
            live.workspace.attachments if live is not None
            else cfg.data_dir / f"run-{run_id}" / _safe_name(challenge.name if challenge else "") / "attachments"
        ).resolve()
        # The name comes from the URL: resolve and confirm containment rather
        # than trusting a sanitised basename alone.
        target = (root / Path(name).name).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(404, "没有这个附件")
        return FileResponse(target, filename=target.name)

    @app.post("/api/runs")
    async def start_run(
        challenge_id: int = Form(...), objective: str = Form(""), folder_id: str = Form("")
    ) -> RedirectResponse:
        target = int(folder_id) if folder_id.strip().isdigit() else None
        if target is not None and store.get_folder(target) is None:
            target = None  # same stale-picker case as /api/tasks
        run = await manager.start(challenge_id, objective, folder_id=target)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    @app.post("/api/runs/{run_id}/chat")
    async def chat(
        run_id: int, request: Request, text: str = Form(""), files: list[UploadFile] = File(default=[])
    ) -> Any:
        """One operator turn: text, attachments, or both.

        Attachments are stored first and named in the message, so the main
        agent learns about them in the same turn it is asked to act on them.
        """
        row = store.get_run(run_id)
        if row is None:
            raise HTTPException(404, "没有这次运行")
        run = manager.get(run_id)
        stored: list[str] = []
        challenge = store.get_challenge(row.challenge_id)
        if files:
            workspace = (
                run.workspace if run is not None
                else Workspace.existing(cfg.data_dir, run_id, challenge.name if challenge else "")
            )
            stored = await _store_uploads(_UploadHolder(workspace), files)
            if stored and challenge is not None:
                _append_challenge_files(store, challenge.id, stored)
                bus.publish(run_id, "run.attachments", {"files": stored})
        if run is None or run.closed:
            # A closed run's conversation is on disk and the CLI can resume it,
            # so the chat box stays usable: bring the lead back rather than
            # refusing the message. The files are already stored by now.
            #
            # One exception: a run that closed by spending past its dollar
            # ceiling cannot be answered. Reopening it sends one request and the
            # watchdog closes it again six seconds later (run 18), so the
            # operator sees a message with no reply. Refuse and say why instead.
            spent_out = row.cost_usd > cfg.limits.run_budget_usd
            if spent_out:
                return RedirectResponse(
                    f"/runs/{run_id}?notice=" + urllib.parse.quote(
                        f"无法继续对话：这次运行已花 ${row.cost_usd:.2f}，超过 "
                        f"${cfg.limits.run_budget_usd:.2f} 预算。新开一次运行，"
                        f"或在 config.toml 调高 limits.run_budget_usd。"
                    ),
                    status_code=303,
                )
            revived = await manager.revive(run_id)
            if revived is None or not await revived.wait_until_listening():
                reason = (revived._session_error if revived is not None else "") or \
                    "没有可恢复的主 agent 会话"
                return RedirectResponse(
                    f"/runs/{run_id}?notice={urllib.parse.quote(f'无法继续对话：{reason}')}",
                    status_code=303,
                )
            run = revived
            # Nothing to send yet: the operator's text goes below, but a bare
            # wake-up is not needed — reopening already starts the loop.
        message = text.strip()
        if stored:
            names = "、".join(stored)
            message = (
                f"{message}\n\n[附件已加入 attachments/：{names}]" if message
                else f"我上传了附件：{names}。它们在工作区的 attachments/ 目录里，请据此继续。"
            )
        if not message:
            return RedirectResponse(f"/runs/{run_id}", status_code=303)
        await run.chat(message, attachments=stored, display=text.strip())
        if request.headers.get("x-requested-with") == "fetch":
            # The page appends the bubble itself; a redirect would make fetch
            # pull the whole workspace document back for nothing.
            return _json({"ok": True, "stored": stored})
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/api/runs/{run_id}/delete")
    async def delete_run(run_id: int) -> RedirectResponse:
        """Forget a run: stop it if live, then drop its rows and workspace.

        The challenge stays registered — an operator retrying a challenge wants
        the same challenge row, just without the old attempt in the rail.
        """
        if store.get_run(run_id) is None:
            raise HTTPException(404, "没有这次运行")
        await manager.delete(run_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/challenges/{challenge_id}/delete")
    async def delete_challenge(challenge_id: int) -> RedirectResponse:
        """Remove a registered challenge, refused while any of its runs remain."""
        if store.get_challenge(challenge_id) is None:
            raise HTTPException(404, "没有这道题")
        if store.list_runs_for_challenge(challenge_id):
            raise HTTPException(400, "这道题还有运行记录，先删除运行")
        store.delete_challenge(challenge_id)
        return RedirectResponse("/new", status_code=303)

    @app.post("/api/folders")
    async def add_folder(name: str = Form(...), redirect: str = Form("/new")) -> RedirectResponse:
        """Create a folder and land on it, ready to start the first run there."""
        target = "/new" if redirect == "/new" else "/"
        try:
            folder = store.add_folder(name)
        except ValueError as exc:
            # A duplicate name: the operator stays on the page with the message.
            # Nothing was created, so there is no folder id to keep.
            return RedirectResponse(f"/new?error={urllib.parse.quote(str(exc))}", status_code=303)
        return RedirectResponse(f"{target}?folder={folder.id}", status_code=303)

    @app.post("/api/folders/{folder_id}/rename")
    async def rename_folder(folder_id: int, name: str = Form(...)) -> RedirectResponse:
        if store.get_folder(folder_id) is None:
            raise HTTPException(404, "没有这个文件夹")
        try:
            store.rename_folder(folder_id, name)
        except ValueError as exc:
            return RedirectResponse(
                f"/new?folder={folder_id}&error={urllib.parse.quote(str(exc))}", status_code=303
            )
        return RedirectResponse(f"/new?folder={folder_id}", status_code=303)

    @app.post("/api/folders/{folder_id}/delete")
    async def delete_folder(folder_id: int) -> RedirectResponse:
        """Remove the grouping; the runs inside it become unfiled, never deleted."""
        store.delete_folder(folder_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/runs/{run_id}/folder")
    async def move_run(run_id: int, folder_id: str = Form("")) -> RedirectResponse:
        """File a run under a folder, or back to 未分类 with an empty value."""
        if store.get_run(run_id) is None:
            raise HTTPException(404, "没有这次运行")
        target = int(folder_id) if folder_id.strip().isdigit() else None
        if target is not None and store.get_folder(target) is None:
            raise HTTPException(404, "没有这个文件夹")
        store.set_run_folder(run_id, target)
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: int, reason: str = Form("操作员停止")) -> RedirectResponse:
        if manager.get(run_id) is not None:
            await manager.stop(run_id, reason)
        else:
            # The run exists in the database but not in this process (a restart,
            # or a second server instance). Closing it out is still correct.
            store.fail_running_tasks(run_id, "stopped", reason)
            store.set_run_status(run_id, RunStatus.STOPPED, summary=reason, reason=reason)
            bus.publish(run_id, "run.finished", {"run_id": run_id, "status": "stopped", "reason": reason})
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.post("/api/flags/{flag_id}/confirm")
    async def confirm_flag(flag_id: int, note: str = Form("")) -> RedirectResponse:
        flag = store.get_flag(flag_id)
        if flag is None:
            raise HTTPException(404, "没有这个 flag")
        # A live Run is the one that can dispatch the writer. Note that a paused
        # run is *not* accepting work — confirmation is exactly how it resumes —
        # so the test is "a Run object exists", not `accepting_work`.
        run = manager.get(flag.run_id)
        if run is not None and not run.closed:
            await run.confirm_flag(flag_id, note)
        else:
            # No live run object (finished, restarted, or the main agent already
            # closed it): the operator's decision is still recorded.
            store.set_flag_status(flag_id, "confirmed", note or "操作员确认")
            store.set_run_status(
                flag.run_id, RunStatus.SOLVED, summary=f"flag 已确认：{flag.value}"
            )
            bus.publish(
                flag.run_id,
                "flag.confirmed",
                {"flag_id": flag_id, "value": flag.value, "note": note},
            )
        return RedirectResponse(f"/runs/{flag.run_id}", status_code=303)

    @app.post("/api/runs/{run_id}/writeup")
    async def write_report(run_id: int) -> RedirectResponse:
        """Write the report for a run that is already closed.

        Confirmation does not start a writer any more: in a competition the next
        challenge matters more than the writeup. The report is produced here,
        on request, against the same finished run — only a writer session is
        started, not the lead.
        """
        # ``attach`` rebuilds a Run for a finished run whose object this process
        # no longer holds; without it the button would only work while the run
        # was still live, which is exactly when nobody needs it.
        run = manager.get(run_id) or manager.attach(run_id)
        if run is None:
            raise HTTPException(404, "没有这次运行")
        out = await run.write_report()
        if out.get("error"):
            note = str(out["error"])
        elif out.get("written"):
            note = f"报告已写入 {out.get('path')}"
        else:
            note = "报告任务已结束，但没有产出 writeup.md"
        return RedirectResponse(
            f"/runs/{run_id}?notice=" + urllib.parse.quote(note), status_code=303
        )

    @app.post("/api/runs/{run_id}/distill")
    async def distill_run(run_id: int) -> RedirectResponse:
        """Distil a solved run into a technique note, on request.

        The counterpart of the 写报告 button for the other kind of memory: the
        case card remembers the challenge, the note remembers the method and is
        what makes the next challenge of the same shape cheaper. Explicit for
        the same reason — it spends a writer session.
        """
        run = manager.get(run_id) or manager.attach(run_id)
        if run is None:
            raise HTTPException(404, "没有这次运行")
        out = await run.distill_technique()
        if out.get("error"):
            note = str(out["error"])
        elif out.get("written"):
            note = f"笔记已写入 {out.get('path')}（方向：{out.get('skill')}）"
        else:
            note = "笔记任务已结束，但没有产出可安装的笔记"
        return RedirectResponse(
            f"/runs/{run_id}?notice=" + urllib.parse.quote(note), status_code=303
        )

    @app.post("/api/flags/{flag_id}/submit")
    async def submit_flag(flag_id: int) -> RedirectResponse:
        """Send a confirmed flag to the platform the challenge came from.

        Deliberately a separate click from confirmation: confirmation is the
        operator's judgement that the flag is right, and submission is the
        outward-facing act of telling the scoreboard. Merging them would make
        one button do both, which is exactly the automation this system avoids.

        A wrong answer is recorded and shown, not raised — the operator needs to
        see the platform's message to decide whether the challenge text or the
        flag was wrong.
        """
        flag = store.get_flag(flag_id)
        if flag is None:
            raise HTTPException(404, "没有这个 flag")
        if flag.status != FlagStatus.CONFIRMED:
            raise HTTPException(409, "确认之后才能提交")
        run = store.get_run(flag.run_id)
        challenge = store.get_challenge(run.challenge_id) if run else None
        if challenge is None or not challenge.competition_id:
            raise HTTPException(409, "这道题不是从比赛平台同步来的")
        comp = store.get_competition(challenge.competition_id)
        if comp is None or not comp.configured:
            raise HTTPException(409, "比赛连接已失效，请到「比赛」页重新配置")
        try:
            result = await asyncio.to_thread(
                CTFdClient(comp.base_url, comp.token).submit,
                challenge.external_id,
                flag.value,
            )
        except PlatformError as exc:
            store.add_submission(
                flag.id, comp.id, challenge.external_id, "error", str(exc)
            )
            return RedirectResponse(
                f"/runs/{flag.run_id}?notice={urllib.parse.quote(f'提交失败：{exc}')}",
                status_code=303,
            )
        store.add_submission(
            flag.id, comp.id, challenge.external_id, result.status, result.message
        )
        bus.publish(
            flag.run_id,
            "flag.submitted",
            {
                "flag_id": flag.id,
                "value": flag.value,
                "status": result.status,
                "message": result.message,
                "competition": comp.name,
            },
        )
        note = {
            "correct": "平台判定正确",
            "already_solved": "平台显示此题此前已解出",
            "incorrect": "平台判定错误",
        }.get(result.status, f"平台返回 {result.status}")
        return RedirectResponse(
            f"/runs/{flag.run_id}?notice={urllib.parse.quote(f'提交结果：{note}')}",
            status_code=303,
        )

    @app.post("/api/flags/{flag_id}/reject")
    async def reject_flag(flag_id: int, note: str = Form("")) -> RedirectResponse:
        flag = store.get_flag(flag_id)
        if flag is None:
            raise HTTPException(404, "没有这个 flag")
        store.set_flag_status(flag_id, "rejected", note or "操作员驳回")
        run = manager.get(flag.run_id)
        if run is not None:
            await run.chat(
                f"操作员驳回了候选 flag {flag.value}。"
                f"理由：{note or '（未填写）'}。不要再提交它。"
            )
        return RedirectResponse(f"/runs/{flag.run_id}", status_code=303)

    @app.get("/api/runs")
    async def list_runs() -> JSONResponse:
        # The challenge name rides along because every consumer of this list is a
        # picker that shows it: the rail re-renders from HTML, but the command
        # palette (app.js) has only a run id to offer without it, and "#121" is
        # not a searchable name.
        challenges = {c.id: c for c in store.list_challenges()}
        rows = []
        for r in store.list_runs(200):
            row = dict(r.__dict__)
            c = challenges.get(r.challenge_id)
            row["challenge_name"] = c.name if c else ""
            row["category"] = c.category if c else ""
            rows.append(row)
        return _json(rows)

    @app.get("/api/runs/{run_id}/fragments", response_class=HTMLResponse)
    async def run_fragments(request: Request, run_id: int) -> HTMLResponse:
        """The two regions the run page redraws after a confirm.

        Confirming a flag used to reload the page. It no longer does: the
        operator has just clicked a button and watched the word land, and
        throwing the page away to redraw one card discards that. So the two
        regions that actually change are re-rendered here — the flag list (the
        buttons go, the stamp changes) and the header action row (写报告 and
        沉淀笔记 appear once a flag is confirmed).

        Both come back in one response so they cannot land a poll apart, and
        both are rendered from the same templates the page uses, so neither can
        drift from what a full reload would have produced.
        """
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "没有这次运行")
        challenge = (
            store.get_challenge(run.challenge_id) if run.challenge_id else None
        )
        flags = store.list_flags(run.id)
        # `notes` is the rail's own computation; reuse it rather than repeating
        # the skill-library lookup here and letting the two disagree.
        rail, _ = _rail_view(store, manager)
        row = next((r for r in rail if r["run"].id == run_id), None)
        workspace = Path(run.workspace) if run.workspace else None
        writeup = workspace / "writeup.md" if workspace else None
        return TEMPLATES.TemplateResponse(
            request,
            "_run_fragments.html",
            {
                "run": run,
                "challenge": challenge,
                "flags": flags,
                "submissions": {f.id: store.latest_submission(f.id) for f in flags},
                "competition": (
                    store.get_competition(challenge.competition_id)
                    if challenge and challenge.competition_id
                    else None
                ),
                "run_notes": (row or {}).get("notes") or [],
                "confirmed_flag": next(
                    (f for f in flags if f.status == "confirmed"), None
                ),
                "writeup_exists": bool(writeup and writeup.exists()),
                "folders": store.list_folders(),
            },
        )

    @app.get("/api/rail", response_class=HTMLResponse)
    async def rail(request: Request) -> HTMLResponse:
        """The rail fragment, re-rendered server-side.

        Polled by the workspace page. Re-rendering on the server keeps the rail
        and the page's own markup in one place — a JS copy of the stamp logic
        would drift the first time a state is added.
        """
        active = request.query_params.get("active") or ""
        rows, groups = _rail_view(store, manager)
        return TEMPLATES.TemplateResponse(
            request,
            "_rail.html",
            {
                "rail": rows,
                "rail_groups": groups,
                "folders": store.list_folders(),
                "active_id": int(active) if active.isdigit() else 0,
            },
        )

    @app.get("/api/challenges")
    async def list_challenges() -> JSONResponse:
        return _json({"challenges": [c.__dict__ for c in store.list_challenges()]})

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: int) -> JSONResponse:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "没有这次运行")
        challenge = store.get_challenge(run.challenge_id)
        return _json(
            {
                "run": run.__dict__,
                "challenge": challenge.__dict__ if challenge else None,
                "tasks": [t.__dict__ for t in store.list_tasks(run_id)],
                "facts": [f.__dict__ for f in store.list_facts(run_id)],
                "flags": [f.__dict__ for f in store.list_flags(run_id)],
            }
        )

    @app.get("/api/runs/{run_id}/live")
    async def run_live(run_id: int) -> JSONResponse:
        """What the running workers last did, for the chat pane's activity strip.

        The lead may be silent for many minutes while workers run; without this
        the page looks dead even though tool calls are streaming. Polled, not
        pushed, so a reload or a detached page needs no extra bookkeeping.
        """
        live = manager.get(run_id)
        tasks = live.live_tasks() if live is not None and not live.closed else []
        return _json({"tasks": tasks})

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: int, after: int = 0, limit: int = 500) -> JSONResponse:
        return _json({"events": [e.__dict__ for e in store.events_since(run_id, after, limit)]})

    @app.get("/api/runs/{run_id}/events/stream")
    async def event_stream(run_id: int, request: Request, once: bool = False) -> StreamingResponse:
        """The live feed. `since` and `Last-Event-ID` are both accepted.

        The browser's EventSource sets ``Last-Event-ID`` itself on reconnect;
        ``since`` is what the page passes on first load (it renders a backlog
        server-side and must not replay it twice).
        """
        last = _last_event_id(request)
        return StreamingResponse(
            _event_stream(app, run_id, last, once=once),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/runs/{run_id}/writeup")
    async def get_writeup(run_id: int) -> PlainTextResponse:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "没有这次运行")
        path = Path(run.workspace) / "writeup.md" if run.workspace else None
        if path is None or not path.exists():
            raise HTTPException(404, "报告还没生成")
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return _json(
            {
                "ok": True,
                "sandbox": sandbox.__dict__,
                "cli": resolve_cli_path(cfg.cli_path),
                "sessions_active": manager.pool.active,
                "max_sessions": cfg.limits.max_sessions,
            }
        )

    return app


def _json(payload: Any) -> JSONResponse:
    """JSON with datetimes/enums stringified: the API is for humans and tests."""
    return JSONResponse(json.loads(json.dumps(payload, default=str, ensure_ascii=False)))


def _last_event_id(request: Request) -> int:
    header = request.headers.get("last-event-id") or request.query_params.get("since") or "0"
    try:
        return int(header)
    except ValueError:
        return 0


async def _event_stream(
    app: FastAPI, run_id: int, last_id: int, once: bool = False
) -> AsyncIterator[bytes]:
    """Replay from ``last_id``, then follow the live bus.

    The replay loop and the live loop share a cursor so a reconnect that lands
    mid-replay cannot duplicate or skip an event. ``once`` stops after the
    replay — the live stream is intentionally endless, which makes it awkward to
    test and to read with curl.
    """
    store: Store = app.state.store
    bus: EventBus = app.state.bus
    queue = bus.subscribe(run_id)
    cursor = last_id
    try:
        yield b": connected\n\n"
        while True:
            batch = store.events_since(run_id, cursor, limit=200)
            for event in batch:
                cursor = event.id
                yield event.sse().encode("utf-8")
            if batch and len(batch) == 200:
                continue
            if once:
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            if event.id == 0:
                # Live-only (a streaming token): never persisted, so it must not
                # move the cursor — the finished message carries the same text.
                yield event.sse().encode("utf-8")
                continue
            if event.id <= cursor:
                continue
            cursor = event.id
            yield event.sse().encode("utf-8")
    except asyncio.CancelledError:
        raise
    finally:
        bus.unsubscribe(run_id, queue)


app = create_app
