---
name: ctf-forensics
description: Provides digital forensics and signal analysis techniques for CTF challenges. Use when analyzing disk images, memory dumps, event logs, network captures, cryptocurrency transactions, steganography, PDF analysis, Windows registry, Volatility, PCAP, Docker images, coredumps, side-channel power traces, DTMF audio spectrograms, packet timing analysis, CD audio disc images, or recovering deleted files and credentials.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# 取证与信号

**入口只做判别，不要通读。** 选题看 [INDEX.md](INDEX.md)，一次最多两篇。

## 先三件事

```bash
file *; md5sum * | tee hashes.txt
binwalk -e --run-as=root artifact 2>/dev/null | head -20
exiftool artifact 2>/dev/null | head -25
```

先算哈希存证，再动任何东西——恢复类操作一旦写盘，原始状态就没了。

## 判别树

| 输入 | 先做 | 读 |
|---|---|---|
| 磁盘镜像 / dd / E01 | 分区表 → 文件系统 → 删除项恢复 | disk-recovery.md |
| 内存镜像 | volatility3 `pslist` → `malfind` → `dumpfiles` | disk-and-memory.md |
| PCAP | 协议分级统计 → 导出对象 → 跟随流 | network.md、network-advanced.md |
| 图片 | exiftool → binwalk → zsteg → 位平面 | steganography.md、stego-image.md |
| 音频 | 看频谱图，再查 DTMF / SSTV / 倒放 | stego-advanced.md |
| 日志 / evtx | evtx_dump + 建时间线 | windows.md、linux-forensics.md |
| 信号 / 硬件采集 | 定采样率 → 找帧头 → 解调 | signals-and-hardware.md |
| PDF | 对象流、增量更新、隐藏层 | steganography.md |
| 二维码 / 条码 | 拼块重建、纠错位 | stego-image.md |
| 外设流量（USB/蓝牙） | 按 HID 报文还原动作 | peripheral-capture.md |
| 3D 打印 / G-code | 解析路径 → 可视化 | 3d-printing.md |

## 纪律

1. **时间线优先于单点证据**：先按时间排序，异常点自己会浮出来。
2. 恢复文件用 photorec / testdisk / foremost，别手写解析器。
3. 拿到加密块先别急着破——先确认密钥是不是就在同一份镜像里。
4. 隐写先看容量：文件大小与像素数/采样数的比例，常常直接告诉你是 LSB 还是别的。

## 转出

- 恢复出来的是密文，难点是密码 → `/ctf-crypto`
- 证据指向恶意载荷或 C2 → `/ctf-malware`
- 需要还原的是 Web 应用逻辑 → `/ctf-web`
- 要追基础设施或公开信息 → `/ctf-osint`

## 细节

[INDEX.md](INDEX.md)：14 篇、339K。`disk-recovery.md`(31K)、`steganography.md`(30K)、`stego-image.md`(30K)、`signals-and-hardware.md`(28K) **只 grep 片段**。
