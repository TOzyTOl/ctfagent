# ctf-pwn 索引

23 篇 · 合计 457K。**这张表是唯一目录来源**，入口 SKILL.md 不重复列举。

按体积降序。选题策略：先看「主题」是否命中症状，命中再读；一次最多读 2 篇，读完先动手验证再读下一篇。

| 文件 | 体积 | 主题（命中的技术名就是读取理由） |
|---|---|---|
| [advanced-exploits.md](advanced-exploits.md) | 35K | VM Signed Comparison Bug (0xFun 2026) / Type Confusion in Interpreter (VuwCTF 2025) / Double win() Call Pattern (VuwCTF 2025) / DNS Record Buffer Overflow |
| [advanced-exploits-3.md](advanced-exploits-3.md) | 32K | Game AI Arithmetic Mean OOB Read (BSidesSF 2024) |
| [format-string.md](format-string.md) | 32K | Format String Basics / Argument Retargeting (Non-Positional %n Trick) / Blind Pwn (No Binary Provided) / Format String with Filter Bypass |
| [rop-advanced.md](rop-advanced.md) | 32K | Seccomp Bypass / Stack Shellcode with Input Reversal / .fini_array Hijack / pwntools Template |
| [advanced-exploits-4.md](advanced-exploits-4.md) | 30K | SeDebugPrivilege to SYSTEM (RainbowTwo HTB) / Forth Interpreter Command Execution (32C3 2015) |
| [advanced-exploits-2.md](advanced-exploits-2.md) | 30K | io_uring UAF with SQE Injection (ApoorvCTF 2026) |
| [rop-and-shellcode.md](rop-and-shellcode.md) | 28K | ROP Chain Building / ret2csu — __libc_csu_init Gadgets (Crypto-Cat) / Stack Pivot via xchg rax,esp (Crypto-Cat) / DynELF Automated Libc Discovery (RC3 CTF 2016) |
| [kernel.md](kernel.md) | 28K | Environment Setup and Recon / Useful Kernel Structures for Heap Spray / Kernel Stack Overflow and Canary Leak / Privilege Escalation Primitives |
| [overflow-basics.md](overflow-basics.md) | 26K | Stack Buffer Overflow / Struct Pointer Overwrite (Heap Menu Challenges) / Signed Integer Bypass (Negative Quantity) / Canary-Aware Partial Overflow |
| [field-notes.md](field-notes.md) | 26K | Heap Exploitation / Additional Exploit Notes |
| [heap-techniques.md](heap-techniques.md) | 24K | Heap Exploitation / Custom Allocator Exploitation / Classic Heap Unlink Attack (Crypto-Cat) / House of Orange |
| [heap-techniques-2.md](heap-techniques-2.md) | 20K | — |
| [kernel-techniques.md](kernel-techniques.md) | 19K | tty_struct RIP Hijack and kROP / userfaultfd Race Stabilization / SLUB Allocator Internals / Leak via Kernel Panic |
| [sandbox-escape.md](sandbox-escape.md) | 16K | Python Sandbox Escape / VM Exploitation (Custom Bytecode) / FUSE/CUSE Character Device Exploitation / Busybox/Restricted Shell Escalation |
| [kernel-bypass.md](kernel-bypass.md) | 15K | KASLR and FGKASLR Bypass / KPTI Bypass Methods / SMEP / SMAP Bypass / KPTI / SMEP / SMAP Quick Reference |
| [heap-fsop.md](heap-fsop.md) | 15K | realloc(ptr, 0) as free() for UAF (AceBear 2018) |
| [advanced.md](advanced.md) | 14K | Seccomp Advanced Techniques / rdx Control in ROP Chains / Use-After-Free (UAF) Exploitation / JIT Compilation Exploits |
| [learned-20261031-914b75b8.md](learned-20261031-914b75b8.md) | 7K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [vm-bytecode-gc.md](vm-bytecode-gc.md) | 7K | 1. 先摸协议，不要拿文本硬怼 / 2. 把值模型和对象布局钉死 / 3. 找“边界检查字段错位”的 1 字节越界 / 4. 用压缩式 GC 把“越界改 kind”升级成 stale 指针 |
| [advanced-exploits-5.md](advanced-exploits-5.md) | 6K | — |
| [learned-20261031-d2723ea6.md](learned-20261031-d2723ea6.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261101-d0891877.md](learned-20261101-d0891877.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261027-60d5d47e.md](learned-20261027-60d5d47e.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |

## 大文件（>25K，建议只 grep 片段而非整篇读入）

- `advanced-exploits.md` 35K
- `advanced-exploits-3.md` 32K
- `format-string.md` 32K
- `rop-advanced.md` 32K
- `advanced-exploits-4.md` 30K
- `advanced-exploits-2.md` 30K
- `rop-and-shellcode.md` 28K
- `kernel.md` 28K
- `overflow-basics.md` 26K
- `field-notes.md` 26K
