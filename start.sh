#!/usr/bin/env bash
# One-click launcher for CTFAGENT (Linux / macOS / Git Bash).
#
#   ./start.sh              start the web UI on the first free port from 8788
#   ./start.sh --demo       offline demo backend (no model calls, no cost)
#   ./start.sh --port 9000  explicit port
#   ./start.sh --check      environment self-check, then exit
#   ./start.sh --restart    stop the running instance, then start fresh
#
# Safe to re-run: the virtualenv and dependencies are only created/installed
# when they are missing.

set -euo pipefail

cd "$(dirname "$0")"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

PORT=0
DEMO=0
CHECK=0
RESTART=0
BIND=""

while [ $# -gt 0 ]; do
  case "$1" in
    --demo)   DEMO=1; shift ;;
    --restart) RESTART=1; shift ;;
    --check)  CHECK=1; shift ;;
    --port)   PORT="${2:-0}"; shift 2 ;;
    --host)   BIND="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  DIM='\033[2m'; OK='\033[32m'; WARN='\033[33m'; BAD='\033[31m'; CYAN='\033[36m'; OFF='\033[0m'
else
  DIM=''; OK=''; WARN=''; BAD=''; CYAN=''; OFF=''
fi

step() { printf "  ${DIM}%s${OFF}\n" "$1"; }
ok()   { printf "  ${OK}ok${OFF}  %s\n" "$1"; }
warn() { printf "  ${WARN}!${OFF}   %s\n" "$1"; }
fail() { printf "  ${BAD}x${OFF}   %s\n" "$1"; }
head() { printf "\n%s\n" "$1"; }

VENV_PY=".venv/bin/python"
# A virtualenv created on Windows (including one this shell sees through
# /mnt/c or /d) uses Scripts/python.exe. Prefer whichever actually exists:
# the two layouts are mutually exclusive in practice, and picking the wrong
# one silently creates a second, unusable environment.
if [ ! -x "$VENV_PY" ] && [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
fi

# Refuse to build a venv that the platform cannot run. The failure mode this
# guards against is real: running this script from WSL/Git Bash against a
# Windows checkout would otherwise create a Linux venv inside .venv/ and
# overwrite the Windows one's pyvenv.cfg.
venv_usable() {
  [ -n "$1" ] && [ -x "$1" ] && "$1" -c "import sys" >/dev/null 2>&1
}

echo
printf "  CTFAGENT  ${DIM}· 多 agent CTF 解题${OFF}\n"

head "环境"

NEED_INSTALL=0
if ! venv_usable "$VENV_PY"; then
  if [ -d .venv ]; then
    fail ".venv 存在但其中的解释器不可用（可能是另一平台创建的）"
    printf "     删掉 .venv 后重跑：rm -rf .venv && ./start.sh\\n"
    exit 1
  fi
  step "创建虚拟环境 .venv（首次运行，需要几十秒）"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
  else
    python3 -m venv .venv
  fi
  VENV_PY=".venv/bin/python"
  [ -x "$VENV_PY" ] || VENV_PY=".venv/Scripts/python.exe"
  NEED_INSTALL=1
fi

# The launcher must not silently "repair" an existing environment: on Windows
# the venv may have been built by start.ps1, and reinstalling from here would
# duplicate that work (or fail if this shell cannot reach the package index).
if ! "$VENV_PY" -c "import fastapi, uvicorn, jinja2, claude_agent_sdk" >/dev/null 2>&1; then
  NEED_INSTALL=1
fi

if [ "$NEED_INSTALL" = "1" ]; then
  step "安装依赖（首次运行需要一两分钟）"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV_PY" -e ".[dev]"
  else
    "$VENV_PY" -m pip install --quiet --upgrade pip
    "$VENV_PY" -m pip install -e ".[dev]"
  fi
  ok "依赖已安装"
else
  ok "依赖已就绪"
fi

ok "Python $("$VENV_PY" -c 'import sys; print("{}.{}.{}".format(*sys.version_info[:3]))')  (.venv)"

CLI_PATH="$("$VENV_PY" -c 'from ctfagent.backend import resolve_cli_path; print(resolve_cli_path())' 2>/dev/null || true)"
if [ -n "$CLI_PATH" ]; then
  ok "claude CLI  $CLI_PATH"
else
  warn "未找到原生 claude CLI；SDK 会回退到自带 CLI（可能版本较旧）"
fi

if [ -d plugins/ctf-skills/skills ]; then
  COUNT=$(find plugins/ctf-skills/skills -mindepth 2 -maxdepth 2 -name SKILL.md | wc -l | tr -d ' ')
  ok "技能库      $COUNT 个"
else
  warn "技能库缺失：plugins/ctf-skills/skills"
fi

SANDBOX="$("$VENV_PY" -c 'from ctfagent.config import load_config; from ctfagent.workspace import probe_sandbox; s = probe_sandbox(load_config()); print(s.mode, s.available)' 2>/dev/null || true)"
case "$SANDBOX" in
  "docker True")  ok "沙箱        docker（可用）" ;;
  docker*)        warn "沙箱        docker 不可用；将降级为宿主机执行" ;;
  *)              ok "沙箱        宿主机（未启用 docker）" ;;
esac

if [ "$DEMO" = "1" ]; then
  ok "模型        演示模式（--scripted，不调用模型）"
