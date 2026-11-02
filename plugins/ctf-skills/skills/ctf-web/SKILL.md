---
name: ctf-web
description: Provides web exploitation techniques for CTF challenges. Use when the target is primarily an HTTP application, API, browser client, template engine, identity flow, or smart-contract frontend/backend surface, including XSS, SQLi, SSTI, SSRF, XXE, JWT, auth bypass, file upload, request smuggling, OAuth/OIDC, SAML, prototype pollution, and similar web bugs. Do not use it for native binary memory corruption, reverse engineering of standalone executables, disk or memory forensics, or pure cryptanalysis unless the web flaw is still the main path to the flag.
license: MIT
compatibility: Requires filesystem-based agent (Claude Code or similar) with bash, Python 3, and internet access for tool installation.
allowed-tools: Bash Read Write Edit Glob Grep Task WebFetch WebSearch
metadata:
  user-invocable: "false"
---

# Web 渗透

**入口只做判别，不要通读。** 选题看 [INDEX.md](INDEX.md)，一次最多两篇。

## 先建立基线

```bash
curl -si http://target/ -o base.txt; head -20 base.txt
curl -s http://target/robots.txt http://target/.git/config 2>/dev/null | head
```

**先存一份正常请求/响应，再开始 fuzz。** 没有基线，后面所有差异都不可信。

## 判别树

| 症状 | 先试 | 读 |
|---|---|---|
| 输入被模板渲染 | `{{7*7}}`、`${7*7}`、`<%= 7*7 %>` | server-side.md |
| 报错含 SQL 关键字 / 参数进查询 | 手工注入优先，别先上 sqlmap | sql-injection.md |
| 服务端按 URL 取回内容 | SSRF，先读云元数据与内网 | server-side-advanced-2.md |
| 有上传点 | 后缀、内容、解析差异三层 | server-side-advanced-4.md |
| 有 cookie / token | JWT 的 alg none 与 RS→HS 混淆 | auth-jwt.md |
| 逻辑全在前端 | 读 JS bundle、source map | client-side.md |
| 有 admin bot | 构造 XSS 让 bot 去访问 | client-side.md |
| 版本号/指纹明显 | 已知 CVE 优先 | cves.md |
| 给了源码 | 先通读找 sink，别猜 | field-notes.md |
| 链上合约 | EIP-1967 代理、ABI 编码差异 | web3.md |
| 只给二进制/WASM | 转 `/ctf-reverse` | — |

## 纪律

1. **先拿最小原语**：读一个文件、伪造一个 token、调一个内网接口。别一上来打完整链。
2. 同一参数先换三种编码（URL、双重、Unicode），再换位置（header、body、path）。
3. 对比「UI 提交的」和「后端接受的」——多出来的可选字段通常是后门。
4. 卡住先怀疑**两个解析器不一致**（代理 vs 应用、URL 解析 vs 取回、过滤器 vs 浏览器）。

## 转出

- 拿到 RCE 后难点变成内存破坏 → `/ctf-pwn`
- 本质是 JWT 数学 / 自定义 MAC → `/ctf-crypto`
- 要分析日志、PCAP、服务器镜像 → `/ctf-forensics`

## 细节

[INDEX.md](INDEX.md)：19 篇、417K。`server-side-exec-2.md`(39K)、`sql-injection.md`(38K)、`client-side-advanced.md`(37K)、`field-notes.md`(35K)、`auth-and-access.md`(35K) **只 grep 片段**。
