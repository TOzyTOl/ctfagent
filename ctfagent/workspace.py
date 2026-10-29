"""Workspace layout, skill installation, and sandbox probing.

Layout per run:

    workspaces/run-<id>/<challenge>/
        .claude/skills/<skill>/    vendored CTF skills (junction, copy as fallback)
        attachments/               copy of the challenge input (untrusted data)
        work/                      the agent's working directory; everything persists here
        writeup.md

``work/`` also carries an empty git repository. It is not for version control: it
is the marker that stops Claude Code's project-root search, which otherwise walks
up out of the workspace and adopts the operator's own ``.claude`` — skills,
settings and ``skillOverrides`` included. Measured on this machine: with the
marker a session under ``workspaces/`` sees 27 skills (the 11 vendored ones),
without it 48 (the operator's 34 plus the vendored ones, ``ctf-reverse`` among
them). The repository is written by hand rather than by ``git init`` so run
preparation does not depend on a ``git`` executable being on PATH.

Workers get ``work/`` as their cwd. Attachments are copied, not linked, because a
worker will modify or extract from them and the operator's original directory
must not be touched.

Skills are installed as a **directory junction** on Windows (creating one needs
no administrator rights, unlike a symlink) and a symlink elsewhere, falling back
to a copy. The skill library is ~3 MB; a copy per run is wasteful but never
wrong, so the fallback is not an error path.

Sandbox: ``sandbox.mode = "docker"`` is meant to run shell commands inside a
container with ``work/`` and ``attachments/`` mounted. Nothing in this package
actually wraps the shell tool, though, so today the session's shell always runs
on the host; what the mode changes is only the advice in the prompt. That makes
the host's real capabilities (WSL, Git Bash, PowerShell's quirks) worth probing
once and telling the agent, because a worker that does not know WSL exists tries
to run an ELF binary with Windows Python and concludes the challenge is broken.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import AppConfig

#: What the agent needs to know about a host shell. Written as facts that were
#: checked on this machine, not as general Windows advice: each bullet cost a
#: wasted command or a wrong conclusion in a past run.
HOST_SHELL_BLOCK = """\
Commands run on the operator's Windows machine, through PowerShell 5.1 (Windows \
PowerShell — there is no `pwsh`). The following are measured facts, not style advice:

