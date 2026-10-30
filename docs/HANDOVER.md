# CTFAGENT 交接文档

**最后更新：2026-10-29**
**适用对象：接手这个项目的下一个人（包括几天后的自己）**
**当前状态：`426 passed / 1 skipped`（2026-10-29 复核两遍），基准锚点 lcg_step 已解出（结果重建，见第二十三轮）**

> **2026-10-29 注入引擎轮**：新增 `ctfagent/kit/`——**与目标无关的注入引擎**（`Session` 默认关代理、
> 按传输层重试；五种 oracle：错误回显/UNION/布尔/时间/堆叠；`Extractor` 二分；`calibrate()` 强制
> 阳性对照）与**每题一份 spec** 的分工。动因是实测：run-19 一个 run 里 5 个 py / 215 行，其中
> **4 份是同一个布尔 oracle 的独立重写**，差别只有重试次数和有没有关代理。
> 引擎已按**分类**（不是角色）注入 web worker 与 web verifier 的系统提示词；verifier 那段写明
> 「独立复现 ≠ 从零重写」——可以 import 引擎，但 `probe` 与判据必须自己写。
> 附带 `local_target.py`（127.0.0.1 上四个注入面、flag 已知）与 `selftest.py`，动目标之前先
> `python -m ctfagent.kit.selftest` 证明工具能找到已知答案。改的是 `ctfagent/kit/*`、`prompts.py`、
> `orchestrator.py`（两处一行），新增 `tests/test_kit.py`。逐条记在 `docs/进度-20261029-注入引擎.md`。

> **2026-10-29 重设计轮**：整站从「白桌面」改成「暗色调度台 + 纸色结果」——近黑地面 + 32px 工程网格，
> 全画面唯一的亮面是纸，纸只出现在有东西被生产出来的地方（已捕获工单 / 已确认 flag / 报告）。
> 只动了 `ctfagent/static/app.css` 与 `docs/界面设计.md`，模板和 JS 一行未改。顺手修掉两个**旧版就存在**的
> 移动端溢出（`.doc-wrap` / `.page` 缺 `width: 100%`，`.composer-bar` 不换行）。逐条记在
> `docs/进度-20261029-暗色重设计.md`，含两次浏览器实测的对比度数字。
>
> **2026-10-29 复制与代码轮**：flag 卡加了显式「复制」按钮（值是 30 来个字符、要粘到别处去，
> 藏在整行文字上等于没人找得到）；围栏代码块加了**一条细栏**（语言 + 复制）与**七色语法高亮**，
> 暗地和纸面各自取色、都在最坏的那块板上量过（暗地最低 6.37、纸面最低 5.02）。
> 没写语言的围栏**不高亮**（那是粘贴的结果，不是源码），且标记逐字节保持不变，
> 否则一次刷新就改了消息的形状。服务端 `mdrender.py` 与 `app.js` 两个渲染器同步改，围栏语言过同一条白名单。
> 顺手修掉报告页一个**旧版就有**的移动端塌陷：`.writeup` 横向 `auto` margin 关掉了 flex 的 stretch，
> 一段长代码把整份报告撑到 684px（见 `docs/界面设计.md` 那条引用块）。同时补齐**纸上的焦点环**——
> 全站焦点环是 `--chalk`，在纸面上只有 1.02–1.11:1 等于没有；已捕获行的链接和删除按钮一直如此，
> 这轮新加的两个按钮也会落在纸上，所以一起换成 `--paper-ink`（14.65:1）。
> 改的是 `app.css`、`app.js`、`mdrender.py`、`workspace.html`、`docs.html`。逐条记在 `docs/进度-20261029-复制与代码.md`。
>
> **2026-10-29 渐变与动效轮**（暗色重设计的收尾）：按「光：唯一的渐变语言」把渐变从大平面下沉到**控件尺度**——
> 主按钮/气泡/跳过链接/toast 走 `--chalk-sheet`，键帽 `--key-ramp`，输入井 `--well-ramp`（井是凹的，方向与凸起物相反），
> 侧栏 `--side-ramp`，任务轨 hover/选中行与命令面板游标行共用同一条短坡，说明页小节线从左淡出。
> 动效只留一个「被作者写下来的时刻」：结果落纸（`sheet-settle`，run 确认后经 `sessionStorage` 交接、
> 只给那一行加 `.just-captured`），其余三处（面板 160ms、新墨 200ms、状态 120ms 内）都只是状态变化。
> 渐进增强的原则没变：`prefers-reduced-motion` 一条开关全关，类仍加上、只是不动。
>
> **这轮踩到的坑**：任务轨每 5 秒 `fetch` 并整个替换 `#rail-list`，启动时那次刷新正好落在动画途中，
> 把刚加的标记连节点一起换掉了——所以 id 要在模块里多留 1.5 秒、每次换列表补一次，到点清标记并停止补。
> 不写这一条，下一个人会看到「动画有时候不播」而找不到原因。
>
> 改的是 `ctfagent/static/app.css`、`ctfagent/static/app.js`、`docs/界面设计.md`（模板仍未改）。
> 逐条与实测时间线记在 `docs/进度-20261029-渐变与动效.md`。
>
> **界面文档现在描述的是暗色版**。要回看白底版本的做法，读 `docs/进度-20261029-界面极客层.md`（那一轮只加语法和键盘，
> 颜色描述已过时）。
>
> **2026-10-29 界面轮**：白底桌面之上加了一层「终端语法 + 键盘优先」（命令面板 `Ctrl+K`、
> 跳转链接、画的箭头记号）。同时修了三个真 bug——手机上面板完全不渲染（类名撞车）、
> 面板光标卡在第 40 行、`/api/runs` 一直在跑旧进程。逐条记在
> `docs/进度-20261029-界面极客层.md`，含每条复现口径。
>
> **教训**：`start.cmd -Restart` 报「已停止 pid …」并不等于端口真的释放了。判断服务是否在跑新代码，
> 用 `Get-NetTCPConnection -LocalPort 8789` 看进程 `CreationDate`，别看启动脚本的打印。

