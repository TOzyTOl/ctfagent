---
name: ctf-crypto
description: Provides cryptography attack techniques for CTF challenges. Use when attacking encryption, hashing, signatures, ZKP, PRNG, or mathematical crypto problems involving RSA, AES, ECC, lattices, LWE, CVP, number theory, Coppersmith, Pollard, Wiener, padding oracle, GCM, key derivation, or stream/block cipher weaknesses.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# 密码学

**入口只做判别，不要通读。** 选题看 [INDEX.md](INDEX.md)，一次最多读两篇；读完先跑一个最小验证，再决定读不读下一篇。

## 先落盘再判断

```bash
file *; ls -la
grep -oE '\b(n|e|d|c|p|q|N|phi|g|y)\s*=' *.py *.sage 2>/dev/null | head -20
```

要拿到四件事：模数位数、e 的大小、有没有 p/q/phi、是否给了 oracle 交互。

## 判别树

| 看到 | 先试 | 读 |
|---|---|---|
| 小 e（3/17）+ 单条密文 | 整数开方 / Coppersmith | rsa-attacks.md |
| e 很大、d 很小 | Wiener / Boneh-Durfee | rsa-attacks.md |
| 多组密文、同一 m、不同 n | Hastad 广播攻击 | rsa-attacks.md |
| 给了 oracle（解密/填充/校验） | Padding oracle、Bleichenbacher | modern-ciphers.md |
| ECB + 可控明文 | 逐字节爆破 | modern-ciphers.md |
| 输出是数字序列 | MT19937 / LCG / LFSR 状态恢复 | prng.md、prng-attacks.md |
| 哈希 + 原长度可猜 | 长度扩展 | modern-ciphers-2.md |
| 给了矩阵/多项式/小误差 | LWE、CVP、Coppersmith | lattice-and-lwe.md |
| 曲线参数可疑 | 奇异曲线、异常曲线、小阶子群 | ecc-attacks.md |
| 古典替换或移位 | 频率分析、词表 | classic-ciphers.md |
| 承诺 / 零知识交互 | ZKP 伪造、Fiat-Shamir 缺陷 | zkp-and-advanced.md |
| 一堆 CRC / 线性方程组 | GF(2) 线性代数 | zkp-and-advanced.md |

## 纪律

1. **先用自造样本验证脚本**（已知答案），再碰真实数据。否则分不清「思路错」和「代码错」。
2. 有 oracle 就先测边界（0、1、n-1），别急着写完整攻击。
3. 15 分钟无进展就换判别树里相邻的一行，不要在同一条路上调参。
4. 格类问题先确认嵌入维度，再谈 BKZ 参数；维度不对，参数调到天亮也没用。

## 转出

- 真正卡在理解二进制 / VM → `/ctf-reverse`
- 要先拆包、恢复磁盘或镜像 → `/ctf-forensics`
- 只是编码谜题、非密码分析 → `/ctf-misc`
- 涉及模型或对抗样本 → `/ctf-ai-ml`

## 细节

[INDEX.md](INDEX.md)：15 篇、331K。超过 25K 的（rsa-attacks-2、modern-ciphers、classic-ciphers）**只 grep 片段**，不要整篇读入。