- **`&&` and `||` are syntax errors** in PowerShell 5.1. Separate statements with `;`, or guard \
with `; if ($?) {{ ... }}`. A POSIX-style chain silently fails to parse and nothing runs.
- **`>` and `| Out-File` write UTF-16LE with a BOM.** Anything that later reads the file — \
`python`, `curl`, `grep`, a service — sees garbage or a BOM. Use `Set-Content -Encoding utf8` or \
write the file from Python.
- **Use `curl.exe` for target requests, not `Invoke-WebRequest`.** IWR routes through the \
machine's WinINET proxy, which answers `502` for any challenge port other than 80/443 — a fake \
result that looks like the target is down, and `-Proxy $null` does not redirect it. `curl.exe` \
honours `NO_PROXY` (set below when this run has a target), so it goes direct.
- **`python -I` ignores `PYTHONUTF8`**, so its stdout is GBK and any Chinese text in a print \
raises `codec can't encode`. When you need `-I`, write `python -I -X utf8`. Without `-I`, the \
run's environment already sets UTF-8.
- **`Bash` is not a tool name on this host**; the shell tool is `PowerShell`. A relative path \
with forward slashes works in both; `~/` and `$HOME` do not mean what they do on Linux.
{wsl_line}{bash_line}"""

WSL_LINE = """\
- **WSL is installed and is the only place Linux tools really exist**: \
`wsl -e bash -lc '<cmd>'` gives {distro} with `python3`, `gdb`, `strace`, `ltrace`, `readelf`, \
`objdump`, `file`, `strings`, `nm`, `xxd`, `nc`, `tcpdump`, `openssl`, `perl` and `java`. Linux \
ELF binaries, pwntools-style workflows and anything that assumes `/proc` belong there. The working \
directory is reachable as `/mnt/<drive>/<path>`. WSL does not inherit the Windows proxy \
variables, so its network already goes straight out.
"""

BASH_LINE = """\
- **Git Bash is available** at `{path}` for POSIX coreutils and shell scripts. Its `python3` is \
the Windows interpreter (GBK output by default), not a Linux one — do not assume it is a Linux \
toolchain.
"""

#: Result of ``_host_toolchain``; ``None`` until the first probe.
_HOST_TOOLCHAIN: dict[str, str] | None = None

#: Python packages a CTF worker should reach for instead of hand-rolling the
#: primitive. Ordered by how often a run needs them.
_PY_TOOLKIT: tuple[tuple[str, str], ...] = (
    ("pwn", "pwntools"),
    ("z3", "z3-solver"),
    ("gmpy2", "gmpy2"),
    ("Crypto", "pycryptodome"),
    ("capstone", "capstone"),
    ("angr", "angr"),
    ("unicorn", "unicorn"),
    ("elftools", "pyelftools"),
)

#: Result of ``_python_toolkit``; ``None`` until the first probe.
_PY_TOOLKIT_LINE: str | None = None


#: Result of ``_wsl_pwn_toolkit``; ``None`` until the first probe.
_WSL_PWN_LINE: str | None = None

#: What the pwn workflow inside WSL needs. Probed in one ``bash -lc`` round
#: trip: the wsl.exe hop costs a few hundred milliseconds and this feeds every
#: pwn worker prompt, so the checks travel together.
_WSL_PWN_CHECKS = (
    ('python3 -c "import pwn" >/dev/null 2>&1', "pwntools"),
    ("command -v patchelf >/dev/null 2>&1", "patchelf"),
    ("command -v ROPgadget >/dev/null 2>&1", "ROPgadget"),
    ('grep -q "source ~/.gef.py" ~/.gdbinit >/dev/null 2>&1', "GEF"),
)


def wsl_pwn_toolkit(distro: str) -> str:
    """Which pwn tools live inside the WSL distro, as one line (cached).

    Empty when anything is missing is NOT the rule — a partial toolkit is
    still worth naming; the caller stays silent only when nothing answered,
    because promising a Linux pwn environment that is not there sends the
    worker into the /mnt/d execution trap with no way out.
    """
    global _WSL_PWN_LINE
    if _WSL_PWN_LINE is not None or not distro:
        return _WSL_PWN_LINE or ""
    wsl = shutil.which("wsl")
    if not wsl:
        _WSL_PWN_LINE = ""
        return ""
    script = "; ".join(f"{cmd} && echo {label} || true" for cmd, label in _WSL_PWN_CHECKS)
    try:
        proc = subprocess.run(
            [wsl, "-d", distro, "-e", "bash", "-lc", script],
            capture_output=True, text=True, timeout=20, errors="replace",
        )
        have = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        _WSL_PWN_LINE = "、".join(dict.fromkeys(have))
    except (OSError, subprocess.TimeoutExpired):
        _WSL_PWN_LINE = ""
    return _WSL_PWN_LINE


#: The pwn workflow, for roles that exploit native binaries. The two rules
#: that cost real runs the most are baked in: binaries must be copied into
#: the distro before they can run (the /mnt/d mount does not carry exec
#: bits, observed live when workers discovered the ~/work workaround on
#: their own, one run at a time), and scripts/artifacts stay in the
#: workspace because the verifier re-runs them from there.
WSL_PWN_BLOCK = """
# 跑 Linux 二进制（宿主是 Windows，ELF 不能直接执行）
用 WSL 里的真 Linux 环境：`wsl -d {distro} -e bash -lc '<命令>'`。已装：{tools}（gdb 带 GEF）。

- **先把题目二进制复制进 WSL 本地目录再执行**：`/mnt/d` 挂载盘上 chmod/执行位不生效，直接跑会静默失败。
  `cp /mnt/d/<工作区>/attachments/x.bin ~/work/ && chmod +x ~/work/x.bin`