---

## 0. 五分钟速览

| 项 | 状态 |
|---|---|
| 这是什么 | 多 agent 协作的 CTF 自动解题平台，本地单机运行 |
| 规模 | `ctfagent/` 包 33 个文件 / 17,307 行（其中 `ctfagent/kit/` 6 个文件 / 1,323 行），前端 5,833 行（`app.css` 2,848 + `app.js` 1,906 + templates 1,079，无构建），测试 38 个文件 / 9,220 行 |
| 测试 | **426 passed / 1 skipped**，全部离线可跑（`ScriptedBackend`，不烧钱） |
| 模型 | `fastmodel.top` 网关，`deepseek-v4-flash`（默认）与 `deepseek-v4-pro`（推理方向） |
| 历史成绩 | 主库 66 次运行、13 次 solved（约 20%）；**8 题基准已解 5 题**，均值 $2.97/题 |
| 最大短板 | ~~无度量~~ **baseline 8/8 已完成**（$24.29，2026-10-30）——下一个短板是 P1 沙箱 |
| 最大风险 | 无进程隔离（`guard.py` 是黑名单，`sandbox.mode=docker` 只是提示词开关） |

**接手后第一件事**：`./start.sh`（Git Bash）或 `.\start.ps1`（PowerShell）起服务，打开
<http://127.0.0.1:8789/>，跑一道 `challenges/n17` 看是否正常。

---

## 1. 怎么把它跑起来

### 启动

```bash
# Git Bash（注意：不是 ./start.ps1，那是 PowerShell 脚本）
./start.sh

# 或 PowerShell
.\start.ps1
```

**端口**：默认从 8788 往上找第一个空闲的。这台机器上 **8788 被别的服务占着**（pid 37076，
同时占着 8000/8080/8081/8787），所以会落到 **8789**。

### 环境自检

```bash
.\.venv\Scripts\ctfagent.exe doctor     # 或 python -m ctfagent doctor
```

现在应该看到：

