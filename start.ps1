#Requires -Version 5.1
<#
.SYNOPSIS
    One-click launcher for CTFAGENT.

.DESCRIPTION
    Prepares the environment (virtualenv, dependencies, preflight checks) and
    starts the web UI. Safe to re-run: every step is skipped when it is already
    satisfied.

    First run on a clean checkout takes a few minutes (it creates .venv and
    installs dependencies). Later runs start in a second or two.

.PARAMETER Port
    Port for the web UI. Default: 8788, or the next free port if that is taken.

.PARAMETER BindHost
    Interface to bind. Default: 127.0.0.1 (this machine only).
    Use 0.0.0.0 to expose the UI on the LAN — see the warning it prints.

.PARAMETER Demo
    Start with the offline scripted backend: no model calls, no cost. Useful for
    checking the interface before configuring credentials.

.PARAMETER NoBrowser
    Do not open a browser window.

.PARAMETER Reinstall
    Force a reinstall of the Python package and its dependencies.

.PARAMETER Check
    Run the environment self-check (ctfagent doctor) and exit.

.EXAMPLE
    .\start.ps1
    .\start.ps1 -Demo
    .\start.ps1 -Port 9000 -NoBrowser
#>
[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$BindHost = "",
    [switch]$Demo,
    [switch]$NoBrowser,
    [switch]$Reinstall,
    [switch]$Check,
    # Stop the instance that is already listening and start a fresh one.
    [switch]$Restart,
    # Internal: poll until the server answers, then open the browser.
    [switch]$OpenWhenReady
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $Root

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

# ---------------------------------------------------------------- presentation

function Step([string]$text) { Write-Host "  $text" -ForegroundColor DarkGray }
function Ok([string]$text) { Write-Host "  ok  " -ForegroundColor DarkGreen -NoNewline; Write-Host $text }
function Warn([string]$text) { Write-Host "  !   " -ForegroundColor Yellow -NoNewline; Write-Host $text }
function Fail([string]$text) { Write-Host "  x   " -ForegroundColor Red -NoNewline; Write-Host $text }
function Head([string]$text) { Write-Host ""; Write-Host $text -ForegroundColor White }

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    # Windows PowerShell 5.1 turns a native command's stderr into a terminating
    # error when $ErrorActionPreference is Stop, which would abort on a warning
    # that is not a failure. Judge by exit code instead.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$What (exit $code)" }
}

function Invoke-Python {
    <# Run the venv interpreter and return stdout as a trimmed string.
       Returns $null on failure; callers decide whether that is fatal. #>
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $VenvPython @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { return $null }
    return ("$output").Trim()
}

function Test-PortFree {
    param([int]$Candidate)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Candidate)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { try { $listener.Stop() } catch { } }
    }
}

# ---------------------------------------------------------------- browser opener

if ($OpenWhenReady) {
    $url = "http://127.0.0.1:$Port/"
    for ($i = 0; $i -lt 120; $i++) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 1 | Out-Null
            Start-Process $url
            return
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return
}

# ---------------------------------------------------------------- banner

Write-Host ""
Write-Host "  CTFAGENT" -ForegroundColor White -NoNewline
Write-Host "  · 多 agent CTF 解题" -ForegroundColor DarkGray
Write-Host ""

# ---------------------------------------------------------------- python

Head "环境"

$uv = Get-Command uv -ErrorAction SilentlyContinue
$needInstall = $Reinstall.IsPresent
$createdVenv = $false

if (-not (Test-Path $VenvPython)) {
    Step "创建虚拟环境 .venv（首次运行，需要几十秒）"
    if ($uv) {
        Invoke-Native $uv.Source @("venv", "--python", "3.12", ".venv") "创建虚拟环境失败"
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        Invoke-Native "py" @("-3.12", "-m", "venv", ".venv") "创建虚拟环境失败（需要 Python 3.12+）"
    } else {
        Invoke-Native "python" @("-m", "venv", ".venv") "创建虚拟环境失败（需要 Python 3.12+）"
    }
    $createdVenv = $true
    $needInstall = $true
}

