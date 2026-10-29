"""read_skill_doc: the one thing that turns the skill index into technique text.

The census behind it: across 134 platform sessions the Skill tool was invoked
63 times, but not once did a session open a technique file the index links
(``ctf-web/auth-jwt.md`` and 106 siblings). The worker loaded a table of
contents and worked from memory.
"""

from __future__ import annotations

import asyncio

import pytest

from ctfagent.worker_tools import WorkerTools


class _Task:
    def __init__(self, role: str) -> None:
        self.role = role


class _Store:
    def __init__(self, role: str) -> None:
        self.role = role

    def get_task(self, task_id: int) -> _Task:
        return _Task(self.role)


class _Workspace:
    def __init__(self, root) -> None:
        # Mirrors the real layout: <work>/.claude/skills/<name>/.
        self.claude = root / ".claude"


class _Run:
    def __init__(self, root, role: str) -> None:
        self.store = _Store(role)
        self.workspace = _Workspace(root)


def _skills(tmp_path, skill: str, docs: dict[str, str]):
    root = tmp_path / ".claude" / "skills" / skill
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: x\n---\nindex\n", encoding="utf-8")
    for name, body in docs.items():
        (root / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_it_returns_the_technique_text(tmp_path) -> None:
    root = _skills(tmp_path, "ctf-web", {"auth-jwt.md": "# JWT\n\nAlgorithm confusion..."})
    tools = WorkerTools(_Run(root, "web"), 1)

    result = tools.read_skill_doc("auth-jwt.md")
    assert result["name"] == "auth-jwt.md"
    assert "Algorithm confusion" in result["content"]
    assert result["truncated"] == 0


def test_an_unknown_name_lists_what_the_index_actually_links(tmp_path) -> None:
    """A wrong guess must cost one call, not a dead end.

    Workers name these files from memory ("jwt.md" for "auth-jwt.md"). Returning
    the real list lets the next call succeed; a bare error would send the model
    back to improvising.
    """
    root = _skills(tmp_path, "ctf-web", {"auth-jwt.md": "x", "server-side.md": "y"})
    tools = WorkerTools(_Run(root, "web"), 1)

    result = tools.read_skill_doc("jwt.md")
    assert "error" in result
    assert result["available"] == ["auth-jwt.md", "server-side.md"]


def test_a_path_is_reduced_to_its_basename(tmp_path) -> None:
    root = _skills(tmp_path, "ctf-web", {"auth-jwt.md": "body"})
    tools = WorkerTools(_Run(root, "web"), 1)

    assert tools.read_skill_doc("../auth-jwt.md")["content"] == "body"
    assert tools.read_skill_doc("ctf-web/auth-jwt.md")["content"] == "body"


def test_traversal_cannot_leave_the_skill_directory(tmp_path) -> None:
    """Only the basename is used, so ``..`` cannot reach a file outside the tree."""
    root = _skills(tmp_path, "ctf-web", {})
    (tmp_path / "secret.md").write_text("host file", encoding="utf-8")
    tools = WorkerTools(_Run(root, "web"), 1)

    result = tools.read_skill_doc("../../secret.md")
    assert "error" in result
    assert "content" not in result


def test_the_index_itself_is_not_a_technique(tmp_path) -> None:
    """SKILL.md is already in context; asking for it means the model misread."""
    root = _skills(tmp_path, "ctf-web", {})
    tools = WorkerTools(_Run(root, "web"), 1)

    assert "已经在上下文里" in tools.read_skill_doc("SKILL.md")["error"]


def test_a_role_without_a_skill_is_told_so(tmp_path) -> None:
    root = _skills(tmp_path, "ctf-web", {"auth-jwt.md": "x"})
    tools = WorkerTools(_Run(root, "no-such-role"), 1)

    assert "没有绑定技能" in tools.read_skill_doc("auth-jwt.md")["error"]


def test_a_long_document_says_how_much_was_withheld(tmp_path) -> None:
    body = "A" * (WorkerTools.SKILL_DOC_CHARS + 500)
    root = _skills(tmp_path, "ctf-web", {"huge.md": body})
    tools = WorkerTools(_Run(root, "web"), 1)

    result = tools.read_skill_doc("huge.md")
    assert result["truncated"] == 500
    assert len(result["content"]) == WorkerTools.SKILL_DOC_CHARS
    assert "Read" in result["note"]


def test_the_tool_is_advertised_and_reachable_over_mcp(tmp_path) -> None:
    """It has to be in the spec list *and* in the worker's allowed_tools.

    A tool defined but not allowlisted is the failure this platform has had
    before with skill names: the session starts, the model calls something the
    allowlist does not name, and the error reads as the model misbehaving.
    """
    from ctfagent.worker_tools import TOOL_NAMES, tool_specs

    assert "read_skill_doc" in TOOL_NAMES
    assert "read_skill_doc" in [s["name"] for s in tool_specs()]