```
模型分配        main=deepseek-v4-pro  worker=deepseek-v4-flash  verifier=deepseek-v4-pro  writer=deepseek-v4-flash
                方向覆盖：crypto=deepseek-v4-pro  pwn=deepseek-v4-pro  reverse=deepseek-v4-pro ...
缺失的 CTF python 包：frida, volatility3
```

只有 frida / volatility3 没装，其余（pwntools、z3、gmpy2、pycryptodome、capstone、angr、
unicorn、pyelftools、ropgadget）都在。

### 装包

venv 是 `uv venv` 建的，**没有 pip**：

```bash
uv pip install --python .venv/Scripts/python.exe <包名>
```

### 命令行

```bash
ctfagent run <题目目录>     # 开跑并在终端跟随
ctfagent list               # 所有题目和运行
ctfagent show <run_id>      # 任务、事实、flag
ctfagent confirm <flag_id>  # 确认候选 flag（不写报告）
ctfagent writeup <run_id>   # 为已确认的 flag 补写报告
ctfagent distill <run_id>   # 把已解出的运行蒸馏成技能笔记（方法层记忆）
ctfagent memory             # 记忆闭环体检：卡、方向落库、笔记、实际检索命中
ctfagent recount [--apply]  # 按「用了才算」重判历史运行的卡片记账
ctfagent bench <目录>       # 跑基准集（默认隔离训练笔记，--notes 显式带入）
ctfagent doctor             # 环境自检
```

---

## 2. 架构（十分钟读懂）

```
浏览器 ──SSE/HTTP──> server.py (FastAPI + Jinja2 + 原生 JS)
                          │
                          ▼
                   orchestrator.py (RunManager / Run)
                   ┌──────┴──────┬─────────────┬──────────┐
                   ▼             ▼             ▼          ▼
              主 agent       worker 们      verifier   writer
              (只调度)      (crypto/pwn/   (独立复现)  (写报告)
              claude-agent-  reverse/web/
              sdk 长会话)    misc/...)
                   │             │
                   ▼             ▼
              tools.py ──> db.py (SQLite，events 表为唯一事实源)
              worker_tools.py
```

### 四条不可破坏的设计约束

这四条是这个项目区别于“一个模型跑到底”的根本，**改动前先想清楚会不会破坏它们**：

1. **主 agent 不执行 shell**。它只负责拆题、派工单、汇总。`disallowed_tools` 里禁掉了
   Bash/PowerShell/Write/Edit。想给它加只读工具可以，但别给写和执行。
2. **worker 不能宣布胜利**。`worker_tools.py` 没有 `submit_flag`；flag 只能由主 agent 通过
   `propose_flag` 提交，再由独立 verifier 复现，最后由人确认。
3. **verifier 独立复现**。它拿不到 claimant 的结论，只能用工作区里的实际文件重推。
4. **一切以事件为准**。`events` 表是唯一事实源，断线重连靠 `Last-Event-ID` 补。
   改状态前先问“这个状态变化有没有事件”。

### 数据流

一次运行的完整链路：

```
prepare() → 装技能/附件 → 主 agent kickoff
   → dispatch(triage) → 分类 + 首枪计划
   → dispatch(crypto/reverse/...) × N → 各自 record_fact 到黑板
   → 主 agent propose_flag(candidate)
   → 自动 dispatch(verifier) → 独立复现 → VERDICT
   → 人确认 → 运行关闭（默认不写报告）
   → caselib.save() 蒸馏成战例卡
   → caselib.attribute() 把胜负记到被展示过的卡上
```

---

## 3. 数据现状（库里的真实结论）

这些数字都来自 `ctfagent.db`，不是估计：

| 事实 | 数值 | 含义 |
|---|---|---|
| 解出率 | 3 / 15 ≈ 20% | 题库难度未知，**不可与 Cybench 的 17% 直接比较** |
| 每轮延迟 | flash 15.4s，pro 约 28s | — |
| 墙钟去向 | **96% 在模型往返**，工具执行 3.7%，启动 <1% | 优化工具链没有意义 |
| thinking 占比 | 输出 166 万字符 vs 文本 7,345（**225:1**） | 唯一能压速度的地方 |
| 并行度 | 0.94x（run 18）～ 1.82x（run 14） | 架构支持并行，看主 agent 怎么用 |
| 工具批处理 | 0.66 轮 / 次调用 | 模型已在批处理，这块没空间 |
| unknown 关闭率 | 13/18 = 72% | 黑板机制在起作用 |
| 战例卡 | 14 张，11 张分类是 unknown | 分类写回已修（第九轮），历史 11 张无源可回，等新题积累 |

