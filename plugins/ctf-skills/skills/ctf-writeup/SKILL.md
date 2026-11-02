---
name: ctf-writeup
description: Generates a single standardized submission-style CTF writeup for competition handoff and organizer review. Use after solving a CTF challenge to document the solution steps, tools used, and lessons learned in a structured format.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash and Python 3.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "true"
  argument-hint: "[challenge-name]"
---

# 报告生成

解出后写一份能被队友或主办方**快速复现**的报告。目标不是好看，是让人照着跑一遍就能拿到同一个 flag。

## Step 1 收料

```bash
find . -name '*.py' -o -name 'solve*' -o -name 'exploit*' | head -20
grep -rniE '(flag|ctf|htb|pico|eno)\{' . 2>/dev/null
```

要拿到：题名 / 赛事 / 分类 / 难度 / 分值 / flag 格式，以及解题脚本与关键命令输出。

## Step 2 写

写入 `writeup-<name>.md`，用下面的骨架：

```markdown
---
title: "<题名>"
ctf: "<赛事>"
date: YYYY-MM-DD
category: web|pwn|crypto|reverse|forensics|osint|malware|misc
difficulty: easy|medium|hard
points: <分值>
flag_format: "flag{...}"
---

# <题名>

## 摘要

<一到两句：这题是什么、核心手法是什么。>

## 解法

### 关键观察

<3–8 行说清破局点。>

```python
<从题目给的原始数据，一路跑到打印 flag 的完整脚本>
```

## Flag

```
flag{...}
```
```

## 硬规则

1. **一个脚本跑到底**：从原始输入开始，到打印 flag 结束。不要拆成「求密钥 / 解密 / 出 flag」三段碎片。
2. **一条路径**：不要列多个备选解法；选最干净的那条。
3. **只写关键步骤**：1–3 步。死路只在解释转向时提一句。
4. **必须贴真实输出**（过长可截断），证明跑通了。
5. **代码块带语言标签**，脚本含全部 import。
6. 除非用户明确要求，**保留真实 flag**，不要打码。
7. 报告里的每个结论都要能指回一条命令或一段代码；做不到的标为推测。

## 交之前自查

- [ ] 元数据齐全
- [ ] 脚本从零可跑通，能复现同一个 flag
- [ ] 没有残留占位符
- [ ] 没有真实凭据、密钥、内网地址
- [ ] 长度够短，能快速复核

## Challenge

$ARGUMENTS
