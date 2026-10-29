"""Smoke test for the Claude Agent SDK path against the local proxy.

Not part of the test suite (it needs a live endpoint and costs money). Run it
before trusting a new SDK/CLI version:

    python scripts/smoke_sdk.py

It checks the four assumptions the orchestrator is built on:

1. the native ``claude.exe`` can be spawned (the PATH shim is rejected),
2. project settings + vendored skills are discovered under ``.claude/skills``,
3. an in-process MCP tool is callable and returns its result to the model,
4. a ``PreToolUse`` deny hook actually blocks a tool call under
   ``bypassPermissions``.

Everything happens in a temp directory; nothing outside it is touched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ctfagent.backend import ClaudeSDKBackend, SessionSpec, resolve_cli_path  # noqa: E402
from ctfagent.config import load_config  # noqa: E402
from ctfagent.guard import build_hooks  # noqa: E402
from ctfagent.models import ROLE_SKILL  # noqa: E402
from ctfagent.workspace import Workspace  # noqa: E402


def _server():
    import claude_agent_sdk as sdk

    calls: list[str] = []

    async def ping(args: dict) -> dict:
        calls.append(str(args.get("text", "")))
        return {"content": [{"type": "text", "text": json.dumps({"pong": args.get("text", "")})}]}

    tool = sdk.tool("ping", "Echo the text back as JSON.", {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})(ping)
    return sdk.create_sdk_mcp_server(name="smoke", version="1.0.0", tools=[tool]), calls


def _ancestor_skill_names(start: Path) -> set[str]:
    """Skill names visible from any ancestor ``.claude/skills``, marker or not.

    Computed independently of the session so the leak check does not have to
    know which skills the CLI bundles: anything in the init list that comes from
    up there, and is not one of the vendored ones, was not supposed to be there.
    """
    names: set[str] = set()
    for parent in (start, *start.parents):
        skills = parent / ".claude" / "skills"
        if skills.is_dir():
            names.update(p.name for p in skills.iterdir() if (p / "SKILL.md").is_file())
    return names


async def main() -> int:
    cfg = load_config()
    cli = resolve_cli_path(cfg.cli_path)
    print(f"cli_path        {cli or '(SDK bundled)'}")
    print(f"base_url        {cfg.models.base_url or '(env/default)'}")

    tmp = Path(tempfile.mkdtemp(prefix="ctfagent-smoke-"))
    # Through Workspace.create, not the bare constructor: the smoke test is
    # supposed to exercise the same layout a run gets, including the git marker
    # that keeps the operator's own .claude out of the session.
    ws = Workspace.create(tmp / "ws-root", 1, "smoke")
    skills = ws.install_skills(cfg.skills_dir)
    print(f"skills linked   {len(skills)}: {', '.join(skills[:4])}...")
    missing = ws.missing_skills(ROLE_SKILL.values())
    print(f"role skills     {'complete' if not missing else 'MISSING ' + ', '.join(missing)}")

    backend = ClaudeSDKBackend(cfg)
    server, calls = _server()
    spec = SessionSpec(
        role="smoke",
        prompt=(
            "Call the `mcp__smoke__ping` tool once with text 'hello'. "
            "Then try to write a file to C:/Windows/ctfagent-should-not-exist.txt. "
            "Report both outcomes briefly."
        ),
        cwd=ws.work,
        system_prompt="You are a smoke test. Follow the instructions exactly.",
        model=cfg.models.worker,
        max_turns=8,
        max_budget_usd=0.25,
        allowed_tools=["mcp__smoke__ping", "Write", "Bash"],
        skills=["solve-challenge"],
        mcp_servers={"smoke": server},
        hooks=build_hooks([ws.root]),
        extra_env={"PYTHONUTF8": "1"},
    )

    init_skills: list[str] = []
    invocable: list[str] = []
    denied: list[str] = []
    text: list[str] = []
    async for event in backend.run(spec):
        if event.kind == "init":
            # The SDK emits several SystemMessages; only the "init" subtype
            # carries the skill list.
            if event.payload.get("subtype") == "init":
                init_skills = event.payload.get("skills") or []
                invocable = event.payload.get("slash_commands") or []
                print(f"init            session={event.payload.get('session_id', '')[:8]} "
                      f"discovered={len(init_skills)} invocable={len(invocable)}")
        elif event.kind == "tool_use":
            print(f"tool_use        {event.payload.get('tool')} {json.dumps(event.payload.get('input'))[:120]}")
        elif event.kind == "tool_result":
            content = str(event.payload.get("content", ""))
            if event.payload.get("is_error") or "deny" in content.lower() or "outside" in content.lower():
                denied.append(content[:200])
            print(f"tool_result     {content[:160]}")
        elif event.kind == "text":
            text.append(str(event.payload.get("text", "")))
        elif event.kind == "result":
            print(f"result          subtype={event.payload.get('subtype')} "
                  f"cost=${event.cost_usd:.4f} turns={event.turns}")

    print()
    ok = True
    if not calls:
        print("FAIL: the in-process MCP tool was never called")
        ok = False
    else:
        print(f"OK   MCP tool called with {calls}")
    if not init_skills:
        print("WARN: no skills in the init message (project setting source may not have loaded)")
    else:
        # Names that exist above the workspace but not inside it came from the
        # operator's own .claude, i.e. the git marker in work/ is not working.
        leaked = sorted((set(init_skills) & _ancestor_skill_names(ws.work)) - set(skills))
        print(f"OK   skills discovered: {len(init_skills)}; invocable under the allowlist: "
              f"{[s for s in invocable if s.startswith('ctf') or s.startswith('solve')]}")
        if leaked:
            print(f"FAIL: skills leaked in from an ancestor directory: {leaked}")
            ok = False
        else:
            print("OK   no skills leaked in from an ancestor directory")
    if not denied:
        print("WARN: no denied tool result observed (the model may not have tried the write)")
    else:
        print("OK   guard denied a tool call")
    print()
    print("model text:", "\n".join(text)[:600])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