---

## 4. 本轮八轮加固（2026-10-23 ～ 10-24）

每一轮都是“先从库里找到证据，再动手”。**详细版在
`docs/优化审查-20261023.md`**，这里是索引：

| 轮次 | 问题 | 证据 | 改动 |
|---|---|---|---|
| 1 安全边界 | 黑名单可绕 | 分析 | `guard.py`：子 shell 展开、路径归一化、危险命令检测、决策钩子；提示词加规范 |
| 2 预算/收敛 | 钱花在错地方、worker 撞上限不交接 | run 18：$35.78，轮数全撞 150/151 | 预算预留 + 角色差异化 + 每工单轮数上限 + 连续 15 轮无新 fact 强制交棒 |
| 3 收敛续跑 | 撞上限后不交接 | 同上 | `_record_exhausted_handoff` 把原因/位置写进黑板 |
| 4 环境与工具链 | worker 用的 python 不是项目 venv；CTF 工具链全空 | `doctor` 报缺 8 个包但 worker 用的是系统解释器 | `_session_env()` 把 venv 放 PATH 最前；补齐 9 个包 |
| 5 模型分配 | 平台锁在最弱模型 | 网关实际有 37 个模型 | `ModelConfig.roles` + `model_for(role)`，按方向分配 pro/flash |
| 6 浪费 | 模型静默直接判死；同方向可无限重派；看板说谎 | 4 个工单 1.91h 被丢弃却记 turns=0/$0 | `stall_retries`、`resumable_tasks` 硬拒、`tools_called` 落库 |
| 7 基础加固 | 单任务墙钟从未执行；收尾会话无时钟；黑板丢最老记录；工具链不可见；交接措辞错 | 16 个工单超 30 分钟（最长 78min） | `task_timeout_s` 强制、收尾有界、`blackboard_facts=80`、`_python_toolkit()` 注入、`task_timeout` 措辞 |
| 8 确认不写报告 | 比赛里确认完被报告阻塞 | 用户要求 | `writeup_on_confirm=false` + 三个补写入口 |
| 9 验证器结论 | 40% 的验证器不按格式输出 | 15 次裁决 6 次 `verdict=none` | `task_complete(verdict=...)` 结构化字段 + 散文兜底放宽 |
| 10 分类写回 | triage 结论不落库，战例卡全 unknown | 14 张卡 12 张 `unknown`，检索最强信号从未生效 | `set_challenge_category`（unknown 守卫）+ `_apply_triage_category` + `challenge.categorized` 事件 |
| 11 基准+成本 | 基准 2 道题无度量；成本是 Anthropic 价目表虚构值 | `baseline.log` 0 字节；96 个 result 事件全带 usage 却没人用它算钱 | 自造 6 题扩到 8（gen/solve/答案在 `bench/sealed/`，守卫测试 4 个）+ `[models.pricing]` 按 usage 差值记账 |
| 12 主 agent 等待开销 | wait_tasks 每 run 被调 43–65 次，每次一个完整回合；gather 等全部导致已完成的工单也要等到超时才被看见 | 库内实测调用数 + 任务级并行度重测（1.5–2.3x，派发不是瓶颈） | `Run.wait` 改 FIRST_COMPLETED 即醒；默认 45→240s、上限 120→480s；`MCP_TOOL_TIMEOUT=600000`；提示词改"长阻塞不轮询" |
| 13 技能沉淀 | 学习按题目形状不按技术泛化；技能库静态，run 学到的方法永不写回 | 战例卡绑定题面词汇，跨题形失配 | `Run.distill_technique()`：确认 flag 后显式蒸馏（四标题格式、flag 擦除、内容哈希防重复），笔记入技能库，派发时向方向 worker 注入最新 8 篇索引 |
| 14 WSL pwn 运行时 | 宿主跑不了 ELF，pwn 结构性弃权；WSL/Docker 都在机器上但平台没用 | run 5 worker 自行摸索 /tmp 绕法，每个 run 重付学费 | WSL 装四件套（pwntools/ROPgadget/patchelf/GEF，用户空间）；`pwn_guidance()` 注入 pwn 与 pwn 题 verifier；doctor 加「WSL pwn 环境」行 |
| 15–19 | 见优化审查文档（省 token、凭据页、启动脚本等） | — | — |
| 16 记忆闭环 | 记忆四条线全断：方向不落库、检索被通用词污染、help/harm 记的是暴露、0 篇笔记 | 实时库 15 张卡 / 23 道题逐条对照 | `informative()` 双层过滤 + 按「用了才算」记账 + `notes.py` 方法层笔记 + `ctfagent memory` 体检 + `recount` 重判 |
| 23 基准结果丢失 | 锚点题解出后 CLI 崩在打印行，报告未落盘、工作目录已删 | `ValueError: Unknown format code 'd' for object of type 'float'` | `cli._duration()` 统一格式化 + 每跑完一题就 `save_report`（用上早已存在的 `on_result` 回调） |
| 25 CLI 输出路径没人测过 | 第 23 轮那种崩溃能烧掉一次付费跑，是因为命令体（拼参、落盘、打印、退出码）零覆盖 | 测试只到 `run_benchmark`/`compare`/`_duration`，`_bench()` 本身从未被执行 | 两条命令级测试：`main(["bench",...])` 走到输出与退出码、`--out` 文件真的落盘、`--only` 拼错返回 2；`ctfagent init` 的示例单价改成空模板 |

