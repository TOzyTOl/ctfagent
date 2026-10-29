"""Read a bench worker transcript and report the numbers that decide whether a
run failed for a reason worth fixing.

Written after the 2026-10-27 easyrs run, where the post-mortem question was
"did it fail because the platform is weak, or because it burned turns on tool
paths that do not work on this host". Both answers live in the transcript, but
only if the same things are counted the same way each time:

  * guard refusals          -- "已拒绝" in a tool result
  * WSL quoting corruption  -- "command not found" from bash
  * oversized results       -- a single tool result that dominates the context
  * repeated identical calls
  * real token totals       -- the CLI's ``totalCostUSD`` is an Anthropic price
                               estimate for a deepseek model, i.e. fiction; the
                               usage counters are real.

Usage:
    python scripts/bench_postmortem.py <transcript.jsonl> [...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: $/M tokens, from ``[models.pricing]`` in config.toml (gateway's own formula).
PRICES = {
    "deepseek-v4-pro": (4.50, 13.50, 0.15),   # input, output, cache_read
    "deepseek-v4-flash": (1.00, 4.00, 0.02),
}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def calls_by_id(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in rows:
        message = row.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                out[block["id"]] = {"name": block.get("name", "?"), "input": block.get("input") or {}}
    return out


def tool_results(rows: list[dict], calls: dict[str, dict]):
    """Yield (n, name, input, text, is_error) for every tool result in order."""
    n = 0
    for row in rows:
        message = row.get("message") or {}
        if message.get("role") != "user":
            continue
        for block in message.get("content") or []:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            n += 1
            call = calls.get(block.get("tool_use_id"), {"name": "?", "input": {}})
            body = block.get("content")
            text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            yield n, call["name"], call["input"], text, bool(block.get("is_error"))


def tokens(rows: list[dict]) -> tuple[dict[str, tuple[int, int, int]], int]:
    """Per-model (input, output, cache_read) summed over turns, plus turn count.

    Synthetic assistant messages (the ones the CLI writes for an API error or a
    local notice) carry zero usage and must not count as turns.
    """
    seen: set[tuple] = set()
    per_model: dict[str, list[int]] = {}
    turns = 0
    for row in rows:
        message = row.get("message") or {}
        if message.get("role") != "assistant" or row.get("isApiErrorMessage"):
            continue
        usage = message.get("usage") or {}
        triple = (
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            int(usage.get("cache_read_input_tokens") or 0),
        )
        if not any(triple):
            continue
        # The CLI records the same usage on several consecutive records; count
        # each distinct triple once so the totals stay a sum over turns.
        if triple in seen:
            continue
        seen.add(triple)
        turns += 1
        model = message.get("model") or "?"
        acc = per_model.setdefault(model, [0, 0, 0])
        acc[0] += triple[0]
        acc[1] += triple[1]
        acc[2] += triple[2]
    return {m: tuple(v) for m, v in per_model.items()}, turns


def cost(per_model: dict[str, tuple[int, int, int]]) -> float:
    total = 0.0
    for model, (pin, pout, cread) in per_model.items():
        rate = PRICES.get(model)
        if rate is None:
            continue
        total += (pin * rate[0] + pout * rate[1] + cread * rate[2]) / 1_000_000
    return total


def report(path: Path) -> None:
    rows = load(path)
    calls = calls_by_id(rows)
    per_model, turns = tokens(rows)

    guard: list[int] = []
    quote: list[int] = []
    big: list[tuple[int, int, str]] = []
    dupes: dict[str, int] = {}
    total_chars = 0
    for n, name, args, text, _err in tool_results(rows, calls):
        total_chars += len(text)
        if "已拒绝" in text:
            guard.append(n)
        if "command not found" in text:
            quote.append(n)
        if len(text) > 20000:
            big.append((n, len(text), name))
        key = json.dumps(args, sort_keys=True, ensure_ascii=False)
        dupes[key] = dupes.get(key, 0) + 1

    print(f"== {path.name}")
    print(f"   会话记录 {len(rows)}，有效回合 {turns}")
    print(f"   token: " + ", ".join(f"{m} in={a:,} out={b:,} cr={c:,}" for m, (a, b, c) in per_model.items()))
    print(f"   按 [models.pricing] 计: ${cost(per_model):.2f}")
    print(f"   guard 拒绝 {len(guard)} 次 {guard}")
    print(f"   WSL 引号损坏 {len(quote)} 次 {quote}")
    print(f"   >20K 的工具结果 {len(big)} 个，合计 {sum(s for _, s, _ in big):,} 字符 {big}")
    print(f"   工具结果总计 {total_chars:,} 字符")
    repeats = sum(v - 1 for v in dupes.values() if v > 1)
    print(f"   完全重复的调用 {repeats} 次")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for arg in sys.argv[1:]:
        report(Path(arg))