$pyVersion = Invoke-Python @("-c", "import sys; print('{}.{}.{}'.format(*sys.version_info[:3]))")
if (-not $pyVersion) {
    Fail "虚拟环境不可用：$VenvPython"
    Write-Host "  删掉 .venv 后重跑本脚本即可重建。" -ForegroundColor DarkGray
    exit 1
}
Ok "Python $pyVersion  (.venv)"

# Dependencies: an import check is faster and more truthful than pip metadata.
$depsProbe = Invoke-Python @("-c", "import fastapi, uvicorn, jinja2, claude_agent_sdk")
$depsOk = ($null -ne $depsProbe)

if (-not $depsOk -or $needInstall) {
    Step "安装依赖（首次运行需要一两分钟）"
    if ($uv) {
        Invoke-Native $uv.Source @("pip", "install", "--python", $VenvPython, "-e", ".[dev]") "安装依赖失败"
    } else {
        Invoke-Native $VenvPython @("-m", "pip", "install", "--quiet", "--upgrade", "pip") "升级 pip 失败"
        Invoke-Native $VenvPython @("-m", "pip", "install", "-e", ".[dev]") "安装依赖失败"
    }
    Ok "依赖已安装"
} else {
    Ok "依赖已就绪"
}

# ---------------------------------------------------------------- preflight

$cliPath = Invoke-Python @("-c", "from ctfagent.backend import resolve_cli_path; print(resolve_cli_path())")
if ($null -eq $cliPath) {
    Fail "无法导入 ctfagent"
    Write-Host ""
    Write-Host "  试试重新安装：.\start.ps1 -Reinstall" -ForegroundColor DarkGray
    exit 1
}

if ($cliPath) {
    Ok "claude CLI  $cliPath"
} else {
    Warn "未找到原生 claude CLI；SDK 会回退到自带 CLI（可能版本较旧）"
}

$skillsDir = Join-Path $Root "plugins\ctf-skills\skills"
if (Test-Path $skillsDir) {
    $skillCount = (Get-ChildItem $skillsDir -Directory | Where-Object { Test-Path (Join-Path $_.FullName "SKILL.md") }).Count
    Ok "技能库      $skillCount 个"
} else {
    Warn "技能库缺失：$skillsDir"
}

$sandbox = Invoke-Python @(
    "-c",
    "from ctfagent.config import load_config; from ctfagent.workspace import probe_sandbox; s = probe_sandbox(load_config()); print(s.mode, s.available, s.detail)"
)
if ($sandbox) {
    $parts = $sandbox -split " ", 3
    if ($parts[0] -eq "docker" -and $parts[1] -eq "True") {
        Ok "沙箱        docker（可用）"
    } elseif ($parts[0] -eq "docker") {
        Warn "沙箱        docker 不可用：$($parts[2])；将降级为宿主机执行"
    } else {
        Ok "沙箱        宿主机（未启用 docker）"
    }
}

# ---------------------------------------------------------------- credentials

$hasCreds = $false
foreach ($name in @("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CTFAGENT_AUTH_TOKEN")) {
    if ([Environment]::GetEnvironmentVariable($name)) { $hasCreds = $true }
}
$hasConfig = Test-Path (Join-Path $Root "config.toml")
# A token in ~/.claude/settings.json is picked up by config.py and forwarded to
# the child sessions (they run with setting_sources=["project"] and do not read
# that file themselves). Ask Python rather than re-implementing the fallback.
$settingsCreds = $false
$sourceLine = Invoke-Python @("-c", "from ctfagent.config import load_config; c = load_config(); print(c.models.credential_source or 'none')")
if ($sourceLine -eq "settings") { $settingsCreds = $true }
$hasCreds = $hasCreds -or $settingsCreds