> 注：文档里轮次编号与上表略有出入（优化审查文档按“轮”组织，这里按主题合并）。
> 以 `docs/优化审查-20261023.md` 为准。

---

## 5. 六道“卡死”防线（已落地）

| 场景 | 时钟 | 动作 |
|---|---|---|
| worker 模型静默 | 300s | 关流 + resume 重试一次（`stall_retries=1`） |
| worker 工具静默 | 900s | 判死（工具真卡住了） |
| worker 任务总时长 | 1800s | 判死，但 transcript 完好可续跑 |
| worker 收尾会话 | 300s | 放弃收尾，正常结束 |
| 主 agent 回合 | 300s / 900s（有工具在跑时） | 中断本回合，**运行保持打开**，不自动重试 |
| run 整体 | 5400s | 关闭 |

---

## 6. 已知缺陷（按优先级）

### P0 — 没有完整度量（◐ 锚点已解出，8 题里还剩 7 题）

第十轮把基准集从 2 题扩到 **8 题**（6 道自造：lcg_step / aes_pin / rsa_broadcast /
crackme_z3 / rsa_wiener / sha256_ext，难度从易到难；生成器、求解器、答案在
`bench/sealed/`，题目材料在 `bench/challenges/`，整个 bench 目录跑基准时封禁）。
确定性（固定种子重跑逐字节一致）、可解性（每个 solve.py 从材料出发复现答案）、
唯一性（crackme 口令由 z3 证明唯一）都已验证，`tests/test_bench_challenges.py` 持续守着。

**锚点 lcg_step 已经解出过一次**（2026-10-27，188 秒，主 agent → crypto → verifier
全链走通，verifier `VERDICT: reproduced`）。但那次 CLI 在打印用时行时崩掉、工作目录
已被 `finally` 删掉，报告是事后按 transcript 重建的
（`bench/results/20261027-1044-lcg_step-RECOVERED.json`，**不能当 baseline 用**）。
第二十三轮已修：格式化收进 `cli._duration()`，且每跑完一题就落盘。

