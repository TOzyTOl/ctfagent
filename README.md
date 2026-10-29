# CTFAGENT

多 agent 协同的 CTF 解题系统：**一个主 agent** 负责对话、拆题、调度，**多个方向 worker**（web / crypto / pwn / reverse / misc / forensics / malware / ai-ml / osint）并行执行，结果写回共享黑板，主 agent 汇总，flag 由独立 verifier 复现、**人工确认**。

支持多题并行、多线程（asyncio 会话池）、多方向。

## 它怎么工作

```
主 agent（Claude Code 会话）
  ├─ dispatch_task(role, objective, context) ──► worker 会话（各自 cwd、各自技能）
  │        worker 用真实 shell/文件/脚本执行，通过 record_fact / submit_flag 写回
  ├─ wait_tasks / list_tasks / get_task_result / read_blackboard
  ├─ propose_flag ──► 自动派 verifier 独立复现
  └─ finish_run
        ↓
     人工确认 flag（Web UI 或 CLI）→ 派 writer 生成 writeup → 运行结束
```

设计要点：

- **主 agent 不执行漏洞利用，worker 不能宣布胜利**。工具集不对称，靠工具边界而非提示词约束。
- **结构化写回**：worker 的产出是 `record_fact` / `submit_flag` / `task_complete` 的结构化结果，主 agent 不解析散文。
- **独立验证**：flag 候选自动派 verifier，verifier 必须自己重新推导，不信任提交者的脚本。
- **双层并发上限**：每 run 的 worker 数（`workers_per_run`）+ 全局会话数（`max_sessions`，每个会话是一个 `claude` 子进程，约 150–250MB）。
- **SSE 事件流**：SQLite `events` 表是真相源，`Last-Event-ID` 断线重连可补齐。

## 安装

要求：Python ≥ 3.12，Windows / Linux / macOS。

### 一键启动（推荐）

Windows 双击 `start.cmd`，或任意终端：

```powershell
.\start.ps1              # 自动建环境、装依赖、起界面、开浏览器
.\start.ps1 -Demo        # 演示模式：不调用模型、不花钱，先看界面
.\start.ps1 -Check       # 只做环境自检
.\start.ps1 -Port 9000   # 指定端口（默认 8788，被占用会自动顺延）
```

Linux / macOS / Git Bash：

```bash
./start.sh               # 同上
./start.sh --demo
./start.sh --check
```

脚本是幂等的：`.venv` 已存在且依赖齐全时直接启动，不会重复安装。

**已经在跑时再启动一次**不会开第二个服务：脚本检测到运行中的实例，直接打开它的页面然后退出（第二个服务会把第一个的进行中任务标记成中断，所以这里主动拦住）。`ctfagent serve` 自己也会拒绝并告知已有实例的地址。

### 手动安装

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
```

### 模型接入

默认走环境变量，与 Claude Code 一致：

```powershell
$env:ANTHROPIC_BASE_URL  = "http://127.0.0.1:15721"   # 你的 Anthropic 兼容代理
$env:ANTHROPIC_AUTH_TOKEN = "..."                     # 或 ANTHROPIC_API_KEY
```

也可以写 `config.toml`（`ctfagent init` 生成模板）：

```toml
[models]
main = "claude-opus-4-8"
worker = "claude-sonnet-5-5"
verifier = "claude-sonnet-5-5"
writer = "claude-sonnet-5-5"
# base_url = "http://127.0.0.1:15721"
# auth_token = "..."
api_format = "anthropic"
```

> 使用 OpenAI 兼容上游时，需要一个把它翻译成 Anthropic 协议的代理（本项目自带的会话层说的是 Anthropic wire format）。

### 技能库

11 个 ctf-* 技能（MIT，来自 [ljagiello/ctf-skills](https://github.com/ljagiello/ctf-skills)）放在 `plugins/ctf-skills/skills/`。每个 run 的工作目录下会用 **Windows junction / Linux symlink** 暴露为 `.claude/skills/`，失败则复制。会话以 `setting_sources=["project"]` 启动，因此只加载 run 自己的 `.claude`，不会继承你机器上的全局设置。

### 可选：Docker 沙箱

`config.toml` 中 `[sandbox] mode = "docker"` 目前**只是提示词层面**的开关：`ctfagent` 没有任何代码把 shell 工具包进容器，所以命令始终在宿主机上执行。`ctfagent doctor` 会报告 Docker 是否可用，但可用与否不改变执行位置。要在容器里跑 pwn/ELF，请用宿主机上的 WSL（`wsl -e bash -lc '<cmd>'`），提示词里已经告诉 worker WSL 的存在和用法。

## 使用

```powershell
# 环境自检
.\.venv\Scripts\ctfagent.exe doctor

