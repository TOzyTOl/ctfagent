"""Workspace attachment handling: the upload path is operator-controlled input."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ctfagent.workspace import Workspace


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path / "ws", 1, "demo")


def test_reserve_upload_keeps_only_the_basename(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    target = ws.reserve_upload("../../etc/passwd")
    assert target.parent == ws.attachments
    assert target.name == "passwd"


def test_reserve_upload_handles_windows_separators(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    target = ws.reserve_upload(r"..\..\Windows\System32\drivers\etc\hosts")
    assert target.parent == ws.attachments
    assert target.name == "hosts"


def test_reserve_upload_dedupes_instead_of_overwriting(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    first = ws.add_upload("dump.bin", b"one")
    second = ws.add_upload("dump.bin", b"two")
    third = ws.add_upload("dump.bin", b"three")

    assert first.name == "dump.bin"
    assert second.name == "dump-2.bin"
    assert third.name == "dump-3.bin"
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"


def test_reserve_upload_rejects_empty_and_dot_names(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    assert ws.reserve_upload("").name == "upload"
    assert ws.reserve_upload("..").name == "upload"
    assert ws.reserve_upload("///").name == "upload"


def test_reserve_upload_strips_path_hostile_characters(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    target = ws.reserve_upload('a<b>c:"d"|e?f*.txt')
    assert target.parent == ws.attachments
    assert "<" not in target.name and "|" not in target.name


def test_create_without_source_dir_does_not_copy_the_cwd(tmp_path: Path) -> None:
    ws = Workspace.create(tmp_path / "ws", 2, "paste-only", None)
    assert list(ws.attachments.iterdir()) == []


def test_host_guidance_names_the_real_shell_and_its_traps() -> None:
    from ctfagent.workspace import SandboxStatus

    text = SandboxStatus("none", False, "test", wsl="Ubuntu-20.04", git_bash=r"D:\git\bash.exe")
    guidance = text.guidance

    assert "PowerShell 5.1" in guidance
    # Each of these is a measured host behaviour that cost a run a wrong result.
    assert "`&&` and `||` are syntax errors" in guidance
    assert "UTF-16LE" in guidance
    assert "python -I -X utf8" in guidance
    assert "Ubuntu-20.04" in guidance
    assert r"D:\git\bash.exe" in guidance
    # The format string has literal braces; they must survive formatting intact.
    assert "{ ... }" in guidance
    assert "{{" not in guidance


def test_host_guidance_omits_toolchain_lines_it_did_not_detect() -> None:
    from ctfagent.workspace import SandboxStatus

    guidance = SandboxStatus("none", False, "test").guidance
    assert "WSL is installed" not in guidance
    assert "Git Bash is available" not in guidance
    assert "PowerShell 5.1" in guidance


def test_docker_mode_does_not_claim_container_execution() -> None:
    """Nothing wraps the shell tool, so claiming a container would mislead.

    A worker told it is inside a container reaches for Linux paths and binaries
    that are not there, and reports the challenge as broken rather than the
    environment as different.
    """
    from ctfagent.workspace import SandboxStatus

    status = SandboxStatus("docker", True, "server 29.3.1")
    assert "容器执行还没有实现" in status.guidance
    assert "PowerShell 5.1" in status.guidance  # the commands still run on Windows
    assert "容器执行尚未实现" in status.note


def test_install_skills_links_only_dirs_that_carry_a_skill_md(tmp_path: Path) -> None:
    """An empty skill directory is not a skill.

    The vendored tree was once emptied to eleven directories with zero files.
    ``install_skills`` correctly linked nothing, but nothing said so: the
    sessions started without a technique index and ``doctor`` still printed
    ``已找到`` because it only checked that the root directory existed.
    """
    root = tmp_path / "vendored"
    (root / "real-skill").mkdir(parents=True)
    (root / "real-skill" / "SKILL.md").write_text("---\nname: real-skill\n---\n", encoding="utf-8")
    (root / "empty-skill").mkdir()
    (root / "loose-file.md").write_text("not a skill", encoding="utf-8")

    ws = _workspace(tmp_path)
    assert ws.install_skills(root) == ["real-skill"]


def test_an_empty_skill_library_installs_nothing(tmp_path: Path) -> None:
    root = tmp_path / "vendored"
    for name in ("ctf-web", "ctf-reverse"):
        (root / name).mkdir(parents=True)
    ws = _workspace(tmp_path)
    assert ws.install_skills(root) == []


def test_a_new_workspace_is_its_own_project_root(tmp_path: Path) -> None:
    """``work/`` must carry a git repo, or Claude Code adopts the operator's.

    ``setting_sources=["project"]`` resolves "project" by walking up from cwd to
    the first ``.git``. Run workspaces live under ``D:\\agentsec\\CTFAGENT\\
    workspaces``, so without a marker of their own the walk reaches
    ``D:\\agentsec\\.claude`` — 34 more skills, ``skillOverrides`` among them —
    and a worker's single allowed skill can be shadowed by a same-named one
    from the operator's tree. Measured: 27 skills with the marker, 48 without.
    """
    ws = _workspace(tmp_path)
    assert (ws.work / ".git" / "HEAD").is_file()
    assert (ws.work / ".git" / "config").is_file()
    # The marker is hand-written, so it must still be a repository git accepts:
    # a bare directory was tried first and git refused it as "not a repository".
    out = subprocess.run(
        ["git", "-C", str(ws.work), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode == 0:
        assert Path(out.stdout.strip()).resolve() == ws.work.resolve()


def test_an_adopted_workspace_gains_the_project_root_it_lacks(tmp_path: Path) -> None:
    """Runs created before the marker existed are re-attached, not re-created.

    ``adopt`` reuses the workspace on disk; a run from before this change has no
    ``.git`` there and would keep leaking the operator's skills for the rest of
    its life (its writer runs on adopt).
    """
    root = tmp_path / "ws" / "run-3" / "old"
    (root / "work").mkdir(parents=True)
    assert not (root / "work" / ".git").exists()

    ws = Workspace.existing(tmp_path / "ws", 3, "old")
    assert (ws.work / ".git" / "HEAD").is_file()


def test_the_project_root_marker_is_not_rewritten(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    head = ws.work / ".git" / "HEAD"
    head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    Workspace.existing(tmp_path / "ws", 1, "demo")
    assert head.read_text(encoding="utf-8") == "ref: refs/heads/main\n"


def test_missing_skills_names_a_single_absent_role_skill(tmp_path: Path) -> None:
    """A partly-stocked library is the quiet failure, not an empty one.

    Each worker is allowed exactly one skill, so losing ``ctf-pwn`` costs the
    pwn direction its index while every other direction looks fine and the run
    reports nothing at all.
    """
    root = tmp_path / "vendored"
    for name in ("ctf-web", "ctf-reverse"):
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    ws = _workspace(tmp_path)
    ws.install_skills(root)

    assert ws.missing_skills(["ctf-web", "ctf-reverse"]) == []
    assert ws.missing_skills(["ctf-web", "ctf-pwn"]) == ["ctf-pwn"]


def test_missing_skills_ignores_the_order_of_repeats(tmp_path: Path) -> None:
    """``ROLE_SKILL`` maps eight roles onto eleven skills; report each once."""
    ws = _workspace(tmp_path)
    assert ws.missing_skills(["solve-challenge", "solve-challenge", "ctf-web"]) == [
        "solve-challenge",
        "ctf-web",
    ]
