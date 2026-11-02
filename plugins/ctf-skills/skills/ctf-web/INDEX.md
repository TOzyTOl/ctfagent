# ctf-web 索引

31 篇 · 合计 472K。**这张表是唯一目录来源**，入口 SKILL.md 不重复列举。

按体积降序。选题策略：先看「主题」是否命中症状，命中再读；一次最多读 2 篇，读完先动手验证再读下一篇。

| 文件 | 体积 | 主题（命中的技术名就是读取理由） |
|---|---|---|
| [server-side-exec-2.md](server-side-exec-2.md) | 39K | SQL Injection via DNS Records (PlaidCTF 2014) / Java Deserialization (ysoserial) / Python Pickle Deserialization / Race Conditions (Time-of-Check to Time-of-Use) |
| [sql-injection.md](sql-injection.md) | 38K | Backslash Escape Quote Bypass / Hex Encoding for Quote Bypass / Second-Order SQL Injection / SQLi LIKE Character Brute-Force |
| [client-side-advanced.md](client-side-advanced.md) | 37K | Hyperscript CDN CSP Bypass (UNbreakable 2026) |
| [field-notes.md](field-notes.md) | 35K | Reconnaissance / SQL Injection Quick Reference / XSS Quick Reference / XSSI via JSONP Callback Exfiltration |
| [auth-and-access.md](auth-and-access.md) | 35K | Password/Secret Inference from Public Data / Weak Signature/Hash Validation Bypass / Client-Side Access Gate Bypass / NoSQL Injection (MongoDB) |
| [server-side.md](server-side.md) | 29K | PHP Type Juggling / PHP File Inclusion / php://filter / SQL Injection / SSTI (Server-Side Template Injection) |
| [server-side-advanced-2.md](server-side-advanced-2.md) | 26K | SSRF to Docker API RCE Chain (H7CTF 2025) / Base64-Encoded Path Traversal (Sharif CTF 2016) |
| [server-side-advanced-4.md](server-side-advanced-4.md) | 22K | ZIP Upload with PHP Webshell (Nullcon 2026) |
| [client-side.md](client-side.md) | 22K | XSS Payloads / DOMPurify Bypass via Trusted Backend Routes / JavaScript String Replace Exploitation / Client-Side Path Traversal (CSPT) |
| [server-side-exec.md](server-side-exec.md) | 21K | Ruby Code Injection / Perl open() RCE / LaTeX Injection RCE (Hack.lu CTF 2012) / Server-Side JS eval Blocklist Bypass |
| [server-side-advanced.md](server-side-advanced.md) | 16K | Zip Symlink Path Traversal (UTCTF 2024) / Path Traversal Bypass Techniques / Flask/Werkzeug Debug Mode Exploitation / XXE with External DTD Filter Bypass |
| [cves.md](cves.md) | 16K | CVE-2025-29927: Next.js Middleware Bypass / CVE-2025-0167: Curl .netrc Credential Leakage / Uvicorn CRLF Injection (Unpatched N-Day) / Python urllib Scheme Validation Bypass (0-Day) |
| [web3.md](web3.md) | 15K | Challenge Infrastructure Pattern / EIP-1967 Proxy Pattern Exploitation / ABI Coder v1 vs v2 - Dirty Address Bypass / Non-Standard ABI Calldata Encoding |
| [server-side-2.md](server-side-2.md) | 15K | XXE (XML External Entity) / Command Injection |
| [auth-infra.md](auth-infra.md) | 14K | OAuth/OIDC Exploitation / CORS Misconfiguration / Git History Credential Leakage (Barrier HTB) / CI/CD Variable Credential Theft (Barrier HTB) |
| [learned-20261101-cbd81772.md](learned-20261101-cbd81772.md) | 9K | 什么时候用 / 第一步：把错误码读成状态机，而不是读成「失败」 / 第二步：证明「类型检查在反序列化之后」 / 第三步：没有现成链时，在框架 core 里拉四张表 |
| [auth-jwt.md](auth-jwt.md) | 9K | Algorithm None / Algorithm Confusion (RS256 to HS256) / Weak Secret Brute-Force / Unverified Signature (Crypto-Cat) |
| [server-side-advanced-3.md](server-side-advanced-3.md) | 7K | Xalan XSLT math:random() Seed Guess (35C3 2018) |
| [node-and-prototype.md](node-and-prototype.md) | 7K | Prototype Pollution Basics / Gadget: Library Settings via Prototype Chain / Node.js VM Sandbox Escape / Affected Libraries |
| [learned-20261027-db0e3465.md](learned-20261027-db0e3465.md) | 6K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261027-a5ceff97.md](learned-20261027-a5ceff97.md) | 6K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261031-842a670b.md](learned-20261031-842a670b.md) | 6K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261027-ee91c69d.md](learned-20261027-ee91c69d.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261027-e5d03c82.md](learned-20261027-e5d03c82.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261029-6ca3b59f.md](learned-20261029-6ca3b59f.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261101-ede5f1de.md](learned-20261101-ede5f1de.md) | 5K | 什么时候用 / 第一步：确认转发语义，别用近似实验下结论 / 第二步：把回包变成泄漏通道 / 第三步：从写到控 |
| [auth-and-access-2.md](auth-and-access-2.md) | 5K | SRP A=0, A=N Auth Bypass (OTW Advent 2018) |
| [learned-20261031-b7d3f10a.md](learned-20261031-b7d3f10a.md) | 5K | 什么时候用 / 怎么做 / 解锁后能接什么 / 坑与判据 |
| [learned-20261027-d3bebc53.md](learned-20261027-d3bebc53.md) | 5K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261027-5123cbe3.md](learned-20261027-5123cbe3.md) | 4K | 什么时候用 / 怎么做 / 坑 / 边界 |
| [learned-20261031-e17752f7.md](learned-20261031-e17752f7.md) | 3K | 什么时候用 / 怎么做 / 坑与判据 |

## 大文件（>25K，建议只 grep 片段而非整篇读入）

- `server-side-exec-2.md` 39K
- `sql-injection.md` 38K
- `client-side-advanced.md` 37K
- `field-notes.md` 35K
- `auth-and-access.md` 35K
- `server-side.md` 29K
- `server-side-advanced-2.md` 26K