- **exp 脚本和产物一律写在本工作区 work/ 下**（verifier 要从那里独立复现）；`~/work` 只放二进制副本。
- 题目附了 libc/ld：先 `patchelf --set-interpreter ~/work/ld.so --replace-needed libc.so.6 \
~/work/libc.so.6 ~/work/x.bin` 再调试，不要拿系统 libc 冒充复现环境。
- 本地 `process()` 稳定后换 `remote(host, port)` 打远程；remote 遵守 NO_PROXY 直连目标。
  调 glibc 堆行为时注意：WSL 的内核/分配器与远程未必一致，以远程返回为准。
"""


def _python_toolkit() -> str:
    """Which CTF packages this interpreter can import, as one line.

    Probed once: the answer cannot change while the process lives, and this runs
    for every worker prompt. ``find_spec`` is used rather than an import because
    importing angr costs seconds and pulls a large dependency tree.
    """
    global _PY_TOOLKIT_LINE
    if _PY_TOOLKIT_LINE is not None:
        return _PY_TOOLKIT_LINE
    have: list[str] = []
    for module, label in _PY_TOOLKIT:
        try:
            if importlib.util.find_spec(module) is not None:
                have.append(label)
        except (ImportError, ValueError):
            continue
    _PY_TOOLKIT_LINE = "、".join(have)
    return _PY_TOOLKIT_LINE


@dataclass
class SandboxStatus:
    mode: str          # "none" | "docker"
    available: bool
    detail: str
    #: A usable WSL distro, if one was found. Commands on the host then have a
    #: real Linux toolchain one `wsl -e bash -lc` away.
    wsl: str = ""
    #: Path to a Git Bash, if one was found.
    git_bash: str = ""

    @property
    def note(self) -> str:
        """One line for the operator's console, not for the agent."""
        if self.mode == "docker" and self.available:
            # Do not claim container execution: nothing wraps the shell tool.
            return (
                f"Docker 可用（{self.detail}），但容器执行尚未实现，命令仍跑在宿主机上。"
            )
        if self.mode == "docker":
            return (
                "配置要求用 Docker，但它不可用 "
                f"（{self.detail}）；命令改为在宿主机上执行，ELF 程序可能跑不起来。"
            )
        return (
            "命令直接在宿主机（Windows）上执行。ELF 程序和 Linux 专用工具可能跑不起来，"
            "优先做静态分析，或者直接打远程服务。"
        )

    @property
    def guidance(self) -> str:
        """The environment section of a worker prompt.

        Docker mode does not wrap the shell tool yet, so the host block is what
        the agent actually needs. Saying "you are in a container" when the
        command runs on Windows is worse than saying nothing: the agent then
        reaches for Linux tools that are not there.
        """
        block = HOST_SHELL_BLOCK.format(
            wsl_line=WSL_LINE.format(distro=self.wsl) if self.wsl else "",
            bash_line=BASH_LINE.format(path=self.git_bash) if self.git_bash else "",
        )
        toolkit = _python_toolkit()
        if toolkit:
            # The failure this prevents: a reverse worker that needs modular
            # arithmetic writes its own 256-bit bignum library because nothing
            # told it gmpy2 was there. Observed in run 18 — five workers, 90
            # turns each, hand-rolling SM2 primitives.
            block += (
                "\n# Python 工具链（已安装，直接用）\n"
                f"{toolkit}\n"
                "`python` 就是装了这些包的那个解释器。动手写之前先 `import` 试一下——"
                "把 z3 一条约束就能解的东西手写成一万行搜索，是这里最常见的浪费。\n"
            )
        if self.mode == "docker":
            return (
                f"配置要求用 Docker（{self.detail}），但容器执行还没有实现，"
                "命令仍然跑在宿主机上。\n\n" + block
            )
        return block

    def pwn_guidance(self) -> str:
        """The WSL pwn workflow, for roles that exploit native binaries.

        Separate from ``guidance`` on purpose: the workflow paragraph costs
        context, and a web or crypto worker would carry it for nothing. Empty
        when there is no distro or nothing inside it — silence beats a
        promised environment that is not there.
        """
        tools = wsl_pwn_toolkit(self.wsl) if self.wsl else ""
        if not tools:
            return ""
        return WSL_PWN_BLOCK.format(distro=self.wsl, tools=tools)


