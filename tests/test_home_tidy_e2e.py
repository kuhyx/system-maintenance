"""End-to-end: migrate a fixture home, then check, sweep and undo it."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from home_tidy_fixture import build_home

import home_tidy


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A populated fixture home with git identity set for the commits."""
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    build_home(tmp_path)
    return tmp_path


def _run(home: Path, *args: str) -> int:
    return home_tidy.main(["--home", str(home), "--manifest", str(home / "home-tidy.toml"), "--state-dir", str(home / ".state"), *args])


def test_plan_is_read_only(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(fake_home, "migrate", "--plan") == 0
    out = capsys.readouterr().out
    assert "todo               ->  src/todo" in out
    assert (fake_home / "todo").is_dir() and not (fake_home / "src").exists()
    assert (fake_home / ".state" / "plan.diff").read_text().count("+++ ") >= 10


def test_apply_needs_yes(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(fake_home, "migrate", "--apply") == 2
    assert "--yes" in capsys.readouterr().out


def test_apply_migrates_everything(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = str(fake_home)
    rc = _run(fake_home, "migrate", "--apply", "--yes")
    out = capsys.readouterr().out
    assert rc == 1  # pushes fail: the fixture repos have no remote
    assert "bridgeless verify passed" in out
    root = sorted(p.name for p in fake_home.iterdir() if not p.name.startswith("."))
    assert root == ["Documents", "Downloads", "archive", "data", "fake_finish.sh", "home-tidy.toml", "inbox", "services", "src", "vendor"]
    assert (fake_home / "src" / "inner" / "i.txt").read_text() == f"{h}/src/inner/i.txt\n"
    assert (fake_home / "archive" / "2026-01-01-wrap-leftovers" / "left.txt").exists()
    assert 'XDG_DESKTOP_DIR="$HOME/inbox"' in (fake_home / ".config" / "user-dirs.dirs").read_text()
    assert (fake_home / ".config" / "user-dirs.conf").read_text() == "enabled=False\n"
    assert 'export GOPATH="$HOME/data/go"' in (fake_home / ".zshenv").read_text()
    assert "WorkingDirectory=%h/src/todo" in (fake_home / ".config" / "systemd" / "user" / "todo.service").read_text()
    assert os.readlink(fake_home / ".local" / "bin" / "tool") == f"{h}/src/utils/bin/tool"
    assert os.readlink(fake_home / ".local" / "bin" / "rel-tool") == "../../src/utils/scripts/x.sh"
    data = json.loads((fake_home / ".claude.json").read_text())
    assert data["mcpServers"]["t"]["args"] == ["--dir", f"{h}/src/todo"]
    assert f"{h}/src/todo" in data["projects"] and data["projects"][f"{h}/src/todo"]["history"] == [f"ran {h}/todo/x"]
    assert (fake_home / ".claude" / "projects" / ("-" + h.strip("/").replace("/", "-") + "-src-todo") / "memory" / "MEMORY.md").read_text() == f"- lives at {h}/src/todo\n"
    assert (fake_home / "src" / "todo" / ".venv" / "bin" / "pip").read_text() == f"#!{h}/src/todo/.venv/bin/python3\n"
    assert (fake_home / "src" / "todo" / ".git" / "hooks" / "pre-commit").read_text().endswith(f"{h}/src/todo/.venv/bin/python\n")
    assert "~/src/utils/gone/old.py" in (fake_home / "src" / "todo" / "README.md").read_text()
    log = subprocess.run(["git", "-C", str(fake_home / "src" / "todo"), "log", "-1", "--format=%s%n%b"], capture_output=True, text=True, check=True).stdout
    assert log.startswith("rewrite")  # the fixture routes commits through fake_finish.sh
    assert not any(p.is_symlink() for p in fake_home.iterdir())
    assert 'paths = []' in (fake_home / "home-tidy.toml").read_text()
    # resumable: a second run has nothing to move and reports the same pushes
    assert _run(fake_home, "migrate", "--apply", "--yes") == 1
    assert "Plan aborted" not in capsys.readouterr().out


def test_check_sweep_undo(fake_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(fake_home, "migrate", "--apply", "--yes")
    capsys.readouterr()
    assert _run(fake_home, "check") == 1
    assert "root-allow: fake_finish.sh" in capsys.readouterr().out
    old = 1_700_000_000
    for name in ("fake_finish.sh", "home-tidy.toml"):
        os.utime(fake_home / name, (old, old))
    (fake_home / "inbox" / "junk.txt").touch()
    os.utime(fake_home / "inbox" / "junk.txt", (old, old))
    assert _run(fake_home, "sweep", "--dry-run") == 1
    assert "would move" in capsys.readouterr().out
    assert (fake_home / "fake_finish.sh").exists()
    assert _run(fake_home, "sweep") == 0
    out = capsys.readouterr().out
    assert "moved:" in out and "inbox: 3 item(s) older" in (fake_home / ".state" / "report.txt").read_text()
    assert (fake_home / "inbox" / "fake_finish.sh").exists()
    assert _run(fake_home, "undo", "last") == 0
    assert "2 move(s) restored" in capsys.readouterr().out
    assert (fake_home / "fake_finish.sh").exists() and not (fake_home / "inbox" / "fake_finish.sh").exists()
