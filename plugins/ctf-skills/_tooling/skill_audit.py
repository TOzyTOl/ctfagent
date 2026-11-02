#!/usr/bin/env python3
"""技能库体检：入口体量、链接有效性、跨域重复、可检索性。

用法：
    python _tooling/skill_audit.py            # 打印报告
    python _tooling/skill_audit.py --json     # 输出 JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 兼容两种布局：工具放在 skills/ 内，或放在其父目录
if (ROOT / "skills").is_dir():
    ROOT = ROOT / "skills"
ENTRY_BUDGET = 2400  # 入口字符数上限（中英混排下约 1400–1700 token）

# 链接形如 [text](file.md) 或 [text](file.md#anchor)
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#]+\.md)(?:#[^)]*)?\)")
HEAD_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.M)

# 结构性标题，不携带「技术主题」信息，不参与跨域重复统计
STRUCTURAL = {"table of contents", "references", "see also", "further reading"}


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT)).replace(os.sep, "/")


def read(p: Path) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return p.read_text(encoding="utf-8", errors="replace")


def frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def collect() -> dict:
    domains = sorted(p for p in ROOT.iterdir() if p.is_dir() and (p.name.startswith("ctf-") or p.name == "solve-challenge"))
    report = {"domains": [], "totals": {}, "dead_links": [], "dup_titles": [], "orphans": []}
    all_titles: dict[str, list[str]] = {}
    referenced: set[str] = set()

    for d in domains:
        entry = d / "SKILL.md"
        files = sorted(p for p in d.rglob("*.md"))
        size = sum(p.stat().st_size for p in files)
        ent_size = entry.stat().st_size if entry.exists() else 0
        text = read(entry) if entry.exists() else ""
        fm = frontmatter(text)

        # 入口 + 索引内的链接（索引是入口的目录层，二者共同构成发现路径）
        links = []
        scope = text
        index = d / "INDEX.md"
        if index.exists():
            scope = text + "\n" + read(index)
        for m in LINK_RE.finditer(scope):
            target = (d / m.group(1)).resolve()
            ok = target.exists()
            links.append({"target": m.group(1), "ok": ok})
            if ok:
                referenced.add(str(target))
            else:
                report["dead_links"].append(f"{rel(d)}/SKILL.md -> {m.group(1)}")

        sections = [m.group(2).strip() for m in HEAD_RE.finditer(text)]
        code_blocks = len(re.findall(r"^```", text, re.M)) // 2

        report["domains"].append(
            {
                "domain": d.name,
                "name": fm.get("name", ""),
                "files": len(files),
                "bytes": size,
                "entry_bytes": ent_size,
                "entry_chars": len(text),
                "entry_over_budget": max(0, len(text) - ENTRY_BUDGET),
                "links": len(links),
                "links_ok": sum(1 for x in links if x["ok"]),
                "unlinked_files": sorted(
                    rel(p) for p in files if p.name != "SKILL.md" and str(p.resolve()) not in referenced
                ),
                "sections": len(sections),
                "code_blocks": code_blocks,
                "has_description": "description" in fm,
                "has_allowed_tools": "allowed-tools" in fm,
            }
        )

        # 跨域重复标题（技术主题撞车）
        for p in files:
            if p.name in {"SKILL.md", "INDEX.md"}:
                continue
            for m in re.finditer(r"^##\s+(.+?)\s*$", read(p), re.M):
                t = m.group(1).strip().lower()
                if t in STRUCTURAL:
                    continue
                if len(t) > 6:
                    all_titles.setdefault(t, []).append(rel(p))

    for t, paths in all_titles.items():
        doms = {p.split("/")[0] for p in paths}
        if len(doms) > 1:
            report["dup_titles"].append({"title": t, "domains": sorted(doms), "files": paths[:6]})

    report["dup_titles"].sort(key=lambda x: -len(x["domains"]))
    report["totals"] = {
        "domains": len(domains),
        "files": sum(x["files"] for x in report["domains"]),
        "bytes": sum(x["bytes"] for x in report["domains"]),
        "entry_bytes": sum(x["entry_bytes"] for x in report["domains"]),
        "entry_chars": sum(x["entry_chars"] for x in report["domains"]),
        "entry_over_budget": sum(x["entry_over_budget"] for x in report["domains"]),
        "dead_links": len(report["dead_links"]),
        "dup_titles": len(report["dup_titles"]),
        "unlinked_files": sum(len(x["unlinked_files"]) for x in report["domains"]),
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = collect()

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    t = r["totals"]
    print("=" * 74)
    print("技能库体检")
    print("=" * 74)
    print(f"域 {t['domains']} 个 · 文件 {t['files']} 篇 · 总体积 {t['bytes']/1024:.0f}KB")
    print(f"入口合计 {t['entry_bytes']/1024:.1f}KB / {t['entry_chars']} 字符（约 {t['entry_chars']*0.7/1000:.0f}k token）")
    print(f"入口超预算合计 {t['entry_over_budget']} 字符 · 死链 {t['dead_links']} · 未挂链接的文件 {t['unlinked_files']} 篇")
    print()
    print(f"{'域':<16}{'文件':>4}{'体积':>8}{'入口字符':>9}{'超预算':>9}{'链接':>8}{'代码块':>7}")
    print("-" * 74)
    for d in sorted(r["domains"], key=lambda x: -x["entry_chars"]):
        flag = "!" if d["entry_over_budget"] else " "
        print(
            f"{flag}{d['domain']:<15}{d['files']:>4}{d['bytes']/1024:>7.0f}K"
            f"{d['entry_chars']:>9}{d['entry_over_budget']:>9}"
            f"{d['links_ok']:>5}/{d['links']:<2}{d['code_blocks']:>7}"
        )

    if r["dead_links"]:
        print()
        print("死链：")
        for x in r["dead_links"][:20]:
            print("  -", x)

    if r["dup_titles"]:
        print()
        print("跨域重复主题（同一技术在多个域里各写一遍）：")
        for x in r["dup_titles"][:15]:
            print(f"  - {x['title']}  [{'+'.join(x['domains'])}]")

    orphan = [f for d in r["domains"] for f in d["unlinked_files"]]
    if orphan:
        print()
        print(f"入口未挂链接的文件（模型无法从入口发现，{len(orphan)} 篇）：")
        for x in orphan[:20]:
            print("  -", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
