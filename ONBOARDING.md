# ONBOARDING — 第二个人怎么把 CTFAGENT 跑起来

> 这是给新接手者的安装与须知文档。项目背景与架构见 `README.md` 与 `docs/`；
> 操作员日常用法见 `docs/使用说明.md`；维护者视角（历轮加固的证据与决策）见
> `docs/HANDOVER.md` 与 `docs/优化审查-20261023.md`。

## 0. 这是什么

多 agent 协同的 CTF 解题平台：主 agent 拆题派工，方向 worker（web / crypto /
reverse / pwn …）并行解题，独立 verifier 复现 flag，人工确认。战例库与技能
沉淀让它对做过的题型越做越强。

## 1. 前置条件（Windows 路线，项目的主场）

| 需要 | 说明 |
|---|---|
| Python 3.12 + [uv](https://docs.astral.sh/uv/) | venv 用 uv 建（项目无 pip 依赖安装习惯） |
| Node.js | 只为装原生 Claude CLI |
| Git | 拉代码 |
| Windows 10/11 | 深度绑定：PowerShell 工具命名、WSL pwn 路线、路径守卫 |

> **Linux / macOS 用户**：项目当前以 Windows 宿主为一等公民（shell 工具按
> 宿主命名、pwn 依赖 WSL、路径归一化按 Windows 写）。在其它系统上需要先做
> 移植评估，`ctfagent doctor` 会暴露大部分断点，但不要假设开箱即用。

## 2. 安装（约 15 分钟）

```powershell
# ① 原生 Claude CLI（SDK 拒绝 .cmd/.ps1 垫片，必须是 claude.exe）
npm install -g @anthropic-ai/claude-code
where.exe claude   # 确认解析到 ...\claude-code\bin\claude.exe

# ② 代码与环境
git clone https://github.com/TOzyTOl/ctfagent.git
cd ctfagent
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"

# ③ 生成配置
.\.venv\Scripts\ctfagent.exe init

# ④ CTF 常用包（doctor 会列出缺哪些）
uv pip install --python .venv\Scripts\python.exe pwntools z3-solver gmpy2 pycryptodome capstone angr unicorn pyelftools ropgadget
```

## 3. 凭据（用你自己的，别共用）

模型走 Anthropic 兼容网关。**注册你自己的账号和 token**——共用一个账号意味着
额度互相烧、一方超额双方都停。

最省事的办法是起服务后打开 **凭据页**（`http://127.0.0.1:8788/settings`）：
填一次端点 + 令牌，它会先调 `/v1/models` 验证、把网关实际提供的模型列出来，
再在「模型分配」里按角色下拉选。试过的端点都会留成供应商卡片，以后换模型/换网关
点一下「切换」即可，下一次派活就生效，不用重启。装过 CC Switch 的话按
「从 CC Switch 导入」可以一次把已有的 Claude 供应商搬过来。

也可以继续用环境变量（临时覆盖文件里的值，启动前设置）：

```powershell
$env:ANTHROPIC_BASE_URL   = "https://fastmodel.top"      # 或你自己的网关
$env:ANTHROPIC_AUTH_TOKEN = "<你的 token>"
```

优先级从高到低：`CTFAGENT_*` 环境变量 → `config.toml`（凭据页写的就是它）→
`ANTHROPIC_*` 环境变量 → `%USERPROFILE%\.claude\settings.json` 的 `env` 段
（服务会把它转发给子会话）。

> `config.toml` 排在 `ANTHROPIC_*` 之前是有意的：否则终端里敲的旧变量会在每次
> 启动时悄悄覆盖掉凭据页上刚切换的网关。要让环境变量压过文件，用 `CTFAGENT_*`。

## 4. 自检与启动

```powershell
.\.venv\Scripts\ctfagent.exe doctor   # 期望：技能库 11 个、claude cli 原生、包齐全
.\start.sh                            # Git Bash；或 .\start.ps1（PowerShell）
# 打开 http://127.0.0.1:8788（被占用会顺延），首页贴题面即可开跑
```

`doctor` 报什么缺什么，照着补；全绿再开工。

## 5. （可选）战例库导入 —— 继承"做过的题"

平台的记忆在 `cases` 表（战例卡）。仓库不含运行数据库，但可以把老兵的卡导入
你的新库，让平台一开局就带着经验：

```powershell
# 老兵导出（在老兵的机器上）
.\.venv\Scripts\python.exe scripts\export_cases.py --db ctfagent.db --out cases.json
# 新人导入
.\.venv\Scripts\python.exe scripts\export_cases.py --import cases.json --db ctfagent.db
```

卡在设计上不携带 flag 字面量（写入时已擦除，导出时再擦一遍）。

## 6. （可选）pwn 方向的 WSL 工具链

宿主是 Windows，ELF 靶标靠 WSL2 里的真 Linux 环境跑。最小集（用户空间，无需
sudo）：pwntools / ROPgadget / patchelf / GEF。完整清单与安装命令见
`docs/优化审查-20261023.md` 第十三轮。

## 7. 安全须知（必读，不是走形式）

1. 会话以 `bypassPermissions` 运行，唯一硬边界是黑名单守卫——**它防事故，不防
   决心绕过的模型，更不是沙箱**。不要拿它处理来路不明的附件；
2. flag 人工确认、提交到平台是独立的第二次点击，系统不自动提交；
3. **删除运行不可逆**（记录、黑板、工作区一起删），界面里是两步确认，看清再点；
4. 跑基准（`ctfagent bench`）时基准目录与训练库自动封禁，但**不要把基准题混进
   正常训练**——那会让分数测的是记忆而不是能力；
5. 预算上限在 `config.toml` 的 `[limits]`，网关模型建议先配 `[models.pricing]`
   再开跑，成本数字才对应真钱。

## 8. 更新

```powershell
git pull
uv pip install --python .venv\Scripts\python.exe -e ".[dev]"
# 改了依赖才需要第二句；改完跑一次 doctor
```

## 9. 遇到问题

先查 `docs/使用说明.md` 的常见问题节（技能不生效、ELF 打不开、worker 卡死、
同方向重复派单这些都有现成答案），再查 `docs/HANDOVER.md` 的环境坑清单。
