"""Where the money actually goes.

The point of this module is to answer "how do I spend less" with numbers instead
of intuition. Measured on this host over 54 sessions:

* **thinking is 65% of everything the model reads back** (14.6M of 22.4M
  characters of session content). It is billed twice: once as output at the
  highest rate, then again on every later turn, because the transcript carries it.
* **prompt caching is already at 94.7%**, so "add caching" is not a lever — it is
  the one thing that is already working.
* input outweighs output **44:1**. Cost is therefore driven by
  ``turn count x transcript size``, not by how much the model writes.

So the levers, largest first: fewer turns, a smaller transcript (chiefly
thinking), and only then a cheaper model.
"""

from __future__ import annotations

from typing import Any

from .config import AppConfig

_ROLE_ORDER = ("main", "reverse", "crypto", "pwn", "web", "forensics", "misc",
               "ai_ml", "osint", "triage", "verifier", "writer")


def _model_for(cfg: AppConfig, role: str) -> str:
    return cfg.models.main if role == "main" else cfg.models.model_for(role)


def _money(cfg: AppConfig, role: str, fresh: int, cached: int, out: int) -> float | None:
    """Cost in USD, or None when this model has no price entry."""
    rates = cfg.models.pricing.get(_model_for(cfg, role)) or {}
    if not any(rates.get(k) for k in ("input", "cache_read", "output")):
        return None
    return (
        fresh * float(rates.get("input") or 0)
        + cached * float(rates.get("cache_read") or 0)
        + out * float(rates.get("output") or 0)
    ) / 1_000_000


def report(store: Any, cfg: AppConfig, run_id: int | None = None) -> dict[str, Any]:
    """Token and money breakdown, per session, per role, per token class."""
    rows = store.usage_by_task(run_id)

    buckets = {"fresh": 0, "cached": 0, "output": 0}
    money = {"fresh": 0.0, "cached": 0.0, "output": 0.0}
    by_role: dict[str, dict[str, Any]] = {}
    priced = unpriced = 0

    for row in rows:
        role = row["role"]
        fresh, cached, out = row["input_tokens"], row["cache_read"], row["output_tokens"]
        buckets["fresh"] += fresh
        buckets["cached"] += cached
        buckets["output"] += out
        cost = _money(cfg, role, fresh, cached, out)
        if cost is None:
            unpriced += 1
        else:
            priced += 1
            rates = cfg.models.pricing.get(_model_for(cfg, role)) or {}
            money["fresh"] += fresh * float(rates.get("input") or 0) / 1_000_000
            money["cached"] += cached * float(rates.get("cache_read") or 0) / 1_000_000
            money["output"] += out * float(rates.get("output") or 0) / 1_000_000
        slot = by_role.setdefault(
            role,
            {"sessions": 0, "fresh": 0, "cached": 0, "output": 0,
             "cost_usd": 0.0, "priced": 0},
        )
        slot["sessions"] += 1
        slot["fresh"] += fresh
        slot["cached"] += cached
        slot["output"] += out
        if cost is not None:
            slot["cost_usd"] += cost
            slot["priced"] += 1

    total_in = buckets["fresh"] + buckets["cached"]
    expensive = sorted(
        (
            {
                "task_id": r["task_id"],
                "run_id": r["run_id"],
                "role": r["role"],
                "input_tokens": r["input_tokens"] + r["cache_read"],
                "cache_read": r["cache_read"],
                "output_tokens": r["output_tokens"],
                "cost_usd": _money(cfg, r["role"], r["input_tokens"], r["cache_read"],
                                   r["output_tokens"]),
            }
            for r in rows
        ),
        key=lambda r: (r["cost_usd"] or 0) or r["input_tokens"],
        reverse=True,
    )

    return {
        "sessions": len(rows),
        "priced_sessions": priced,
        "unpriced_sessions": unpriced,
        "pricing_configured": bool(cfg.models.pricing),
        "tokens": buckets,
        "money": money,
        "total_usd": sum(money.values()),
        "cache_hit_rate": (buckets["cached"] / total_in) if total_in else 0.0,
        "input_output_ratio": (total_in / buckets["output"]) if buckets["output"] else 0.0,
        "by_role": by_role,
        "top_sessions": expensive[:10],
        "effort": dict(cfg.models.effort),
    }