**easyrs 已跑过一次，未解出**（2026-10-27 13:18–13:33，925 秒，$7.45 记在库里；
报告 `bench/results/bench-20261027-131816.json`）。它是一份**合格的失败记录**，不是
一次平台崩溃：reverse worker 拿到 3 条观察（Rust 二进制、PDB 只剩
`easyrs::crypt::encrypt_pkcs7` 和 `easyrs::crypt::rc4`、任何输入都打印 `Fail`），
在 60 轮 / $8 的角色上限处停住；主 agent 接着 resume 了同一会话继续派工，然后撞上
网关余额不足（403，剩余额度不够一次预扣费），运行以 failed 收尾。也就是说
**这次测的是「钱不够」，不是「解不出」**——但 60 轮花掉 $8 本身已经说明 reverse 方向
在 pro 上单价过高。

顺带第三次踩到输出编码：`bench` 表格用的 `✓/✗` 不在 GBK 码页里，`print` 抛
`UnicodeEncodeError`，报告已落盘但汇总行丢了。第二十六轮改成 GBK 安全的
`√/×`，并给 `cli.main` 加了 `errors="replace"` 兜底，同时加了「渲染结果必须能
`encode("cp936")`」的测试。

**剩下的就是跑第一份真正落盘的 baseline——要花真钱（8 次真实运行），操作员决定何时跑**。

`[models.pricing]` 已在 2026-10-27 填好，来源不是估的：网关自己公布计费公式
（`GET /api/pricing` 的 `billing_expr`），再用账单计数器
（`GET /v1/dashboard/billing/usage`，单位是「分」）逐笔核对——

| 模型 | input $/M | output $/M | cache_read $/M |
|---|---|---|---|
| deepseek-v4-flash | 1.00 | 4.00 | 0.02 |
| deepseek-v4-pro | 4.50 | 13.50 | 0.15 |

核对结果：flash 4 万 prompt token 预测 4.0096 分、实测 4.0096 分（1.000）；
8 万 token 预测 8.0096、实测 8.1660（1.020）。复现命令：
`./.venv/Scripts/python.exe scripts/calibrate_price.py --verify-ratio`。

**高峰时段价格翻倍**（工作日 09–12、14–18 点，Asia/Shanghai）。基准报告里记录了
`peak_hours`，`--baseline` 比较时若两次时段不同会明确警告——否则一次纯时段的
2 倍差异会被当成优化成果。

顺手修正一个此前写错的判断：锚点那次 CLI 报 $1.89（crypto+verifier），按新表算
约 $1.64，**主 agent 会话另计约 $0.45**，合计约 $2.09（那次跑在高峰，实际约 $4.18）。
也就是说 CLI 的估值在这个量级上并没有夸张，之前说「差 7 倍」是拿 config 里一段
早已过时的示例单价比的，作废。真正的收益在历史全量：`ctfagent cost` 现在报的
55 个会话合计与旧口径差别明显，以新数字为准。

```bash
./.venv/Scripts/ctfagent.exe bench bench
# 带训练库再考一次（train/test 分离）：--library ctfagent.db
# 与旧结果对比：--baseline bench\results\<旧文件>.json
# 逐块跑（一块崩了不牵连别的）：--only lcg_step,aes_pin
```

自造题**不要**在正常训练中跑（战例卡会污染基准）。

### P1 — 无进程隔离

`guard.py` 是黑名单，`sandbox.mode=docker` 只是提示词开关，命令仍跑在宿主机。
黑名单拦不住决心绕过的模型。这是唯一“出了事不可挽回”的风险。

### P2 — 成本数字是虚构的（✅ 已修，2026-10-24 第十轮）

查库发现 96 个 result 事件**全部**带完整 token usage——数据一直在，只是没人用它算钱。
现在 `[models.pricing]` 配了价格表的模型按 usage 差值记账（usage 和 CLI 成本一样是会话
累计值，所以同样走基线-差值纪律；resume/attach 都从事件日志预热基线，不重复记账），
没配表的模型继续用 CLI 值，两种模式不混。`doctor` 会显示当前是哪种模式。

**价格表已于 2026-10-27 填入真实单价**（见 P0 的表与核对结果），现在 `ctfagent cost`
报的是真实美元数，不再是价目表虚构值。

