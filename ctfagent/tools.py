"""Orchestration tools exposed to the main agent as an in-process MCP server.

The main agent's job is conversation, decomposition and aggregation. Its only
write actions are: dispatch a worker, write a fact, propose a flag, ask the
human, and finish the run. Everything else is read-only. That asymmetry is
deliberate (ARTEX enforces the same split by tool sets, not prompts): the
agent that talks to the human should not be the one running exploits, and the
agent running exploits should not be able to declare victory.

Results are returned as JSON text so the model reads them without a schema
mismatch, and every call appends an event so the UI can show the dispatch.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from .models import CLOSING_ROLES, ROLES, grounding_gap, negative_gap

if TYPE_CHECKING:  # pragma: no cover
    from .orchestrator import Run


TOOL_NAMES = [
    "dispatch_task",
    "wait_tasks",
    "list_tasks",
    "get_task_result",
    "read_blackboard",
    "record_fact",
    "record_unknown",
    "propose_flag",
    "ask_user",
    "finish_run",
]


def tool_specs() -> list[dict[str, Any]]:
    """Anthropic-shaped tool specs for the orchestrator server."""
    return [
        {
            "name": "dispatch_task",
            "description": (
                "Dispatch one worker task to a direction specialist. Returns immediately with a "
                "task_id. Dispatch several in parallel when the directions are independent. "
                "The worker gets the objective, the context you pass, the challenge files, and the "
                "blackboard digest; it reports back through structured tools."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": list(ROLES)},
                    "objective": {"type": "string", "description": "One concrete goal, with the expected output."},
                    "context": {
                        "type": "string",
                        "description": "What this worker needs to know: prior facts, hypotheses, exact paths/URLs.",
                    },
                    "resume_task_id": {
                        "type": "integer",
                        "description": (
                            "Id of an earlier task of the same role that ended status=exhausted. The new "
                            "worker resumes that session's transcript instead of starting over. Use it "
                            "when the earlier worker stopped on a budget/turn ceiling mid-work, not when "
                            "it reached a conclusion."
                        ),
                    },
                    "force": {
                        "type": "boolean",
                        "description": (
                            "Only when the same role already has a live task and a second, genuinely "
                            "different angle is wanted. Without it a duplicate dispatch is refused."
                        ),
                    },
                },
                "required": ["role", "objective"],
            },
        },
        {
            "name": "wait_tasks",
            "description": (
                "Block until one of the given tasks reaches a terminal state or the timeout "
                "expires, then return a snapshot: finished tasks with their results, and the "
                "ids still pending. It wakes the moment any task finishes, so one long call "
                "beats a chain of short ones: every call back here is a full model turn. "
                "Use a generous timeout_s and let it block; a timeout is not an error."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_ids": {"type": "array", "items": {"type": "integer"}},
                    "timeout_s": {
                        "type": "integer",
                        "default": 240,
                        "minimum": 1,
                        "maximum": 480,
                        "description": (
                            "Bounded under the CLI's MCP tool timeout (raised to 600s for "
                            "these sessions). Default 240s spans a real stretch of work."
                        ),
                    },
                },
                "required": ["task_ids"],
            },
        },
        {
            "name": "list_tasks",
            "description": "List tasks in this run with status, role, cost and a one-line result summary.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_task_result",
            "description": "Read a finished task's structured result, findings and final report text.",
            "input_schema": {
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        },
        {
            "name": "read_blackboard",
            "description": "Read the run's facts and flag candidates, newest last.",
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 60}},
            },
        },
        {
            "name": "record_fact",
            "description": (
                "Write a conclusion or dead end to the blackboard so every later worker sees it. "
                "Use kind=dead_end for approaches that failed and why. Set basis to say how it is "
                "grounded (observed / derived / inferred / external). If basis=observed, quote the "
                "evidence too: `evidence.command` (what ran) and `evidence.output` (what it printed) "
                "— otherwise the record is filed as 推测."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["observation", "insight", "credential", "dead_end"],
                        "default": "insight",
                    },
                    "evidence": {
                        "type": "object",
                        "description": (
                            "command / output (or request / response) for observed facts; paths and "
                            "URLs for inferred ones. A dead_end also needs `scope` (what space was "
                            "searched) and `control` (the known answer that proved the detector works)."
                        ),
                    },
                    "basis": {
                        "type": "string",
                        "enum": ["observed", "derived", "inferred", "external"],
                    },
                    "resolves": {
                        "type": "integer",
                        "description": "Id of an open unknown this fact answers.",
                    },
                    "supersedes": {
                        "type": "integer",
                        "description": (
                            "Id of an earlier record this one overturns. The board then marks the "
                            "old one 已被推翻, so two contradicting conclusions never sit side by "
                            "side with no way to tell which is current."
                        ),
                    },
                },
                "required": ["content"],
            },
        },
        {
            "name": "record_unknown",
            "description": (
                "Record a question the run cannot answer yet, so it becomes a dispatchable item "
                "instead of a silent gap. Close it later with record_fact(resolves=<id>)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "would_resolve": {"type": "string"},
                },
                "required": ["question"],
            },
        },
        {
            "name": "propose_flag",
            "description": (
                "Submit a candidate flag. It is recorded, then an independent verifier worker is "
                "dispatched automatically. A human confirms the final flag."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "method": {"type": "string", "description": "How it was obtained, step by step."},
                    "source": {"type": "string", "description": "File path or artifact it came from."},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"], "default": "medium"},
                },
                "required": ["value", "method"],
            },
        },
        {
            "name": "ask_user",
            "description": (
                "Ask the human operator a question and wait for the answer. Use for missing "
                "credentials, a hint request, or a decision only the operator can make."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
        {
            "name": "finish_run",
            "description": "End the run with a final summary. status=needs_input if a human must decide.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "status": {"type": "string", "enum": ["solved", "failed", "needs_input"], "default": "failed"},
                },
                "required": ["summary"],
            },
        },
    ]


class OrchestratorTools:
    """Implements the tools against a live Run. One instance per run."""

    def __init__(self, run: "Run") -> None:
        self.run = run
        #: resolve(question) -> answer; set by the run's human-in-the-loop channel.
        self.question_handler = None

    async def dispatch_task(
        self,
        role: str,
        objective: str,
        context: str = "",
        resume_task_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        if role not in ROLES:
            return {"error": f"unknown role {role!r}; choose from {list(ROLES)}"}
        if not self.run.accepting_work:
            return {"error": "run is finishing; no new tasks accepted"}
        # Near the dollar ceiling, opening a new direction costs more than it can
        # return, but verifying a flag already in hand and writing it up is the
        # whole point of what has been spent. Refuse only the exploration roles.
        if role not in CLOSING_ROLES and not self.run.accepting_exploration:
            return {
                "error": (
                    f"运行已接近预算上限（已花 ${self.run.cost_usd:.2f}），不再派发新的探索工单。"
                    "已派出的 worker 会跑完；verifier 和 writer 仍然可以派。"
                    "现在应该做的是：read_blackboard 汇总已有结论，对可复现的候选调用 "
                    "propose_flag，或直接 finish_run 交出一份说明。"
                )
            }
        # Refuse before spending: a second worker in a direction that already
        # has one running is a guess that it is stuck, and the guess has been
        # wrong every time it was made under time pressure. Run 5 paid $10.43
        # for two reverse workers that both hit the turn ceiling; run 15 was
        # warned four times in one run and dispatched anyway, paying for four
        # transcripts that each re-read the same 1312-layer archive. A warning
        # the model can read past is not a guard — this returns an error, and
        # the model has to either wait, resume the live task, or say ``force``.
        live = self.run.live_direction_tasks(role) if resume_task_id is None else []
        if live and not force:
            names = "、".join(f"#{t.id}" for t in live)
            options = [
                f"wait_tasks([{live[0].id}]) 看它在做什么，再决定",
                f"如果它撞了上限，dispatch_task(role='{role}', resume_task_id={live[0].id}, ...)",
                "确实需要第二个角度时，带 force=true 再派一次",
            ]
            # Name the reason when there is one. A worker waiting on its own
            # background job is the case the lead most often misreads as stuck:
            # run 15 hedged four times against workers whose cracks were still
            # printing progress.
            for t in live:
                progress = self.run.live_progress(t.id)
                if progress.get("background"):
                    options.insert(
                        0,
                        f"#{t.id} 正在等 {progress['background']} 个后台任务，这是在做工，"
                        f"不要因为安静就重派",
                    )
            return {
                "error": f"{role} 方向已有活跃工单 {names}，未派发。",
                "live_tasks": [t.id for t in live],
                "options": options,
            }
        # A direction that already has a transcript is the other half of the same
        # waste, and the half a warning did not stop: run 15 dispatched seven
        # reverse workers, each re-reading the same 1312-layer archive from the
        # start, because the guard only looked at tasks still running. Resuming
        # reuses what the last one established; a genuinely different angle is
        # what ``force`` is for.
        if resume_task_id is None and not force:
            spent = self.run.resumable_tasks(role)
            if spent:
                last = spent[-1]
                return {
                    "error": (
                        f"{role} 方向已有可续跑的会话 #{last.id}"
                        f"（{last.status}，已花 ${last.cost_usd:.2f}）。"
                        "新开一个会从零重读同一份材料，把已经做完的分析再付一遍钱。"
                    ),
                    "resumable_tasks": [t.id for t in spent],
                    "options": [
                        f"dispatch_task(role='{role}', resume_task_id={last.id}, ...) "
                        "接上它的 transcript 继续",
                        "先用 get_task_result / read_blackboard 看它到底做到哪一步",
                        "确实是另一个角度（不是同一条路的重试）时，带 force=true",
                    ],
                }
        warnings = self.run.duplicate_direction_warnings(role, resume_task_id)
        task = await self.run.dispatch(role, objective, context, resume_task_id=resume_task_id)
        out: dict[str, Any] = {"task_id": task.id, "role": task.role, "status": task.status}
        if warnings:
            out["warnings"] = warnings
        out["clock"] = self.run.clock()
        return out

    async def wait_tasks(self, task_ids: list[int], timeout_s: int = 240) -> dict[str, Any]:
        # One call should span a real stretch of worker time. At the old 45 s
        # default the lead re-polled up to 65 times per run (measured), each
        # poll a full model turn on a transcript that keeps growing. The
        # ceiling stays under the CLI's MCP_TOOL_TIMEOUT, raised to 600 s for
        # these sessions in ``_session_env``; if that env ever stops being
        # honored, the CLI killing the call is the failure mode — the lead
        # sees an error and retries, which is the old behaviour, not worse.
        timeout_s = max(1, min(int(timeout_s), 480))
        tasks = await self.run.wait(task_ids, timeout_s)
        pending = [t["task_id"] for t in tasks if not t.get("terminal")]
        return {
            "tasks": tasks,
            "pending": pending,
            "timed_out": bool(pending),
            "clock": self.run.clock(),
        }

    def list_tasks(self) -> dict[str, Any]:
        return {"tasks": self.run.task_summaries(), "clock": self.run.clock()}

    def get_task_result(self, task_id: int) -> dict[str, Any]:
        return self.run.task_result(task_id)

    def read_blackboard(self, limit: int = 60) -> dict[str, Any]:
        return self.run.blackboard_digest(limit=limit, for_main=True)

    def record_fact(
        self, content: str, kind: str = "insight", basis: str = "",
        resolves: int | None = None, evidence: dict[str, Any] | None = None,
        supersedes: int | None = None,
    ) -> dict[str, Any]:
        gap = grounding_gap(basis, evidence)
        ngap = negative_gap(kind, evidence)
        fact_id = self.run.record_fact(
            None, kind, content, evidence or {}, basis, resolves, supersedes
        )
        if ngap:
            return {
                "fact_id": fact_id,
                "warning": f"已记为「未验证的否定」（{ngap}）：否定只对实际跑过的空间成立，"
                           f"补 scope 与 control 后再记一次。",
            }
        if gap:
            return {
                "fact_id": fact_id,
                "warning": f"按 {basis} 记录但缺少证据（{gap}），已降为 推测。",
            }
        return {"fact_id": fact_id}

    def record_unknown(
        self, question: str, why_it_matters: str = "", would_resolve: str = ""
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            return {"error": "问题为空：写成一个具体、可回答的问题"}
        fact_id = self.run.record_fact(
            None,
            "unknown",
            question,
            {"why_it_matters": why_it_matters, "would_resolve": would_resolve},
            "inferred",
        )
        return {"unknown_id": fact_id}

    async def propose_flag(
        self, value: str, method: str, source: str = "", confidence: str = "medium"
    ) -> dict[str, Any]:
        return await self.run.propose_flag(value, method, source, confidence, task_id=None)

    async def ask_user(self, question: str) -> dict[str, Any]:
        if self.question_handler is None:
            return {"answer": "", "error": "no human attached"}
        answer = await self.question_handler(question)
        return {"answer": answer}

    def finish_run(self, summary: str, status: str = "failed") -> dict[str, Any]:
        self.run.request_finish(status, summary)
        return {"ok": True}

    # -- dispatch table ----------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        handler = getattr(self, name, None)
        if handler is None:
            raise ValueError(f"未知工具：{name}")
        return handler(**arguments)


def build_sdk_mcp_server(tools: OrchestratorTools) -> Any:
    """Wrap the orchestrator tools as an in-process SDK MCP server.

    Import is lazy: the offline test path never needs the SDK installed.
    """
    import claude_agent_sdk as sdk  # noqa: PLC0415

    async def make(name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = tools.call(name, args)
            if asyncio.iscoroutine(result):
                result = await result
        except TypeError as exc:  # bad argument shape
            result = {"error": f"bad arguments: {exc}"}
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
        text = json.dumps(result, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": text}]}

    sdk_tools = []
    for spec in tool_specs():
        name = spec["name"]

        async def handler(args: dict[str, Any], _name: str = name) -> dict[str, Any]:
            return await make(_name, args)

        sdk_tools.append(
            sdk.tool(name, spec["description"], spec["input_schema"])(handler)
        )
    return sdk.create_sdk_mcp_server(name="orchestrator", version="1.0.0", tools=sdk_tools)