# 命令行直接跑一道题（不开 Web）
.\.venv\Scripts\ctfagent.exe run D:\agentsec\CTFAGENT\challenges\n17

# 查看运行、确认 flag
.\.venv\Scripts\ctfagent.exe list
.\.venv\Scripts\ctfagent.exe show 1
.\.venv\Scripts\ctfagent.exe confirm 1
```

> 启动界面用 `.\start.ps1`（见上）。`ctfagent serve` 也可以直接起，但不会帮你准备环境。

### Web 界面

`serve` 之后打开 http://127.0.0.1:8788，默认落在**对话式首页**：一个输入框，贴题目、给目标地址，或者先随便聊两句都行。附件点「＋ 附件」选，也可以直接拖进页面任意位置（整页会浮出一层「松手即可附上文件」）；Enter 发送，Shift+Enter 换行。第一条消息同时就是主 agent 看到的题目描述——不需要 `challenge.yaml`，也不用先把文件放进某个目录。目录形式的题目、标题/分类/flag 格式这些补充项收在首页下方的折叠区里。

发送后进入工作台，三栏：

- 左：**多任务窗口**。每个 run 一行（名字、状态章、花费、最后一条动态），点一下就切换；运行中的排在上面，已捕获 flag 的整行反转成纸色。任何页面都能看到这块，跑着的时候也能随时开新任务。
- 中：**对话**。你发的每条消息都在，附件显示成可点开的文件卡片（点一下直接下载）；主 agent 的回复按 token 实时流出，整段到齐后落成一条正式消息，刷新后看到的内容和实时看到的一致。跑的过程中可以继续补线索，也可以再上传附件（文件落进 `attachments/`，同时告诉主 agent）。旁边「报告」页签在确认 flag 后显示 writer 写的报告。
- 右：**工作产物**——工单（每个 worker 的目标与结论）、flag 候选（确认/驳回）、共享黑板、实时日志（可隐藏工具返回）。

目录形式的题目仍然支持：首页下方「已有题目目录？直接登记」，或 `ctfagent run <目录>`。

### 比赛接入（CTFd）

顶栏「比赛」页可以接一个 CTFd 账号：填平台地址和 Access Token（CTFd → Settings → Access Token），点「连接并验证」——会先调 `/api/v1/users/me` 确认 token 有效，失败不保存。

- **同步**：把平台题目拉成本地题目，未解的题连同附件一起下载；重复同步按 `(比赛, 题目 id)` 原地更新，不会重复建行，也不会覆盖你手动加的附件。
- **开跑**：同步完可以直接「全部开跑」，或逐题点「开跑」。
- **提交**：flag 仍然由你在工作台确认；确认之后 flag 卡片上才会出现「提交到 <比赛名>」，点它才发到平台。平台返回 `correct` / `already_solved` / `incorrect` 都会记成一条回执（`submissions` 表）并显示在卡片上——错误答案只记录，不抛错。

**系统不会自动提交任何 flag**，连接 token 只存在本机数据库，不会出现在页面或日志里。

设计说明见 `docs/界面设计.md`。界面无构建步骤：服务端渲染 + 两个原生 JS 文件（`app.js`、`compose.js`）+ SSE。

> 对话只对当前进程管理的运行可用。服务重启后，运行会标记为「中断」，仍可确认 flag、查看报告，但不能继续对话——页面会直接说明这一点。

### 题目格式

目录里放 `challenge.yaml`：

```yaml
name: silentrotor
category: crypto          # 可选；不写则由 triage 判断
description: |
  多阶段转子密码。