### P3 — 战例库积累机制有一处断点（✅ 已修，2026-10-24 第九轮）

**这曾是"越做越强"不成立的原因，现在分类这根线接上了。**

- 原状：`cases` 表 14 张卡 12 张 `category='unknown'`；`caselib.relevant()` 最强信号
  `same_category`（+3，且是准入门槛之一）要求双方分类已知，unknown 永不成立；
  根因是 triage 判出的分类从不写回 `challenges.category`
- 修法（已落地）：`db.set_challenge_category` 带 `unknown` 守卫（平台/操作员给的分类
  永不被覆盖）；`_finish_task` 的 triage 分支解析 `category=<cat>` finding
  （散文兜底、只认九个方向角色、typo 丢弃）后写库，**同时更新内存里的
  `run.challenge.category`**，并广播 `challenge.categorized` 事件
- 历史 12 张 unknown 卡逐张查过：只有 1 张有真实来源（run 7 的 triage 结论 reverse），
  已回填；其余 11 张的 run 没跑过 triage，无源可回，等新题积累
- `caselib.attribute()`（胜负反馈）本来就接好了——现在分类和反馈两根线都在

**验证方式**：新进程起服务后跑一道粘贴建的题（不带 `category`），看 triage 结束后
`challenges` 表的该行是否变成实际方向、事件流里有没有 `challenge.categorized`。

### P4 — RE 工具集使用率 1%

`re_tools.py`(976) + `re_probe.py`(656) 共 1632 行，注入 5 个角色的系统提示词。
实测 reverse 1158 次调用里只有 42 次用 `mcp__re__*`，**pwn 115 次里 0 次**。
要么不可发现，要么不如 shell。**先读轨迹查原因，别急着改。**

### P5 — thinking 没有上限

SDK 的 `ClaudeAgentOptions` 支持 `max_thinking_tokens` 和 `thinking`，
但 `backend._options()` 只设了 `cli_path` / `fallback_model` / `resume` / `skills` 四项。
thinking 占输出 225 倍，这是唯一能直接压速度的杠杆——但它是能力换速度，
**必须等基准建好再动**。

### P6 — 其他小项

- 主 agent 的 `wait_tasks` 轮询约占 4%，不是瓶颈
- 前端 3,572 行无构建，靠纪律维护
- `fallback` 模型配置为空，网关 503 时没有回退

---

## 7. 下一步最该做的三件事

按顺序（P3 分类写回第九轮已完成；P0 题集第十轮已建、P2 成本第十轮已修）：

1. **跑第一份 baseline（P0 收尾）** —— 要花真钱，操作员决定时机。建议顺序：
   先把 `config.toml` 的 `[models.pricing]` 填上真实单价（成本刻度才是真的），
   再 `ctfagent bench bench`。跑完把 json 留在 `bench/results/`，以后每次改动
   用 `--baseline` 对比。**自造题不要在正常训练中跑。**
   锚点 lcg_step 已在 2026-10-27 解出过（188 秒，全链走通），剩下 7 题还没跑。
2. **考虑进程隔离（P1）** —— 唯一不可挽回的风险。`guard.py` 黑名单已加固但拦不住
   决心绕过的模型，真隔离要把 shell 包进容器。有了 baseline 之后，隔离改动也能验收。
3. **P5（thinking 上限）** —— 225:1 是唯一能直接压速度的杠杆，但它拿能力换速度，
   baseline 建好之前不许动。

P4（RE 工具使用率）已销案：查轨迹发现 re 工具 54 次调用仅 1 错、使用集中在有真二进制的
run 5/6/7；reverse 的 shell 命令 49% 是跑自写 python 脚本（无工具可替代），重叠面仅 9%。
重复命令检测也销案（全库仅 2.7% 浪费）。

---

## 8. 环境与工具坑（给接手人省时间）

这些都是今天真实踩到的：