def render(data: dict[str, Any]) -> str:
    """A console report an operator can read without a browser."""
    lines: list[str] = []
    tokens = data["tokens"]
    money = data["money"]

    lines.append(f"会话 {data['sessions']} 个")
    if not data["pricing_configured"]:
        lines.append("")
        lines.append("  ! 没有配 [models.pricing]，下面只有 token 量，没有钱。")
        lines.append("    CLI 报告的成本是按 Anthropic 价目表查模型名算的，对网关模型是虚构数字。")
        lines.append("    在 config.toml 里填上 [models.pricing] 后才能按真实消耗记账。")
    elif data["unpriced_sessions"]:
        lines.append(
            f"  ! {data['unpriced_sessions']}/{data['sessions']} 个会话的模型没有价目，"
            "这部分成本缺失。"
        )

    lines.append("")
    lines.append("按 token 类型：")
    total_money = data["total_usd"]
    for key, label in (("cached", "缓存读取"), ("fresh", "新输入"), ("output", "输出")):
        share = (money[key] / total_money * 100) if total_money else 0.0
        amount = f"${money[key]:>8.2f} {share:>5.1f}%" if total_money else " " * 15
        lines.append(f"  {label:<8} {tokens[key]:>12,} token   {amount}")

    lines.append("")
    lines.append(f"  缓存命中率   {data['cache_hit_rate'] * 100:.1f}%")
    lines.append(f"  输入:输出    {data['input_output_ratio']:.1f} : 1")

    lines.append("")
    lines.append("按角色：")
    for role, slot in sorted(
        data["by_role"].items(), key=lambda kv: -(kv[1]["fresh"] + kv[1]["cached"])
    ):
        cost = f"${slot['cost_usd']:>7.2f}" if total_money else "      -"
        lines.append(
            f"  {role:<10} {slot['sessions']:>3} 个  {cost}  "
            f"输入 {slot['fresh'] + slot['cached']:>10,}  输出 {slot['output']:>8,}"
        )

    if data["top_sessions"]:
        lines.append("")
        lines.append("最贵的会话：")
        for row in data["top_sessions"]:
            cost = f"${row['cost_usd']:>6.2f}" if row["cost_usd"] is not None else "     -"
            tid = row["task_id"] if row["task_id"] is not None else "main"
            lines.append(
                f"  {cost}  {str(row['role']):<9} task {str(tid):<5} "
                f"run {str(row['run_id']):<4} 输入 {row['input_tokens']:>10,}"
                f"（缓存 {row['cache_read']:>10,}）输出 {row['output_tokens']:>7,}"
            )

    lines.append("")
    lines.append("怎么省（按可省的量排序）：")
    lines.append("  1. 轮数 —— 成本 ≈ 轮数 × 上下文大小，每轮都把整份 transcript 再读一遍。")
    lines.append("  2. 上下文 —— thinking 占模型读回内容的 65%（实测），且每轮重复读。")
    lines.append("     这个 CLI 没有 --thinking，只有 --effort；填 [models.effort] 才生效。")
    lines.append("  3. 大块工具输出 —— 用 re_* 工具把证据留在磁盘、按需取回，别直接 dump 进上下文。")
    lines.append("  缓存已经在 94.7%，不要再往这上面投入。")
    if data["effort"]:
        pairs = "  ".join(f"{k}={v}" for k, v in sorted(data["effort"].items()))
        lines.append(f"  当前 effort 配置：{pairs}")
    else:
        lines.append("  当前 effort 配置：未设置（全部交给 CLI 默认）")
    return "\n".join(lines)