def _host_toolchain() -> dict[str, str]:
    """Best-effort detection of a Linux toolchain reachable from the host.

    Every probe is cheap and failure is not an error: an empty result just means
    the prompt says nothing about it.

    ``wsl -l -q`` is not free (about 0.1 s on Windows, and it waits for the WSL
    service the first time), and ``probe_sandbox`` runs on every server start and
    in every test that builds an app. The answer cannot change while the process
    lives, so it is computed once.
    """
    global _HOST_TOOLCHAIN
    if _HOST_TOOLCHAIN is not None:
        return _HOST_TOOLCHAIN
    found: dict[str, str] = {}

    wsl = shutil.which("wsl")
    if wsl:
        try:
            # `wsl -l -q` writes UTF-16LE on this build, so it must stay bytes
            # until decoded: text mode would decode it as UTF-8 first and bury
            # the names in NULs.
            proc = subprocess.run([wsl, "-l", "-q"], capture_output=True, timeout=15)
            names = [
                line.strip().replace("\x00", "")
                for line in (proc.stdout or b"").decode("utf-16-le", "replace").splitlines()
            ]
            # docker-desktop is WSL plumbing, not a distro with a toolchain.
            for name in names:
                if name and not name.startswith("docker-"):
                    found["wsl"] = name
                    break
        except (OSError, subprocess.TimeoutExpired):
            pass

    bash = shutil.which("bash")
    if bash and os.name == "nt":
        found["git_bash"] = bash
    _HOST_TOOLCHAIN = found
    return found