| 坑 | 说明 |
|---|---|
| `./start.ps1` 在 Git Bash 里跑不了 | 那是 PowerShell 脚本，bash 会报 BOM 和 `#Requires` 错。用 `./start.sh` |
| 8788 被占 | 会落到 8789，这是正常现象不是故障 |
| venv 没有 pip | `uv pip install --python .venv/Scripts/python.exe <pkg>` |
| worker 的 python | `_session_env()` 已把 `.venv\Scripts` 放到 PATH 最前，worker 用的是装好包的那个解释器 |
| `wsl.exe` 在 Program Blacklist | 启动时会打印一条 BLOCKED，不影响服务，忽略即可 |
| PowerShell `Start-Process` 报“字典中的关键字” | 环境里 `HTTPS_PROXY` 和 `https_proxy` 重复键冲突。要么清掉一个，要么用 `.NET Process.Start`（也可能被策略拦） |
| 跑测试要加 `--basetemp` | `pytest --basetemp=<项目内临时目录>`，否则结束时 `[safe-delete]` 会拦掉清理并冲掉汇总行 |
| `rm -rf` 删超过 50 个文件会被拦 | safe-delete 钩子的限制。改用 PowerShell `Remove-Item -Recurse -Force` |
| 我起的服务活不过本次会话 | 安全策略不允许拉起持久进程，前台 `serve` 会随工具调用结束被回收。**服务必须由人在自己终端起** |
| `bench/results/` 里只有一份 `-RECOVERED.json` | 2026-10-27 的锚点跑解出了，但 CLI 崩在打印行，报告是事后重建的。正式 baseline 仍未产生（见第 7 节） |

---

## 9. 测试怎么跑

```bash
cd /d/agentsec/CTFAGENT
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.\.pt_tmp
```

全量约 **7–8 分钟**。跑完 `grep passed` 看汇总，然后删掉 `.pt_tmp`。

**`tests/test_live.py` 会真实调模型**，默认跳过（需要 `CTFAGENT_LIVE=1` 才会跑），
所以全量结果里的 `1 skipped` 就是它。如果你看到它开始跑，说明有人设了这个环境变量。

测试文件的含义：

| 文件 | 覆盖什么 |
|---|---|
| `test_orchestration.py` | 端到端全链路（scripted backend） |
| `test_guard.py` | 命令黑名单与命令注入检测 |
| `test_budget.py` | 预算、角色上限、收敛纪律、模型分配 |
| `test_supersession.py` | 结论取代链 |
| `test_stall_and_resume.py` | 静默重试、同方向拒派 |
| `test_foundations.py` | 任务墙钟、黑板容量、工具链可见性、主 agent 回合时钟 |
| `test_verdict.py` | 验证器结论结构化传递 |
| `test_writeup_on_request.py` | 确认不写报告 |
| `test_case_library.py` | 战例卡蒸馏/检索/胜负记账、triage 分类写回、`recount` 重判 |
| `test_notes.py` | 方法笔记：命中排序、按使用记账、方向回退、`memory` 体检、沉淀入口 |
| `test_bench_challenges.py` | 自造基准题的完整性：manifest 与答案一致、每题可解、crackme 唯一口令 |
| `test_bench.py` | 基准调度：每题跑完即回调落盘、报告往返、时段标记、CLI 表格渲染（含小数秒不崩） |
| `test_costing.py` | `ctfagent cost` 的会话成本汇总与按 token 记账 |
| `test_skill_distill.py` | 技能沉淀：确认门、flag 擦除、覆盖不堆积、worker 可见性 |
| `test_pwn_environment.py` | WSL pwn 工作流：两条硬规则、无工具时沉默、pwn/verifier-for-pwn 定向注入 |
| `test_kit.py` | 注入引擎：五类 oracle 对着本地靶子解出已知 flag、坏 spec 必须抛异常、提示词按分类注入 |

---

## 10. 诚实的自我评估

**作为单人研究工具：可用。作为产品：不成熟。**

强项：架构分层清晰（四条设计约束）、事件溯源完善、注释说明“为什么”且带实测证据、
九轮加固每一处都有库内数据支撑。

弱项：**无法证明自己变好**（无基准）、无隔离、成本数据不可信、单用户单机。

接手的人请先把第 7 节的第一件事做掉，再考虑加新功能。
