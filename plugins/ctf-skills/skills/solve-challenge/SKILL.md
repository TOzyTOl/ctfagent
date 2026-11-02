---
name: solve-challenge
description: Solves CTF challenges by performing first-pass triage, identifying the dominant category, and routing execution to the right specialized ctf-* skill. Use when the user gives you a challenge bundle, a remote service, a suspicious file, or only a vague challenge description and you must determine where to start. Do not use it when the category is already clear and a specialized skill can be invoked directly; this is the dispatcher and recon entrypoint, not the deepest reference for category-specific techniques.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access. Orchestrates other ctf-* skills.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch Skill
metadata:
  user-invocable: "true"
  argument-hint: "[category] [challenge-file-or-url]"
---

# CTF 调度入口

**一次只加载一个域技能。** 同时开两个域，上下文会被两份判别树互相污染。

## Step 0 三样东西先齐

题面原文、附件全量、目标地址（host:port / URL）。缺一样就先问，不要猜着开工。

## Step 1 30 秒分诊

```bash
file *; ls -la; du -sh *
strings -a * 2>/dev/null | grep -iE '(flag|ctf)\{' | head
```

| 证据 | 域 |
|---|---|
| 有 URL / HTTP 交互 | `ctf-web` |
| ELF/PE + 给了远程端口 | `ctf-pwn` |
| 二进制但只问「它做了什么」 | `ctf-reverse` |
| 镜像 / PCAP / 日志 / 音频 / 图片 | `ctf-forensics` |
| 数字、模数、密文、脚本 | `ctf-crypto` |
| 混淆脚本 / C2 / 样本 | `ctf-malware` |
| 只要「找出来是谁/在哪」 | `ctf-osint` |
| 以上都不贴 | `ctf-misc`（兜底，别当默认） |

判据优先看**输入形态**，其次看题面关键词。题面关键词经常是误导。

## Step 2 进域后

域入口只给判别树。按树走，读 `INDEX.md` 选题，一次最多两篇，读完立刻动手验证。

## Step 3 卡住时的四个换向动作

1. **质疑分类**：题面说的域不一定是真域（"web" 题可能要 JWT 数学）。
2. **降一级**：完整链打不通就先拿最小原语（读一个文件 / 伪造一个 token）。
3. **找遗漏**：隐藏文件、其他端口、响应头、源码注释、图片元数据。
4. **找简路**：默认口令、已知 CVE、逻辑缺陷——比精细利用快得多。

连续 2 个方向各 15 分钟无进展，回 Step 1 重判，而不是继续调参。

## Step 4 flag 必须能重放（防幻觉）

**不允许**把「看起来像 flag」的字符串直接当答案。判定标准：

- 用一条命令/一个脚本，**从原始输入重新跑出同一串**，并把该命令记下来；
- 有多个候选时，选能指回**预期产物**的那个，排除元数据噪声与诱饵；
- 报告里必须写清来源文件路径与提取命令。

```bash
grep -rniE '(flag|ctf|htb|pico|eno)\{' . | head
```

## Step 5 收尾

解出后调 `/ctf-writeup` 出报告。没解出也要留：试过什么、否证了什么、卡在哪一步——否证清单比成功路径更值钱。
