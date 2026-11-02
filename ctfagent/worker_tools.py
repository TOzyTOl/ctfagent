"""Worker-side tools: the structured write-back channel.

A worker's final text is a summary; its *product* is what it writes here. The
distinction is what makes aggregation reliable — the orchestrator never parses
prose for flags or findings, and a worker that runs out of turns mid-way has
already persisted everything worth keeping (the same reason ARTEX forces a
write-back turn during settlement).

These are exposed as a second in-process MCP server, ``worker``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .models import ROLE_SKILL, FactKind, grounding_gap, negative_gap

if TYPE_CHECKING:  # pragma: no cover
    from .orchestrator import Run

TOOL_NAMES = [
    "record_fact",
    "record_unknown",
    "submit_flag",
    "report_progress",
    "request_help",
    "task_complete",
    "read_skill_doc",
    "post_hypothesis",
]


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "record_fact",
            "description": (
                "Write a durable conclusion to the shared blackboard. Every other worker and the "
                "main agent will see it. Record negatives too: kind=dead_end for an approach that "
                "failed, with why, so nobody repeats it. A dead_end also needs evidence.scope "
                "(what space you actually searched) and evidence.control (the known answer you "
                "planted to prove the detector works); without both it is stored as an unverified "
                "negative, not a closed route. Always set basis: observed if you ran "
                "something and saw the result, derived if you computed it from observed data, "
                "inferred if it is your reading of code or a hypothesis, external if it came from "
                "documentation or the challenge text. Pass resolves=<unknown id> when this closes "
                "a recorded unknown."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["observation", "insight", "credential", "dead_end"],
                        "default": "observation",
                    },
                    "evidence": {
                        "type": "object",
                        "description": (
                            "What the claim rests on. For basis=observed this is required and must "
                            "carry both sides: `command` (the exact command, request or URL that ran) "
                            "and `output` (the raw stdout/stderr or the relevant response excerpt). "
                            "For derived, `command` alone is enough. A fact with neither is filed as "
                            "推测. A dead_end additionally needs `scope` (what space was searched) "
                            "and `control` (the known answer that proved the detector works)."
                        ),
                    },
                    "basis": {
                        "type": "string",
                        "enum": ["observed", "derived", "inferred", "external"],
                        "description": (
                            "How this is grounded. Do not claim observed without a run: an observed "
                            "fact without command+output is automatically downgraded to inferred."
                        ),
                    },
                    "resolves": {
                        "type": "integer",
                        "description": "Id of an open unknown this fact answers.",
                    },
                    "supersedes": {
                        "type": "integer",
                        "description": (
                            "Id of an earlier record this one overturns. Pass it whenever your "
                            "finding contradicts something already on the board: the old record is "
                            "then shown as 已被推翻 instead of the next worker having to work out "
                            "which of two conflicting conclusions is current."
                        ),
                    },
                },
                "required": ["content", "basis"],
            },
        },
        {
            "name": "record_unknown",
            "description": (
                "Record a question the team cannot answer yet, instead of guessing or staying "
                "silent. Write it as one answerable question plus why it blocks the solve and what "
                "would resolve it. The main agent sees open unknowns and can dispatch work at them. "
                "Close it later with record_fact(resolves=<id>)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "One specific, answerable question."},
                    "why_it_matters": {"type": "string", "description": "What decision this blocks."},
                    "would_resolve": {
                        "type": "string",
                        "description": "The observation, tool or artifact that would settle it.",
                    },
                },
                "required": ["question", "why_it_matters"],
            },
        },
        {
            "name": "submit_flag",
            "description": (
                "Submit a candidate flag. State exactly how you derived it so an independent "
                "verifier can reproduce it. Do not submit guesses."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "method": {"type": "string", "description": "Step-by-step derivation."},
                    "source": {"type": "string", "description": "Artifact path or URL."},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"], "default": "medium"},
                },
                "required": ["value", "method"],
            },
        },
        {
            "name": "report_progress",
            "description": "Post a short progress note for the human watching the run.",
            "input_schema": {
                "type": "object",
                "properties": {"note": {"type": "string"}},
                "required": ["note"],
            },
        },
        {
            "name": "request_help",
            "description": (
                "Ask the main agent for something you cannot do yourself: another direction, "
                "a missing tool, a file you cannot read. Returns the main agent's reply."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "blocking": {"type": "boolean", "default": False},
                },
                "required": ["question"],
            },
        },
        {
            "name": "task_complete",
            "description": (
                "Declare this task finished with a structured result. Call this last. "
                "status=done if the objective was met, blocked if it could not be met."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["done", "blocked"], "default": "done"},
                    "summary": {"type": "string"},
                    "findings": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Key results, one per line.",
                    },
                    "artifacts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paths (relative to the work dir) worth keeping.",
                    },
                    "next_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "What should be tried next, if anything.",
                    },
                    "verdict": {
                        "type": "string",
                        "enum": ["reproduced", "refuted", "inconclusive"],
                        "description": (
                            "Verifier only. Your conclusion about the candidate flag, as a field "
                            "rather than a sentence: the orchestrator reads this to decide whether "
                            "the flag is verified, and prose is only a fallback. reproduced = your "
                            "own fresh run produced this exact value; refuted = your run shows it "
                            "is wrong; inconclusive = you checked but cannot settle it."
                        ),
                    },
                },
                "required": ["summary"],
            },
        },
        {
            "name": "read_skill_doc",
            "description": (
                "Open one technique document from this direction's skill library (for example "
                "`auth-jwt.md` under ctf-web). The skill you loaded is an index: it describes which "
                "techniques exist and links a file per technique. Reading the index is not the same "
                "as reading the technique — when the challenge matches one of those entries, open it "
                "before working from memory. Call this more than once when the challenge spans "
                "several entries. An unknown name returns the list of documents that do exist."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "File name from the skill index, e.g. `auth-jwt.md`. A path with "
                            "separators is accepted and only the file name is used."
                        ),
                    }
                },
                "required": ["name"],
            },
        },
        {
            "name": "post_hypothesis",
            "description": (
                "Write a direction on the board that you are NOT going to chase yourself. Use it "
                "when your work turns up an angle for another direction (a crypto weakness in a web "
                "flow, a second binary behind a login) so the observation is not lost when your task "
                "ends. criterion is what would prove it wrong; it is required. Posting the same idea "
                "again merges with the existing entry instead of duplicating it."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "family": {
                        "type": "string",
                        "enum": [r for r in ROLE_SKILL if r not in ("verifier", "writer")],
                        "description": "Which direction should pick this up.",
                    },
                    "statement": {"type": "string", "description": "The claim, in one sentence."},
                    "criterion": {
                        "type": "string",
                        "description": "What you would have to observe for this to be wrong.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "The observation you just made that suggests it.",
                    },
                    "priority": {"type": "integer", "default": 0},
                },
                "required": ["family", "statement", "criterion", "evidence"],
            },
        },
    ]


class WorkerTools:
    #: How much of one technique document comes back in a call. A skill file is
    #: 3-40 KB; the cap keeps two or three opens from filling a worker's context,
    #: while the note says how much was withheld so the model can judge coverage.
    SKILL_DOC_CHARS = 24_000

    def __init__(self, run: "Run", task_id: int) -> None:
        self.run = run
        self.task_id = task_id

    def record_fact(
        self,
        content: str,
        kind: str = "observation",
        evidence: dict[str, Any] | None = None,
        basis: str = "",
        resolves: int | None = None,
        supersedes: int | None = None,
    ) -> dict[str, Any]:
        gap = grounding_gap(basis, evidence)
        ngap = negative_gap(kind, evidence)
        fact_id = self.run.record_fact(
            self.task_id, kind, content, evidence or {}, basis, resolves, supersedes
        )
        if ngap:
            # Tell the worker while it can still fix it: the record already
            # exists, so the fix is a second record_fact carrying scope and
            # control (not a restatement — it has evidence the first lacked).
            return {
                "fact_id": fact_id,
                "warning": (
                    f"已记为「未验证的否定」（{ngap}）。否定结论只对你实际跑过的空间成立；"
                    f"用 record_fact 再记一次，evidence 里补 scope（搜索空间多大、枚举的是哪个变量）"
                    f"和 control（你种进去的已知答案，以及它被找回来了）。"
                ),
            }
        if gap:
            return {
                "fact_id": fact_id,
                "warning": (
                    f"按 {basis} 记录但缺少证据（{gap}），已降为 推测。"
                    f"如果你确实跑过，用 record_fact 重记一次并附 command 与 output。"
                ),
            }
        return {"fact_id": fact_id}

    def record_unknown(
        self, question: str, why_it_matters: str = "", would_resolve: str = ""
    ) -> dict[str, Any]:
        question = question.strip()
        if not question:
            return {"error": "问题为空：写成一个具体、可回答的问题"}
        fact_id = self.run.record_fact(
            self.task_id,
            FactKind.UNKNOWN,
            question,
            {"why_it_matters": why_it_matters, "would_resolve": would_resolve},
            "inferred",
        )
        return {"unknown_id": fact_id, "note": "主 agent 会看到这条未解问题；解决后用 record_fact(resolves=该 id)。"}

    def post_hypothesis(
        self,
        family: str,
        statement: str,
        criterion: str,
        evidence: str,
        priority: int = 0,
    ) -> dict[str, Any]:
        """Put a direction on the board that this worker is not going to chase.

        A worker is usually the one who sees the next angle while doing
        something else. Writing it down is what lets the lead dispatch it later
        instead of the observation dying with this task's transcript.
        """
        if family not in ROLE_SKILL:
            return {"error": f"未知方向 {family!r}；选 {[r for r in ROLE_SKILL if r not in ('verifier', 'writer')]}"}
        return self.run.post_hypothesis(
            family=family,
            statement=statement,
            criterion=criterion,
            evidence=evidence,
            origin=f"task#{self.task_id}",
            priority=priority,
        )

    async def submit_flag(
        self, value: str, method: str, source: str = "", confidence: str = "medium"
    ) -> dict[str, Any]:
        return await self.run.propose_flag(value, method, source, confidence, task_id=self.task_id)

    def report_progress(self, note: str) -> dict[str, Any]:
        self.run.emit(self.task_id, "progress", {"note": note})
        return {"ok": True}

    def read_skill_doc(self, name: str) -> dict[str, Any]:
        """Open one technique file from the role's skill directory.

        The skill the worker loads is an index: ``ctf-web/SKILL.md`` lists
        ``auth-jwt.md``, ``server-side.md`` and a hundred more, and the model
        then works from memory instead of opening any of them. Nothing enforced
        that, and a transcript census found zero technique files opened across
        134 sessions. A named tool is a far stronger nudge than a prompt line,
        and it keeps the read inside the skill tree, which ``Read`` does not.

        Returns the text plus how many characters were withheld, so a worker
        can tell "this is the whole document" from "this is the first page".
        """
        task = self.run.store.get_task(self.task_id)
        skill = ROLE_SKILL.get(getattr(task, "role", ""), "") if task else ""
        if not skill:
            return {"error": "这个角色没有绑定技能，没有可读的技术文档。"}

        root = self.run.workspace.claude / "skills" / skill
        wanted = Path(str(name).replace("\\", "/")).name
        if not wanted:
            return {"error": "文档名为空：给出技能索引里列出的文件名，例如 auth-jwt.md。"}
        if wanted.lower() == "skill.md":
            return {"error": "技能索引本身已经在上下文里了；这里要的是它链接的技术文档。"}

        available = sorted(
            p.name for p in root.glob("*.md") if p.is_file() and p.name.lower() != "skill.md"
        )
        target = root / wanted
        if not target.is_file():
            # Naming the real files turns a failed guess into the next call.
            return {
                "error": f"{skill} 下没有 {wanted}。",
                "available": available,
            }

        text = target.read_text(encoding="utf-8", errors="replace")
        cap = self.SKILL_DOC_CHARS
        if len(text) > cap:
            return {
                "name": wanted,
                "skill": skill,
                "content": text[:cap],
                "truncated": len(text) - cap,
                "note": f"只返回了前 {cap} 字（全文 {len(text)} 字）。需要更多就直接 Read 整个文件。",
            }
        return {"name": wanted, "skill": skill, "content": text, "truncated": 0}

    async def request_help(self, question: str, blocking: bool = False) -> dict[str, Any]:
        return await self.run.worker_help(self.task_id, question, blocking=blocking)

    def task_complete(
        self,
        summary: str,
        status: str = "done",
        findings: list[str] | None = None,
        artifacts: list[str] | None = None,
        next_steps: list[str] | None = None,
        verdict: str = "",
    ) -> dict[str, Any]:
        self.run.complete_task(
            self.task_id,
            status=status,
            summary=summary,
            findings=findings or [],
            artifacts=artifacts or [],
            next_steps=next_steps or [],
            verdict=verdict,
        )
        return {"ok": True}

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        handler = getattr(self, name, None)
        if handler is None:
            raise ValueError(f"未知工具：{name}")
        return handler(**arguments)


def build_sdk_mcp_server(tools: WorkerTools) -> Any:
    import asyncio  # noqa: PLC0415

    import claude_agent_sdk as sdk  # noqa: PLC0415

    async def make(name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = tools.call(name, args)
            if asyncio.iscoroutine(result):
                result = await result
        except TypeError as exc:
            result = {"error": f"bad arguments: {exc}"}
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]}

    sdk_tools = []
    for spec in tool_specs():
        name = spec["name"]

        async def handler(args: dict[str, Any], _name: str = name) -> dict[str, Any]:
            return await make(_name, args)

        sdk_tools.append(sdk.tool(name, spec["description"], spec["input_schema"])(handler))
    return sdk.create_sdk_mcp_server(name="worker", version="1.0.0", tools=sdk_tools)