remote: "nc 1.2.3.4 31337"   # 可选
flag_format: "flag\\{...\\}"  # 可选，收窄候选提取
hints:
  - "先看 manual.txt"
time_limit_s: 3600
```

附件（除 `challenge.yaml` 外的全部文件）会被复制到 run 工作目录的 `attachments/`。

## 目录

```
ctfagent/
  backend.py       Claude Agent SDK 会话层（主 agent 长会话 / worker 一次性会话）
  orchestrator.py  Run / RunManager / 会话池 / 任务生命周期 / 预算看门狗
  tools.py         主 agent 的编排工具（进程内 MCP）
  worker_tools.py  worker 的写回工具（进程内 MCP）
  re_probe.py      逆向探针：ELF/PE/Mach-O 解析、字符串/字节扫描、外部工具探测
  re_tools.py      逆向工具服务（进程内 MCP）：证据记录 + 分页 + 覆盖声明
  prompts.py       各角色系统提示词
  guard.py         PreToolUse 钩子：危险命令与越界写入拦截
  db.py            SQLite（challenges/runs/tasks/facts/flags/events/messages/competitions/submissions）
  ctfd.py          CTFd 客户端（拉题、下载附件、提交 flag），无第三方依赖
  events.py        SQLite seq + 进程内 pub/sub
  workspace.py     工作目录、技能安装、沙箱探测
  server.py        FastAPI + Jinja2 + SSE
  cli.py           CLI
  templates/       base / index（调度板）/ run / competitions / docs
  static/          app.css · app.js
```

## 逆向工具服务（`re` MCP）

`triage / reverse / pwn / malware / forensics` 五个角色额外获得一组结构化逆向工具。
与让 worker 自己解析 `objdump` 文本不同，每次调用返回一条**证据记录**：

```json
{
  "evidence_id": "re-inspect-501592e904",
  "provider": {"name": "builtin+file"},
  "normalized_result": { "kind": "elf", "mitigations": {"nx": true, "pie": "PIE"} },
  "coverage": {"status": "complete", "scope": "elf 结构与保护机制", "paging": {}},
  "limitations": ["只覆盖 ..."],
  "residual_unknowns": ["..."]
}
```

- `coverage.status` 是 `complete / partial / failed`；`partial` 和 `failed` **不能**当作「不存在」的证据。
- 所有列表类结果分页，`coverage.paging.next_offset` 是续读位置；单条结果有字符上限，超出会被标记为截断而不是静默丢失。
- `re_evidence_get` 按 `evidence_id` 取回已产出的证据，避免重复扫描。
- 内置解析器（ELF/PE/Mach-O 头、段表、符号、保护机制、字符串、字节搜索）不依赖外部工具；
  `readelf / objdump / nm / file / gdb / docker / r2 / jadx ...` 作为增强项由 `re_capabilities` 报告，
  缺什么会明说，不会静默降级。
- `re_inspect` 会指出「结构表之外还有东西」：大块非可执行数据段（PyInstaller 的 `pydata`）、
  文件尾部附加数据、以及 UPX/PyInstaller 的打包痕迹。这类目标的反汇编只是壳。
- 工具只读工作区内的文件，不执行目标程序；动态分析仍走 Bash，`guard.py` 照常生效。

## 安全边界（请读）

- 会话以 `bypassPermissions` 运行（CTF 场景不能中途停下来等人点确认）。**唯一的硬边界是 `guard.py` 的 PreToolUse 钩子**：危险命令黑名单 + 写入必须落在 run 工作目录内。
- 这是**防事故的黑名单，不是沙箱**。目前也没有真正的隔离层：`sandbox.mode = "docker"` 只是提示词开关，命令始终跑在宿主机上。跑不可信附件前请自行放进虚拟机 / WSL / 一次性机器。
- 题目附件是不可信数据；仅在隔离或授权环境中使用本系统。
- flag 必须由人工确认，系统不会自动提交到任何平台。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试全部离线（`ScriptedBackend`），不需要 API key。标记 `live` 的测试需要真实代理。
