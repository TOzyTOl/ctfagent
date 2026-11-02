---
name: ctf-reverse
description: Provides reverse engineering techniques for CTF challenges. Use when the main job is to understand how a compiled, obfuscated, packed, or virtualized target works before exploiting or solving it, including binaries, APKs, WASM, firmware, custom VMs, bytecode, game clients, malware-like loaders, and anti-debug or anti-analysis logic. Do not use it when the vulnerability is already understood and the remaining task is exploitation; use pwn instead. Do not use it for pure web workflows, log or disk forensics, or standalone crypto problems unless reversing the implementation is the real blocker.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# 逆向工程

**入口只做判别，不要通读。** 选题看 [INDEX.md](INDEX.md)，一次最多两篇。

## 先定语言和平台

```bash
file ./chall; ls -la
strings -a ./chall | head -30
readelf -p .comment ./chall 2>/dev/null; objdump -s -j .comment ./chall 2>/dev/null | head
```

`strings` 里有 Go build ID / rustc / MSVC / PyInstaller / Unity 字样，基本就定了路线。

## 判别树

| 现象 | 方向 | 读 |
|---|---|---|
| 校验逻辑藏得深、函数巨大 | 从「正确/错误」字符串的交叉引用倒推 | patterns.md |
| 有反调试 / 反 VM | 先 patch 掉再跑 | anti-analysis.md、anti-analysis-ctf.md |
| Go / Rust / Swift / JVM | 按语言运行时特性找主逻辑 | languages-compiled.md |
| Python 打包成 exe | 提取 pyc 或直接 unmarshal | languages.md |
| 自定义 VM / 字节码 | 提 opcode 表 → 自己写解释器 | patterns.md |
| 跑起来比静态看快 | Frida / angr / Qiling | tools-dynamic.md、tools-emulation.md |
| 游戏或引擎资源 | 提资源再解格式 | languages-platforms.md |
| 有硬件/固件 | binwalk 拆 → 找架构 → 反汇编 | platforms-hardware.md |
| 加壳 / VMProtect / Themida | 先脱壳，别硬跟 | tools-advanced.md |

## 纪律

1. **先找校验函数的入口**：搜 "Correct"、"Wrong"、"flag" 的引用，绝大多数题都从那一个 cmp 开始。
2. angr 只在分支清晰、约束可枚举时用；逻辑一复杂就退回动态调。
3. 能 patch 就别硬算——把 `jz` 改成 `jnz` 看行为，比读完整个函数快十倍。
4. 记录每一个已知的偏移和结构体布局，别靠记忆。

## 转出

- 看懂了、要打利用 → `/ctf-pwn`
- 核心逻辑是密码算法 → `/ctf-crypto`
- 是真实恶意样本（C2、驻留、规避） → `/ctf-malware`
- 只是 pyjail / 玩具 VM / 编码谜题 → `/ctf-misc`
- 目标其实是 Web 前端脚本 → `/ctf-web`

## 细节

[INDEX.md](INDEX.md)：18 篇、392K。`patterns-ctf-3.md`(38K)、`field-notes.md`(32K)、`patterns-ctf.md`(30K) **只 grep 片段**。