if ($Demo) {
    Ok "模型        演示模式（--scripted，不调用模型）"
} elseif ($hasCreds -or $hasConfig) {
    $endpoint = [Environment]::GetEnvironmentVariable("ANTHROPIC_BASE_URL")
    if (-not $endpoint) { $endpoint = "config.toml / CLI 默认" }
    Ok "模型        $endpoint"
    if ($settingsCreds) { Step "凭据来自 ~/.claude/settings.json（config.py 会转发给子会话）" }
} else {
    Warn "没有检测到模型凭据"
    Write-Host ""
    Write-Host "     界面可以正常打开，但开跑会失败：子会话不读 ~/.claude/settings.json。" -ForegroundColor DarkGray
    Write-Host "     二选一：" -ForegroundColor DarkGray
    Write-Host "       1) 先设置环境变量再启动：" -ForegroundColor DarkGray
    Write-Host '          $env:ANTHROPIC_BASE_URL  = "http://127.0.0.1:15721"' -ForegroundColor Gray
    Write-Host '          $env:ANTHROPIC_AUTH_TOKEN = "..."' -ForegroundColor Gray
    Write-Host "       2) 把 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 写进 ~/.claude/settings.json 的 env 段" -ForegroundColor DarkGray
    Write-Host "       3) 先看界面：.\start.ps1 -Demo" -ForegroundColor DarkGray
    Write-Host ""
}

if ($Check) {
    Write-Host ""
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $VenvPython -m ctfagent doctor } finally { $ErrorActionPreference = $previous }
    exit $LASTEXITCODE
}

# ---------------------------------------------------------------- restart helpers

# The newest .py mtime, but only when it is newer than the process started —
# that is the same rule the UI banner uses, so the two never disagree about
# whether a restart is needed.
function Get-CodeStaleness([int]$TargetPid) {
    try { $started = (Get-Process -Id $TargetPid -ErrorAction Stop).StartTime }
    catch { return $null }
    $newest = $null
    $sourceDir = Join-Path $Root "ctfagent"
    if (Test-Path $sourceDir) {
        Get-ChildItem -Path $sourceDir -Recurse -Filter *.py -ErrorAction SilentlyContinue |
            ForEach-Object { if (-not $newest -or $_.LastWriteTime -gt $newest) { $newest = $_.LastWriteTime } }
    }
    if ($newest -and $newest -gt $started.AddSeconds(1)) {
        return [pscustomobject]@{ Newest = $newest; Started = $started }
    }
    return $null
}

# Kill it, then wait for the port to actually stop answering. Returning before
# that would let the new process collide with the old one's socket.
function Stop-RunningInstance([int]$TargetPid, [int]$TargetPort) {
    try { Stop-Process -Id $TargetPid -Force -ErrorAction Stop }
    catch { Warn "无法结束 pid $TargetPid：$($_.Exception.Message)"; return $false }
    for ($i = 0; $i -lt 60; $i++) {
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$TargetPort/api/health" -UseBasicParsing -TimeoutSec 1 | Out-Null
            Start-Sleep -Milliseconds 250
        } catch { return $true }
    }
    return $false
}

# ---------------------------------------------------------------- already running

# Starting a second server against the same database is worse than refusing:
# its startup sweep marks the first server's live runs as interrupted, and each
# process only knows the runs it started. So reuse the running one instead.
$running = Invoke-Python @(
    "-c",
    "from ctfagent.config import load_config; from ctfagent import instance; r = instance.running_instance(load_config()); print(r['pid'], r['port']) if r else None"
)
if ($running) {
    $parts = $running -split " ", 2
    $runPid = $parts[0]
    $runPort = $parts[1]
    # A pid can be reused by an unrelated process; confirm the port answers.
    $alive = $false
    try {
        $health = Invoke-WebRequest -Uri "http://127.0.0.1:$runPort/api/health" -UseBasicParsing -TimeoutSec 3
        $alive = ($health.StatusCode -eq 200)
    } catch { $alive = $false }

    if ($alive) {
        $active = 0
        try { $active = [int](($health.Content | ConvertFrom-Json).sessions_active) } catch { }
        $stale = Get-CodeStaleness ([int]$runPid)

        $doRestart = $Restart.IsPresent
        if (-not $doRestart -and $stale) {
            # This is the trap the banner exists for: the code on disk is newer
            # than the process, so every page is serving old behaviour and any
            # page added since answers 404. Say it here too, because this is
            # where the operator goes when that happens.
            Head "已在运行，但代码比进程新"
            Warn "最后一次改动 $($stale.Newest.ToString('MM-dd HH:mm'))，进程启动于 $($stale.Started.ToString('MM-dd HH:mm'))"
            Write-Host "  继续用这个实例，新加的页面会 404。" -ForegroundColor DarkGray
            if ([Environment]::UserInteractive -and -not $NoBrowser) {
                $answer = ""
                try { $answer = Read-Host "  重启？会中断正在跑的会话（当前 $active 个）[y/N]" } catch { $answer = "" }
                $doRestart = ($answer -match '^(y|yes)$')
            } else {
                Write-Host "  加 -Restart 可自动重启。" -ForegroundColor DarkGray
            }
        }

        if ($doRestart) {
            Head "重启"
            if ($active -gt 0) {
                Warn "有 $active 个会话在运行，重启会把它们标记为「服务重启，会话已中断」"
            }
            if (-not (Stop-RunningInstance ([int]$runPid) ([int]$runPort))) {
                Fail "旧实例没有停下来，放弃重启"
                exit 1
            }
            Ok "已停止 pid $runPid，端口 $runPort 已释放"
            # Falls through to a normal start below.
        } else {
            Head "已在运行"
            Ok "CTFAGENT pid $runPid   http://127.0.0.1:$runPort/"
            if (-not $NoBrowser) { try { Start-Process "http://127.0.0.1:$runPort/" } catch { } }
            Write-Host ""
            Write-Host "  要重启：start.cmd -Restart   （或结束 pid $runPid 后重跑）" -ForegroundColor DarkGray
            exit 0
        }
    }
    Warn "发现残留的实例记录（pid $runPid 已不在监听），继续启动"
}