def probe_sandbox(cfg: AppConfig) -> SandboxStatus:
    host = _host_toolchain()
    if cfg.sandbox.mode != "docker":
        return SandboxStatus("none", False, "配置里没有启用沙箱", **host)
    exe = shutil.which("docker")
    if not exe:
        return SandboxStatus("docker", False, "PATH 里找不到 docker 命令", **host)
    try:
        proc = subprocess.run(
            [exe, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxStatus("docker", False, f"docker info 执行失败：{exc}", **host)
    if proc.returncode != 0:
        first = (proc.stderr or proc.stdout or "").strip().splitlines()
        return SandboxStatus(
            "docker", False, f"连不上 docker 守护进程：{first[0] if first else '未知'}", **host
        )
    return SandboxStatus("docker", True, f"server {proc.stdout.strip()}", **host)


def _link_dir(target: Path, link: Path) -> bool:
    """Create a junction (Windows) or symlink (POSIX). Returns False if impossible."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
                text=True,
                # cmd.exe writes its messages in the console codepage, which is
                # GBK on a Chinese Windows. Without this, every skill junction
                # spawns a reader thread that dies decoding its own output.
                errors="replace",
            )
        else:
            link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


#: A git repository with no commits and no work tree to speak of. Claude Code
#: finds the project root by walking up from cwd to the first ``.git``; without
#: one here the walk leaves the workspace and lands on the operator's own
#: ``.claude`` under ``D:\\agentsec``, whose 34 skills and ``skillOverrides``
#: then apply to every worker. Hand-written rather than ``git init`` so run
#: preparation works on a machine with no git on PATH; ``git status`` accepts it
#: (verified) and the CLI only ever looks for the directory.
_GIT_FILES = {
    "HEAD": "ref: refs/heads/master\n",
    "config": (
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
    ),
    "description": "",
}


def _write_project_boundary(work: Path) -> None:
    """Make ``work`` its own Claude Code project root. Idempotent."""
    git = work / ".git"
    if git.exists():
        return
    (git / "objects").mkdir(parents=True)
    (git / "refs" / "heads").mkdir(parents=True)
    for name, text in _GIT_FILES.items():
        (git / name).write_text(text, encoding="utf-8")


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.work = root / "work"
        self.attachments = root / "attachments"
        #: Sessions run with cwd=work and ``setting_sources=["project"]``, so
        #: the project directory Claude Code reads is ``work/.claude``.
        self.claude = self.work / ".claude"

    @classmethod
    def create(
        cls,
        base: Path,
        run_id: int,
        challenge_name: str,
        source_dir: Path | None = None,
    ) -> "Workspace":
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in challenge_name)[:48]
        root = base / f"run-{run_id}" / (safe or "challenge")
        ws = cls(root)
        ws.work.mkdir(parents=True, exist_ok=True)
        ws.attachments.mkdir(parents=True, exist_ok=True)
        _write_project_boundary(ws.work)
        if source_dir is not None and source_dir.is_dir():
            for item in source_dir.iterdir():
                if item.name in ("challenge.yaml", "challenge.yml", "challenge.json"):
                    continue
                target = ws.attachments / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
        return ws

    @classmethod
    def existing(cls, base: Path, run_id: int, challenge_name: str) -> "Workspace":
        """Re-open a workspace created by an earlier process (no copying)."""
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in challenge_name)[:48]
        root = base / f"run-{run_id}" / (safe or "challenge")
        ws = cls(root)
        ws.work.mkdir(parents=True, exist_ok=True)
        ws.attachments.mkdir(parents=True, exist_ok=True)
        # Workspaces made before the boundary existed do not have one, and an
        # adopted run still starts sessions with cwd=work: without this, its
        # workers silently see the operator's skills instead of the vendored
        # ones. Adding an empty .git to a directory the agent already worked in
        # changes nothing else it can observe.
        _write_project_boundary(ws.work)
        return ws

    def reserve_upload(self, filename: str) -> Path:
        """Pick a safe, unused path under ``attachments/`` for one upload.

        Only the basename is used: an upload named ``../../etc/passwd`` must land
        in the workspace, not outside it. An existing name is suffixed rather
        than overwritten, because the operator may upload two files with the
        same name from different folders and losing one silently is worse than
        an odd name.

        Returns the path without creating the file, so a caller can stream a
        large attachment straight to disk instead of holding it in memory.
        """
        name = Path(filename.replace("\\", "/")).name or "upload"
        name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*').strip()
        if not name or name in (".", ".."):
            name = "upload"
        target = self.attachments / name
        if target.exists():
            stem, suffix = target.stem, target.suffix
            for index in range(2, 1000):
                candidate = self.attachments / f"{stem}-{index}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
        self.attachments.mkdir(parents=True, exist_ok=True)
        return target

    def add_upload(self, filename: str, data: bytes) -> Path:
        """Store one uploaded file under ``attachments/`` (small files)."""
        target = self.reserve_upload(filename)
        target.write_bytes(data)
        return target

    def install_skills(self, skills_root: Path) -> list[str]:
        """Expose the vendored skills at ``<work>/.claude/skills``.

        The session's cwd is ``work/`` and it runs with
        ``setting_sources=["project"]``, so Claude Code discovers skills at
        ``<cwd>/.claude/skills``. They must therefore live under ``work``, not
        under the run root — a mistake that silently yields zero skills.
        Only directories containing a ``SKILL.md`` are linked.
        """
        if not skills_root.is_dir():
            return []
        dest = self.claude / "skills"
        dest.mkdir(parents=True, exist_ok=True)
        installed: list[str] = []
        for skill_dir in sorted(skills_root.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
                continue
            target = dest / skill_dir.name
            if target.exists():
                installed.append(skill_dir.name)
                continue
            if _link_dir(skill_dir.resolve(), target.resolve()):
                installed.append(skill_dir.name)
                continue
            shutil.copytree(skill_dir, target)
            installed.append(skill_dir.name)
        return installed

    def missing_skills(self, names: Iterable[str]) -> list[str]:
        """Which of ``names`` have no usable ``SKILL.md`` under ``.claude/skills``.

        Checked on disk rather than against ``install_skills``'s return value:
        that list says what was linked, while this asks what the session will
        actually resolve when its allowlist names a skill. A junction whose
        target has since disappeared still exists as a directory, so the file
        itself is what gets probed.
        """
        root = self.claude / "skills"
        missing: list[str] = []
        for name in dict.fromkeys(names):
            if not (root / name / "SKILL.md").is_file():
                missing.append(name)
        return missing

    def write_note(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
