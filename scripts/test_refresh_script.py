"""Tests for the plugin refresh workflow."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REFRESH_SCRIPT = ROOT / "scripts" / "refresh.sh"


def _write_recorder(path: Path, command_name: str) -> None:
    path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f'printf "%s" "{command_name}" >> "$COMMAND_LOG"',
                'printf " %s" "$@" >> "$COMMAND_LOG"',
                'printf "\\n" >> "$COMMAND_LOG"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


def _write_noop(path: Path) -> None:
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def _run_refresh(tmp_path: Path, *arguments: str) -> list[str]:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    fake_bin = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(REFRESH_SCRIPT, scripts_dir / "refresh.sh")

    _write_recorder(fake_bin / "python", "python")
    _write_recorder(fake_bin / "git", "git")
    command_log = tmp_path / "commands.log"
    environment = {
        **os.environ,
        "COMMAND_LOG": str(command_log),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }

    subprocess.run(
        ["bash", str(scripts_dir / "refresh.sh"), *arguments],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return command_log.read_text(encoding="utf-8").splitlines()


def test_single_plugin_refresh_stages_only_its_lockfile(tmp_path: Path) -> None:
    commands = _run_refresh(tmp_path, "demo")

    assert commands == [
        "python scripts/lock-deps.py demo",
        "git add -A -- :(top,literal)plugins/demo/requirements.lock",
        "python scripts/build-registry.py",
    ]


def test_single_plugin_refresh_leaves_other_lockfile_unstaged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    fake_bin = tmp_path / "bin"
    scripts_dir.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(REFRESH_SCRIPT, scripts_dir / "refresh.sh")
    _write_noop(fake_bin / "python")

    for plugin_name in ("demo", "other"):
        plugin_dir = repo / "plugins" / plugin_name
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "requirements.lock").write_text(
            "before\n",
            encoding="utf-8",
        )

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        cwd=repo,
        check=True,
    )
    for plugin_name in ("demo", "other"):
        (repo / "plugins" / plugin_name / "requirements.lock").write_text(
            "after\n",
            encoding="utf-8",
        )

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    subprocess.run(
        ["bash", str(scripts_dir / "refresh.sh"), "demo"],
        cwd=repo,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    staged = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        text=True,
    ).splitlines()
    unstaged = subprocess.check_output(
        ["git", "diff", "--name-only"],
        cwd=repo,
        text=True,
    ).splitlines()

    assert staged == ["plugins/demo/requirements.lock"]
    assert unstaged == ["plugins/other/requirements.lock"]


def test_full_refresh_stages_all_plugin_lockfiles(tmp_path: Path) -> None:
    commands = _run_refresh(tmp_path)

    assert commands == [
        "python scripts/lock-deps.py",
        "git add -A -- :(glob)plugins/*/requirements.lock",
        "python scripts/build-registry.py",
    ]