# ---------------------------------------------------------------- port

Head "启动"

if ($Port -le 0) {
    $Port = 8788
    $found = $false
    for ($candidate = $Port; $candidate -lt $Port + 20; $candidate++) {
        if (Test-PortFree $candidate) { $Port = $candidate; $found = $true; break }
    }
    if (-not $found) {
        Fail "8788 起的 20 个端口都被占用"
        exit 1
    }
    if ($Port -ne 8788) { Warn "8788 被占用，改用 $Port" }
} elseif (-not (Test-PortFree $Port)) {
    Fail "端口 $Port 已被占用。换一个：.\start.ps1 -Port 9000"
    exit 1
}

$bind = $BindHost
if (-not $bind) { $bind = "127.0.0.1" }
$url = "http://$bind`:$Port/"
$localUrl = "http://127.0.0.1:$Port/"

if ($bind -ne "127.0.0.1" -and $bind -ne "localhost") {
    Write-Host ""
    Warn "绑定到 $bind：同一网络内的任何人都能打开这个界面并确认 flag。"
    Warn "仅在你信任该网络时使用。"
}

$serveArgs = @("-m", "ctfagent", "serve", "--port", "$Port")
if ($BindHost) { $serveArgs += @("--host", $BindHost) }
if ($Demo) { $serveArgs += "--scripted" }

# Report the paths the server will actually use, not assumed defaults.
$dbPath = Invoke-Python @("-c", "from ctfagent.config import load_config; print(load_config().db_path)")
if (-not $dbPath) { $dbPath = Join-Path $Root "ctfagent.db" }

if (-not $NoBrowser) {
    try {
        Start-Process -FilePath "powershell" -WindowStyle Hidden -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", $PSCommandPath, "-OpenWhenReady", "-Port", "$Port"
        ) | Out-Null
    } catch {
        # A missing browser is not a reason to refuse to start.
    }
}

Write-Host ""
Write-Host "  界面  " -ForegroundColor DarkGray -NoNewline
Write-Host $localUrl -ForegroundColor Cyan
if ($Demo) {
    Write-Host "  模式  " -ForegroundColor DarkGray -NoNewline
    Write-Host "演示（不调用模型）" -ForegroundColor Gray
}
Write-Host "  数据  " -ForegroundColor DarkGray -NoNewline
Write-Host $dbPath -ForegroundColor Gray
Write-Host "  停止  " -ForegroundColor DarkGray -NoNewline
Write-Host "Ctrl+C" -ForegroundColor Gray
Write-Host ""

# ---------------------------------------------------------------- run

try {
    & $VenvPython @serveArgs
} finally {
    Write-Host ""
    Write-Host "  CTFAGENT 已停止。" -ForegroundColor DarkGray
    Write-Host "  运行记录保留在 ctfagent.db；工作区在 workspaces\。" -ForegroundColor DarkGray
    Write-Host ""
}
