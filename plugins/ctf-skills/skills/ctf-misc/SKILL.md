---
name: ctf-misc
description: Provides miscellaneous CTF challenge techniques for problems that do not cleanly fit the main categories. Use for encoding puzzles, pyjails, bash jails, RF/SDR, DNS oddities, unicode tricks, esoteric languages, QR or audio puzzles, constraint solving, game theory, unusual sandbox escapes, and hybrid logic puzzles. Prefer a more specific skill first when the challenge is mainly web, pwn, reverse, forensics, malware, OSINT, or crypto. Treat this as the fallback skill for genuine cross-category or edge-case challenges, not the default starting point.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch Skill
metadata:
  user-invocable: "false"
---

# 杂项（兜底域）

**先确认别的域都不适用再进这里。** 通用判别看入口，具体技术在 [INDEX.md](INDEX.md)。

## 判别树

| 现象 | 先试 | 读 |
|---|---|---|
| 交互式但受限 | 先区分 pyjail / bashjail / 命令白名单 | pyjails.md、bashjails.md |
| 一层套一层的编码 | 逐层 `file` + 试解，记流水账 | encodings.md、encodings-advanced.md |
| 方程组 / 约束 / 位运算 | z3 建模 | `../ctf-crypto/zkp-and-advanced.md` |
| 游戏、字节码、WASM | 提取 marshal / 反编译 | games-and-vms.md、games-and-vms-3.md |
| IQ 文件 / SDR | 定采样率 → 找帧头 → 解调 | rf-sdr.md |
| 要提权（靶机内） | `sudo -l`、SUID、cron、docker 组 | linux-privesc.md |
| 平台自身操作 | CTFd API 直接拉题与提交 | ctfd-navigation.md |
| DNS 行为怪 | 记录类型、ECS、区域传送、rebinding | dns.md |
| 自助终端 / 服务逃逸 | 快捷方式、设备管理器、VNC | kiosk-and-service-escape.md |

## 纪律

1. 编码题记流水账：每解一层就写下「输入指纹 → 判据 → 工具」，否则第三层就迷路。
2. jail 题先枚举**你能用什么**（可用字符、可用函数、可用模块），再想怎么拼。
3. 兜底域最容易发散：**20 分钟没有可复现进展就回主域重判一次**。

## 转出

- 是真正的内存破坏 → `/ctf-pwn`
- 是密码或数论 → `/ctf-crypto`
- 要先恢复文件 → `/ctf-forensics`
- 是真实二进制 → `/ctf-reverse`

## 细节

[INDEX.md](INDEX.md)：14 篇、236K。`games-and-vms-3.md`(32K) **只 grep 片段**。
