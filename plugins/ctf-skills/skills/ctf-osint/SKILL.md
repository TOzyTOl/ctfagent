---
name: ctf-osint
description: Provides open source intelligence techniques for CTF challenges. Use when gathering information from public sources, social media, geolocation, DNS records, username enumeration, reverse image search, Google dorking, Wayback Machine, Tor relays, FEC filings, or identifying unknown data like hashes and coordinates.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for OSINT lookups.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# 开源情报（OSINT）

**入口只做判别，不要通读。** 选题看 [INDEX.md](INDEX.md)。

## 判别树

| 拿到什么 | 先试 | 读 |
|---|---|---|
| 一张照片，要找地点 | 反向图搜 → 文字/招牌 → 阴影与太阳角 → 植被与车牌 | geolocation-and-media.md |
| 一个用户名 | 跨平台枚举，找同名账号的复用痕迹 | social-media.md |
| 一个域名 | `dig` 全记录类型、`crt.sh`、whois、Wayback | web-and-dns.md |
| 一个哈希 / 未知串 | 先识别类型再查库 | web-and-dns.md |
| 一段引文 / 一张截图 | 精确短语搜索 + 时间范围收窄 | web-and-dns.md |

## 纪律

1. **先固定可验证的锚点**（经纬度、时间、账号 ID），再往四周扩；纯猜会发散。
2. 每条结论都要能指回一个 URL 或一条命令输出，否则标为待证。
3. 图搜无果就换语言搜：同一地标在不同语种的名称往往能搜到不同结果。

## 转出

- 拿到了本地文件要提取 → `/ctf-forensics`
- 要真的去打这个服务 → `/ctf-web`
- 追到恶意样本或 C2 → `/ctf-malware`

## 细节

[INDEX.md](INDEX.md)：3 篇、51K，都不大，可以整篇读。
