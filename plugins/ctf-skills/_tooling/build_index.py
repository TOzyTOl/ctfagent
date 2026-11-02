#!/usr/bin/env python3
"""为每个 ctf-* 域生成 INDEX.md：文件 → 体积 → 主题 → 触发条件。

入口文件（SKILL.md）只放判别树，正文目录由本脚本生成，保证永不与文件列表脱节。
新增或改名一篇笔记后重跑一次即可。

用法：
    python _tooling/build_index.py           # 重建全部 INDEX.md
    python _tooling/build_index.py --check   # 只报告索引是否过期
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 兼容两种布局：工具放在 skills/ 内，或放在其父目录
if (ROOT / "skills").is_dir():
    ROOT = ROOT / "skills"
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)
H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)

# 这些二级标题是套路化的，不携带主题信息
NOISE = {
    "table of contents",
    "prerequisites",
    "additional resources",
    "when to pivot",
    "quick start commands",
    "useful tools",
    "useful commands",
    "useful one-liners",
    "common patterns",
    "common flag locations",
    "methodology",
    "workflow",
    "overview",
}

TRIGGER_RE = re.compile(r"^##\s+(when to use|when to reach for|使用时机|先看这里)\s*$", re.M | re.I)


def read(p: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def topics(text: str, limit: int = 4) -> list[str]:
    out = []
    for m in H2_RE.finditer(text):
        t = m.group(1).strip()
        if t.lower() in NOISE or len(t) > 48:
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def trigger(text: str) -> str:
    m = TRIGGER_RE.search(text)
    if not m:
        return ""
    tail = text[m.end() :]
    for line in tail.splitlines():
        line = line.strip()
        if line.startswith("##"):
            break
        if line.startswith("-") or line.startswith("*"):
            return line.lstrip("-* ").strip()[:80]
    return ""


def human(n: int) -> str:
    return f"{n/1024:.0f}K" if n >= 1024 else f"{n}B"


def build(domain: Path) -> str:
    files = sorted(
        (p for p in domain.rglob("*.md") if p.name not in {"SKILL.md", "INDEX.md"}),
        key=lambda p: (-p.stat().st_size),
    )
    rows = []
    for p in files:
        text = read(p)
        h1 = H1_RE.search(text)
        title = h1.group(1).strip() if h1 else p.stem
        tp = " / ".join(topics(text)) or "—"
        tr = trigger(text)
        relp = p.relative_to(domain).as_posix()
        rows.append((relp, human(p.stat().st_size), tp, tr, title))

    total = sum(p.stat().st_size for p in files)
    has_trigger = any(r[3] for r in rows)
    lines = [
        f"# {domain.name} 索引",
        "",
        f"{len(files)} 篇 · 合计 {human(total)}。**这张表是唯一目录来源**，入口 SKILL.md 不重复列举。",
        "",
        "按体积降序。选题策略：先看「主题」是否命中症状，命中再读；一次最多读 2 篇，读完先动手验证再读下一篇。",
        "",
    ]
    if has_trigger:
        lines += ["| 文件 | 体积 | 主题 | 触发条件 |", "|---|---|---|---|"]
        for relp, size, tp, tr, _ in rows:
            lines.append(f"| [{relp}]({relp}) | {size} | {tp} | {tr or '—'} |")
    else:
        lines += ["| 文件 | 体积 | 主题（命中的技术名就是读取理由） |", "|---|---|---|"]
        for relp, size, tp, _tr, _ in rows:
            lines.append(f"| [{relp}]({relp}) | {size} | {tp} |")

    lines += [
        "",
        "## 大文件（>25K，建议只 grep 片段而非整篇读入）",
        "",
    ]
    big = [r for r in rows if int(r[1].rstrip("K")) > 25 and r[1].endswith("K")]
    lines += [f"- `{r[0]}` {r[1]}" for r in big] or ["- 无"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    stale = 0
    for domain in sorted(p for p in ROOT.iterdir() if p.is_dir() and p.name.startswith("ctf-")):
        if not any(x.name != "SKILL.md" for x in domain.glob("*.md")):
            continue
        target = domain / "INDEX.md"
        content = build(domain)
        if target.exists() and read(target) == content:
            continue
        stale += 1
        if args.check:
            print(f"过期: {target.relative_to(ROOT)}")
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
            print(f"已生成: {target.relative_to(ROOT)}  ({len(content)}B)")
    if args.check and not stale:
        print("索引全部最新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