elif [ -n "${ANTHROPIC_BASE_URL:-}${ANTHROPIC_AUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ] || [ -f config.toml ]; then
  ok "模型        ${ANTHROPIC_BASE_URL:-config.toml / CLI 默认}"
else
  warn "没有检测到模型凭据"
  printf "     界面可以正常打开，但开跑会失败。二选一：\n"
  printf "       1) export ANTHROPIC_BASE_URL=... ANTHROPIC_AUTH_TOKEN=... 后再启动\n"
  printf "       2) 先看界面：./start.sh --demo\n"
fi

if [ "$CHECK" = "1" ]; then
  echo
  exec "$VENV_PY" -m ctfagent doctor
fi

# Starting a second server against the same database is worse than refusing:
# its startup sweep marks the first server's live runs as interrupted. Reuse
# the running one instead.
RUNNING="$("$VENV_PY" -c 'from ctfagent.config import load_config; from ctfagent import instance; r = instance.running_instance(load_config()); print("{} {}".format(r["pid"], r["port"])) if r else None' 2>/dev/null || true)"
if [ -n "$RUNNING" ]; then
  RUN_PID="${RUNNING%% *}"
  RUN_PORT="${RUNNING##* }"
  if curl -fsS --max-time 3 "http://127.0.0.1:$RUN_PORT/api/health" >/dev/null 2>&1; then
    # The pid file is written at startup, so its mtime stands in for the
    # process start time -- portable, and it is the same comparison the UI
    # banner makes when it decides the running code is out of date.
    STALE=""
    if [ -f "$PWD/ctfagent.pid" ]; then
      # ``command head`` is not a formality: ``head()`` is defined above as a
      # section-title printer, and an unqualified ``head`` here resolves to that
      # function. It exits without reading, so ``find`` dies of SIGPIPE, and
      # ``set -o pipefail`` turns the pipeline into a 141 that ``set -e`` then
      # aborts on — ``--restart`` silently did nothing at all.
      STALE="$(find "$PWD/ctfagent" -name '*.py' -newer "$PWD/ctfagent.pid" 2>/dev/null | command head -1)"
    fi
    DO_RESTART=$RESTART
    if [ "$DO_RESTART" -eq 0 ] && [ -n "$STALE" ]; then
      head "已在运行，但代码比进程新"
      warn "有 .py 在进程启动后被改动：${STALE#"$PWD"/}"
      printf "  继续用这个实例，新加的页面会 404。\n"
      if [ -t 0 ]; then
        printf "  重启？会中断正在跑的会话 [y/N] "
        read -r ANSWER || ANSWER=""
        case "$ANSWER" in y|Y|yes|YES) DO_RESTART=1 ;; esac
      else
        printf "  加 --restart 可自动重启。\n"
      fi
    fi

    if [ "$DO_RESTART" -eq 1 ]; then
      head "重启"
      # ``kill`` in Git Bash only addresses processes this shell started (MSYS
      # pids). A server launched from a native Windows terminal is invisible to
      # it: the kill failed, the script printed "已停止" anyway, and the next
      # ``serve`` then refused with exit 3 because the old one still held the
      # database. Try both, then let the health check decide what to print.
      kill "$RUN_PID" 2>/dev/null || true
      if command -v taskkill >/dev/null 2>&1; then
        taskkill //F //PID "$RUN_PID" >/dev/null 2>&1 || true
      fi
      for _ in $(seq 1 60); do
        if curl -fsS --max-time 1 "http://127.0.0.1:$RUN_PORT/api/health" >/dev/null 2>&1; then
          sleep 0.25
        else
          break
        fi
      done
      if curl -fsS --max-time 1 "http://127.0.0.1:$RUN_PORT/api/health" >/dev/null 2>&1; then
        fail "pid $RUN_PID 仍在监听 $RUN_PORT，未重启"
        printf "  手动结束它再试：taskkill /F /PID %s\\n" "$RUN_PID"
        exit 1
      fi
      ok "已停止 pid $RUN_PID，端口 $RUN_PORT 已释放"
      # The pid file belongs to the process just stopped; leaving it behind makes
      # the next ``instance.acquire`` think an instance is still running.
      rm -f "$PWD/ctfagent.pid" 2>/dev/null || true
    else
      head "已在运行"
      ok "CTFAGENT pid $RUN_PID   http://127.0.0.1:$RUN_PORT/"
      printf "\n  要重启：./start.sh --restart\n"
      if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:$RUN_PORT/" >/dev/null 2>&1 || true
      elif command -v open >/dev/null 2>&1; then open "http://127.0.0.1:$RUN_PORT/" >/dev/null 2>&1 || true
      fi
      exit 0
    fi
  else
    warn "发现残留的实例记录（pid $RUN_PID 已不在监听），继续启动"
  fi
fi

head "启动"

port_free() {
  "$VENV_PY" - "$1" <<'PY'
import socket, sys
s = socket.socket()
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

if [ "$PORT" = "0" ]; then
  for candidate in $(seq 8788 8807); do
    if port_free "$candidate"; then PORT=$candidate; break; fi
  done
  [ "$PORT" != "0" ] || { fail "8788 起的 20 个端口都被占用"; exit 1; }
  [ "$PORT" = "8788" ] || warn "8788 被占用，改用 $PORT"
elif ! port_free "$PORT"; then
  fail "端口 $PORT 已被占用。换一个：./start.sh --port 9000"
  exit 1
fi

ARGS=(-m ctfagent serve --port "$PORT")
[ -n "$BIND" ] && ARGS+=(--host "$BIND")
[ "$DEMO" = "1" ] && ARGS+=(--scripted)

DB_PATH="$("$VENV_PY" -c 'from ctfagent.config import load_config; print(load_config().db_path)' 2>/dev/null || echo ctfagent.db)"

echo
printf "  界面  ${CYAN}http://127.0.0.1:%s/${OFF}\n" "$PORT"
[ "$DEMO" = "1" ] && printf "  模式  %s\n" "演示（不调用模型）"
printf "  数据  %s\n" "$DB_PATH"
printf "  停止  Ctrl+C\n"
echo

exec "$VENV_PY" "${ARGS[@]}"
