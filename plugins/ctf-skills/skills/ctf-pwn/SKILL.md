---
name: ctf-pwn
description: Provides binary exploitation techniques for CTF challenges. Use when you already have a vulnerable native target or service and need to turn memory corruption or low-level primitives into code execution or privilege escalation, such as buffer overflows, format strings, heap bugs, ROP, ret2libc, shellcode, kernel exploitation, seccomp bypass, sandbox escape, or Windows/Linux exploit chains. Do not use it when the main blocker is understanding what the binary does; use reverse engineering first. Do not use it for pure web bugs, disk or packet forensics, or standalone crypto/math challenges.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# 二进制利用（Pwn）

**入口只做判别，不要通读。** 选题看 [INDEX.md](INDEX.md)，一次最多两篇。

## 第一件事永远是 checksec

```bash
file ./chall; checksec --file=./chall 2>/dev/null || pwn checksec ./chall
strings -a ./chall | grep -E 'GNU C Library|version' | head -3
```

还要拿到：libc 版本（有就 `strings libc.so.6 | grep "GNU C Library"`）、远程地址端口、是否静态链接。

## 按保护选路

| 保护组合 | 打法 | 读 |
|---|---|---|
| 无 canary、无 NX | 直接 shellcode | rop-and-shellcode.md |
| 无 canary、有 NX | ret2libc / ROP | rop-and-shellcode.md |
| 有 canary 且有泄漏点 | 先泄漏 canary 再溢出 | overflow-basics.md |
| 静态链接 | ret2syscall、syscall gadget | rop-and-shellcode.md |
| 有格式化字符串 | 任意写 → GOT / 返回地址 | format-string.md |
| 菜单式 malloc/free | tcache / unsorted bin / UAF | heap-techniques.md、field-notes.md |
| 有 seccomp | ORW（open-read-write） | rop-advanced.md |
| 内核模块 / 驱动 | 提权原语 + KASLR 绕过 | kernel.md、kernel-bypass.md |
| 自写 VM / 解释器 | 先 reverse，再找越界 | advanced-exploits.md |
| 无二进制只有远程 | 盲打，用长度/回显推断 | format-string.md |

## 纪律

1. **libc 版本没确认之前，别写偏移。** 版本错一步，后面全废。
2. 本地打通再打远程。远程挂了先怀疑时序（`recvuntil` 边界、sleep、缓冲）。
3. 每次崩溃都看 core / 回显的寄存器，不要盲调偏移量。
4. 泄漏优先于猜测：能泄漏就不爆破，能爆破就不猜。

## 转出

- 还不懂二进制在干什么 → `/ctf-reverse`（先逆向，别硬打）
- 入口其实是 Web 端点或上传 → `/ctf-web`
- 动手前要先破一个密码原语 → `/ctf-crypto`
- 是受限 shell / pyjail 而非内存破坏 → `/ctf-misc`

## 细节

[INDEX.md](INDEX.md)：18 篇、428K。前十篇都超过 25K，**只 grep 关键词取片段**。
