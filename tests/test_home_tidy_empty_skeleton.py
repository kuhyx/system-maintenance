"""Sweep deletes all-directory trees instead of filing them in inbox/."""

from __future__ import annotations

import os
import time
from pathlib import Path

import _home_tidy_sweep
import home_tidy
import pytest
from _home_tidy_manifest import load_manifest
from _home_tidy_sweep import (
    EMPTY_REASON,
    Move,
    append_moves,
    is_empty_skeleton,
    read_moves,
    remove_empty_tree,
    sweep,
    undo,
)
from home_tidy_fixture import MANIFEST

NOW = time.time()
OLD = NOW - 86_400


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home-tidy.toml").write_text(MANIFEST)
    for b in ("src", "inbox"):
        (tmp_path / b).mkdir()
    os.utime(tmp_path / "home-tidy.toml", (NOW, NOW))  # inside grace: not swept
    return tmp_path


def _skeleton(home: Path, name: str = "signal-bot") -> Path:
    top = home / name
    (top / "app" / "build" / "release" / "x86_64").mkdir(parents=True)
    os.utime(top, (OLD, OLD))
    return top


def _manifest(home: Path):
    return load_manifest(home / "home-tidy.toml", home)


def test_is_empty_skeleton_cases(tmp_path: Path) -> None:
    assert not is_empty_skeleton(tmp_path / "missing", 50)
    (tmp_path / "f").write_text("x")
    assert not is_empty_skeleton(tmp_path / "f", 50)
    os.symlink(tmp_path / "f", tmp_path / "lnk")
    assert not is_empty_skeleton(tmp_path / "lnk", 50)
    deep = tmp_path / "deep"
    (deep / "a" / "b").mkdir(parents=True)
    assert is_empty_skeleton(deep, 50)
    assert not is_empty_skeleton(deep, 1)  # too big to call empty
    (deep / "a" / "b" / "keep.txt").write_text("x")
    assert not is_empty_skeleton(deep, 50)
    linked = tmp_path / "linked"
    linked.mkdir()
    os.symlink(deep, linked / "to-deep")
    assert not is_empty_skeleton(linked, 50)


def test_remove_empty_tree_refuses_a_file(tmp_path: Path) -> None:
    top = tmp_path / "t"
    (top / "a").mkdir(parents=True)
    (top / "a" / "late.txt").write_text("appeared after the check")
    with pytest.raises(OSError):
        remove_empty_tree(top)
    assert (top / "a" / "late.txt").exists()


def test_sweep_removes_skeleton_and_journals(home: Path) -> None:
    state = home / ".state"
    top = _skeleton(home)
    moves, _ = sweep(_manifest(home), state, NOW, dry_run=True)
    assert [(m.src, m.dst, m.reason) for m in moves] == [(str(top), "", EMPTY_REASON)]
    assert top.exists() and not read_moves(state)
    moves, _ = sweep(_manifest(home), state, NOW, dry_run=False)
    assert not top.exists() and not (home / "inbox" / "signal-bot").exists()
    assert [m.reason for m in read_moves(state)] == [EMPTY_REASON]


def test_sweep_still_files_a_tree_with_a_file(home: Path) -> None:
    top = _skeleton(home, "real")
    (top / "app" / "note.txt").write_text("x")
    os.utime(top, (OLD, OLD))
    moves, _ = sweep(_manifest(home), home / ".state", NOW, dry_run=False)
    assert [m.reason for m in moves] == ["root-allow"]
    assert (home / "inbox" / "real" / "app" / "note.txt").exists()


def test_sweep_reports_a_failed_removal(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    top = _skeleton(home)

    def _raise(_path: Path) -> None:
        raise OSError("Directory not empty")

    monkeypatch.setattr(_home_tidy_sweep, "remove_empty_tree", _raise)
    moves, skipped = sweep(_manifest(home), home / ".state", NOW, dry_run=False)
    assert moves == [] and top.exists()
    assert skipped[-1:] == [
        "signal-bot: empty-skeleton removal failed (Directory not empty)"
    ]


def test_undo_skips_removed_skeletons(home: Path) -> None:
    state = home / ".state"
    gone = Move(str(home / "gone"), "", "2026-01-01T00:00:00", EMPTY_REASON, "b1")
    append_moves(state, [gone])
    assert undo(state, "all") == []
    assert not (home / "gone").exists()
    assert read_moves(state) == [gone]


def test_cli_prints_removed_skeleton(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    top = _skeleton(home)
    base = [
        "--home",
        str(home),
        "--manifest",
        str(home / "home-tidy.toml"),
        "--state-dir",
        str(home / ".state"),
    ]
    home_tidy.main([*base, "sweep", "--dry-run"])
    assert f"would remove empty skeleton: {top}\n" in capsys.readouterr().out
    home_tidy.main([*base, "sweep"])
    assert f"removed empty skeleton: {top}\n" in capsys.readouterr().out
    assert not top.exists()
