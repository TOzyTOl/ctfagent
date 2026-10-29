# batch-2026-10：18 道 2026 年真题（未解题，纯素材）

按你的要求，这一批**没有解**，只做了取件、筛除和登记所需的目录结构，18 道题放在
`D:\agentsec\CTFAGENT\challenges\batch-2026-10\` 这一个文件夹下，每道题一个子目录：

```
batch-2026-10/
  <slug>/
    challenge.yaml      # 题面 + 方向 + flag 格式 + 提示
    materials/          # 原始 handout（运行时会整目录复制进工作区 attachments/）
```

来源仍是 [sajjadium/ctf-archives](https://github.com/sajjadium/ctf-archives)（2026-10-25
快照）。四场比赛的时间按 ctftime 事件页核对过：

| 比赛 | ctftime | 时间 | 题数 |
|---|---|---|---|
| DiceCTF 2026 Quals | [3110](https://ctftime.org/event/3110) | 2026-03-07 ~ 03-08 | 4 |
| NCTF 2026 | [3206](https://ctftime.org/event/3206) | 2026-04-04 ~ 04-05 | 4 |
| SekaiCTF 2026 | [3113](https://ctftime.org/event/3113) | 2026-06-27 ~ 06-29 | 2 |
| Cryovault 2026 Quals | [3441](https://ctftime.org/event/3441) | 2026-10-10 ~ 10-11 | 8 |

## 清单

| 目录 | 比赛 | 方向 | 附件 | 备注 |
|---|---|---|---|---|
| `dice-plane-or-exchange` | DiceCTF | crypto | protocol.py, public.txt | 127 解，纯离线 |
| `dice-bedtime` | DiceCTF | reverse | bedtime (ELF) | 17 解，静态 elf 要 Linux 下跑 |
| `dice-locked-in` | DiceCTF | reverse | locked_in, flag_verifier.bin | 7 解 |
| `dice-interpreter-required` | DiceCTF | reverse | interpreter, flag_riddle.txt | 53 解 |
| `nctf-ez-rsa` | NCTF | crypto | task.py（末注释含实例数据） | |
| `nctf-hard-rsa` | NCTF | crypto | task.py（末注释含实例数据） | |
| `nctf-vm-encryptor` | NCTF | reverse | vm-encryptor.exe, code.bin | Windows PE + 字节码 |
| `nctf-ez-protocol` | NCTF | misc | protocol.txt, test.pcapng | 流量里是 XOR 过的 JSON |
| `sekai-orbital-strike` | SekaiCTF | crypto | chall.py（末注释含实例数据） | HARD，43 解 |
| `sekai-untitled-encore` | SekaiCTF | reverse | untitled-encore.exe | NORMAL，173 解 |
| `cryovault-half-truths` | Cryovault | crypto | chall.py, output.json | Shamir + 大量错误份额 |
| `cryovault-sparse-change` | Cryovault | crypto | chall.py, output.json | 稀疏多项式插值 |
| `cryovault-implicit-twins` | Cryovault | crypto | chall.py, output.json | 双 RSA，共享低位 |
| `cryovault-ouroboros` | Cryovault | reverse | chall (ELF) | 32 字节输入，**答案可能不是 isfcr{} 形式** |
| `cryovault-the-recurrence` | Cryovault | reverse | chall (ELF) | 28 字节输入，同上 |
| `cryovault-image-that-forgot` | Cryovault | forensics | events.jsonl, images.tar, vault.json | 证据字段 → scrypt → AESGCM |
| `cryovault-last-committed-message` | Cryovault | forensics | case.db, case.db-wal | SQLite WAL 恢复 |
| `cryovault-lossless-not-harmless` | Cryovault | forensics | mosaic_0417.png | 非标准 PNG 编码 |

合计 4.1 MB，18 份 handout 都已解压/摊平到 `materials/`（归档格式不再是原 zip，
省得 agent 多做一步解包；内容未改动）。

## 怎么喂给平台

每道题一个目录，网页 `/new` 页面的「已有题目目录？直接登记」一次只接受一个路径，
所以有两条路：

```powershell
# 单跑一道
.venv\Scripts\python.exe -m ctfagent run challenges\batch-2026-10\dice-plane-or-exchange
```

```powershell
# 网页登记：把每个子目录路径依次填进 /new 的那个输入框
D:\agentsec\CTFAGENT\challenges\batch-2026-10\dice-plane-or-exchange
D:\agentsec\CTFAGENT\challenges\batch-2026-10\dice-bedtime
...
```

我已经用 TestClient 把 18 个目录都走过一次 `POST /api/challenges`，全部 303，
`/new` 页面也都列出来了，`Workspace.create` 会把 `materials/` 整个复制进工作区
`attachments/`——也就是说登记这一步是通的，不需要你手改什么。**没有写进
`bench/bench.yaml`**，也没有登记进你那套正在用的数据库（我用的是临时库）。

## 选材时排除掉的东西

不是为了凑数而凑数，这几类是明确排除的：

- **附件里直接躺着答案**：HITCON 的 `Works_on_My_Machine` 把 `FL4G=...` 明文写在
  `flag.env`；DEADFACE 那批有几道 handout 里就是答案文件。这类的「解」是 grep。
- **只能连远程实例才能做**：DiceCTF `another_onion`（要拉一份随机二进制、30 分钟内
  解完交 token）、Cryovault 的 `brine` / `moraine` / `blind_ratchet`（SESSION 协商）、
  R3CTF `pewpew` / `MineMaze` / `r3reach`（要 Windows 主机或 MC 客户端）、HITCON
  `uushell`。平台跑的是离线工作区，接不上就给不出真反馈。
- **附件带 `<REDACTED>` 占位、且没有别的可解材料**：例如某些 Cryovault 题的
  `load_flag()` 兜底值。这里留下的三道 Cryovault crypto 虽然脚本里也有
  `isfcr{REDACTED}`，但真正的密文在 `output.json` 的 `flag_ct` 里，能独立解出来，
  所以留下；对应的 `flag_format` 也加了负向断言 `(?!REDACTED\})`，防止把占位符当答案。
- **题目依赖框架才能验**：R3CTF `HEuristic` 要先 `git clone` SEAL 再编译服务、
  `Inside` 用 sage 且要 PoW、`trustedhash` 要在 VM 里被远程 checker 喂 flag——
  离线判对错没有意义。

## 已知的不确定点（别把 agent 报的第一版答案当标准答案）

- `cryovault-ouroboros` / `cryovault-the-recurrence`：程序只说 `CORRECT`，二进制里
  没有 `isfcr` 字样。很可能答案是「被接受的那段输入」本身，而不是 `isfcr{...}`。
  这两道我故意没写 `flag_format`，让 agent 交候选、人工确认。
- `nctf-ez-protocol`：`test.pcapng` 里的报文是用 `NCTF` 做单字节循环异或的 JSON
  （我解出过 `{"password":"test","username":"test1"}` 和一条 `"level":"1"` 的响应），
  说明拿 `test1`/`test` 是登不进去的，要自己构造 admin 请求——**但这不等于答案已确认**，
  题目要求的 flag 仍然要靠自己拼请求去拿。
- `dice-interpreter-required` 的题面明说「别真的解释这段 puzzle，会 OOM」，附件里的
  `flag_riddle.txt` 是 1685 行的中文谜面，我没读懂，也没解。
- `cryovault-image-that-forgot` / `cryovault-last-committed-message`：题面把
  scrypt 参数、salt、nonce、密文全写出来了，这是**判分方式**而不是答案——证据字段
  凑错时 AESGCM 会直接抛异常，所以这两道天然自带校验。
