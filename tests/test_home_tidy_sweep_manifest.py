"""Sweep guardrails, the moves journal + undo, manifest helpers, the runner."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from home_tidy_fixture import MANIFEST

from _home_tidy_manifest import expand_home, load_manifest, write_bridges
from _home_tidy_run import Runner
from _home_tidy_sweep import (
    LOCK_NAME,
    Move,
    append_moves,
    forced_dry_run,
    read_moves,
    sweep,
    undo,
    unique_destination,
)

NOW = time.time()
OLD = NOW - 86_400


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home-tidy.toml").write_text(MANIFEST)
    for b in ("src", "inbox"):
        (tmp_path / b).mkdir()
    return tmp_path


def _manifest(home: Path):
    return load_manifest(home / "home-tidy.toml", home)


def _aged(path: Path) -> Path:
    os.utime(path, (OLD, OLD), follow_symlinks=False)
    return path


def test_sweep_moves_journals_and_skips(home: Path) -> None:
    state = home / ".state"
    (home / "stray.txt").write_text("x")
    _aged(home / "stray.txt")
    (home / "fresh.txt").write_text("x")
    big = home / "big"
    big.mkdir()
    (big / "blob").write_bytes(b"x" * 200_000)
    _aged(big)
    (home / "inbox" / "stray.txt").write_text("taken")  # forces the .1 suffix
    _aged(os.symlink("src", home / "lnk") or home / "lnk")
    _aged(home / "home-tidy.toml")
    moves, skipped = sweep(_manifest(home), state, NOW, dry_run=True)
    assert [Path(m.dst).name for m in moves] == ["home-tidy.toml", "lnk", "stray.txt.1"]
    assert (home / "stray.txt").exists() and not read_moves(state)
    assert any("fresh.txt" in s and "grace" in s for s in skipped)
    assert any("big" in s and "over sweep limit" in s for s in skipped)
    manifest = _manifest(home)
    moves, _ = sweep(manifest, state, NOW, dry_run=False)
    assert (home / "inbox" / "stray.txt.1").read_text() == "x" and not (home / "stray.txt").exists()
    assert len(read_moves(state)) == 3 and read_moves(state)[0].batch == moves[0].batch
    # lock file blocks the timer during a migration
    (state / LOCK_NAME).write_text("1")
    assert sweep(manifest, state, NOW, dry_run=False) == ([], ["migration lock present; sweep skipped"])


def test_sweep_bridges(home: Path) -> None:
    _aged(os.symlink("src", home / "old-repo") or home / "old-repo")
    write_bridges(home / "home-tidy.toml", "2020-01-01", ["old-repo"])
    moves, skipped = sweep(_manifest(home), home / ".state", NOW, dry_run=True)
    assert (home / "old-repo").is_symlink() and any("expired bridge" in s for s in skipped)
    write_bridges(home / "home-tidy.toml", "2040-01-01", ["old-repo"])
    moves, skipped = sweep(_manifest(home), home / ".state", NOW, dry_run=False)
    assert (home / "old-repo").is_symlink() and not any("old-repo" in m.src for m in moves)
    write_bridges(home / "home-tidy.toml", "2020-01-01", ["old-repo"])
    sweep(_manifest(home), home / ".state", NOW, dry_run=False)
    assert not (home / "old-repo").exists()


def test_undo_variants(home: Path) -> None:
    state = home / ".state"
    a, b, c = (home / n for n in ("a", "b", "c"))
    for p in (a, b, c):
        p.write_text(p.name)
    inbox = home / "inbox"
    os.rename(a, inbox / "a")
    os.rename(b, inbox / "b")
    os.rename(c, inbox / "c")
    append_moves(state, [Move(str(a), str(inbox / "a"), "2026-01-01T10:00:00", "r", "b1"), Move(str(b), str(inbox / "b"), "2026-01-02T10:00:00", "r", "b2")])
    append_moves(state, [Move(str(c), str(inbox / "c"), "2026-01-02T11:00:00", "r", "b3")])
    (inbox / "c").unlink()  # destination vanished: skipped, journal keeps it
    assert [m.src for m in undo(state, "last")] == []
    assert len(read_moves(state)) == 3
    assert [Path(m.src).name for m in undo(state, "2026-01-02")] == ["b"]
    assert b.read_text() == "b" and len(read_moves(state)) == 2
    a.write_text("conflict")  # source exists again: not overwritten
    assert undo(state, "all") == []
    a.unlink()
    assert [Path(m.src).name for m in undo(state, "all")] == ["a"]
    assert undo(home / "nostate", "last") == []


def test_unique_destination_and_dry_run_window(tmp_path: Path) -> None:
    d = tmp_path / "x"
    assert unique_destination(d) == d
    d.write_text("")
    (tmp_path / "x.1").write_text("")
    assert unique_destination(d) == tmp_path / "x.2"
    assert not forced_dry_run(tmp_path, 24, NOW)
    (tmp_path / "installed-at").write_text(str(NOW - 3600))
    assert forced_dry_run(tmp_path, 24, NOW) and not forced_dry_run(tmp_path, 0, NOW)
    (tmp_path / "installed-at").write_text("garbage")
    assert not forced_dry_run(tmp_path, 24, NOW)


def test_manifest_helpers(home: Path) -> None:
    m = _manifest(home)
    assert expand_home("~", home) == str(home) and expand_home("$HOME/x", home) == f"{home}/x"
    assert expand_home("/abs", home) == "/abs" and str(m.expand("${HOME}/y")) == f"{home}/y"
    assert m.hook_for("svc") is not None and m.hook_for("nope") is None
    assert m.ordered_mapping()[0][0] == "wrap/inner" and m.rewrite_mapping()["gone"] == "src/utils/gone"
    write_bridges(home / "home-tidy.toml", "2030-01-01", ["a", "b"])
    m = _manifest(home)
    assert m.bridges_expires == "2030-01-01" and m.bridges_paths == ("a", "b")


def test_runner(tmp_path: Path) -> None:
    lines: list[str] = []
    r = Runner(dry_run=False, log=lines.append, home=tmp_path)
    assert r.run("echo ~").stdout.strip() == str(tmp_path)
    assert r.run("printf x; exit 3").returncode == 3
    with pytest.raises(RuntimeError, match="command failed"):
        r.run("false", check=True)
    assert r.run("cat", stdin=b"in").stdout == "in"
    assert r.run("pwd", cwd=tmp_path).stdout.strip() == str(tmp_path)
    dry = Runner(dry_run=True, log=lines.append, home=tmp_path)
    assert dry.run("rm -rf /", sudo=True).ok and dry.sudo_available()
    assert dry.history == ["sudo rm -rf /"] and any("[dry-run]" in l for l in lines)
    argv = r._argv("ls ~/x $HOME/y", sudo=True)
    assert argv[:2] == ["sudo", "-n"] and argv[-1] == f"ls {tmp_path}/x {tmp_path}/y"
    assert Runner()._env()["HOME"] == os.environ["HOME"]


def test_sweep_refuses_to_bury_newer_data(home: Path) -> None:
    """A resurrected path holding the *live* copy must not be swept.

    Regression for 2026-09-12: the todo desktop wrapper still wrote
    ~/todo/BACKLOG.md while the MCP read ~/src/todo/BACKLOG.md. Sweeping
    would have filed the only current backlog under inbox/ and left every
    reader on the stale file, so the sweep now fails closed and names the
    offending file.
    """
    state = home / ".state"
    (home / "src" / "todo").mkdir()
    (home / "src" / "todo" / "BACKLOG.md").write_text("stale")
    os.utime(home / "src" / "todo" / "BACKLOG.md", (OLD, OLD))
    (home / "todo").mkdir()
    (home / "todo" / "BACKLOG.md").write_text("live")
    _aged(home / "todo")  # past the grace period, so only freshness can block

    moves, skipped = sweep(_manifest(home), state, NOW, dry_run=False)

    assert moves == [] and not read_moves(state)
    assert (home / "todo" / "BACKLOG.md").read_text() == "live"
    reason = next(s for s in skipped if s.startswith("todo:"))
    assert "newer than ~/src/todo" in reason
    assert "todo/BACKLOG.md" in reason  # names the file, so reconciling is a diff


def test_sweep_allows_resurrection_once_reconciled(home: Path) -> None:
    """Same collision, but src/ already has everything: sweeping is safe."""
    state = home / ".state"
    (home / "src" / "todo").mkdir()
    (home / "src" / "todo" / "BACKLOG.md").write_text("current")
    (home / "todo").mkdir()
    (home / "todo" / "BACKLOG.md").write_text("current")
    os.utime(home / "todo" / "BACKLOG.md", (OLD, OLD))
    _aged(home / "todo")

    moves, skipped = sweep(_manifest(home), state, NOW, dry_run=False)

    assert [Path(m.dst).name for m in moves] == ["todo"]
    assert not any(s.startswith("todo:") for s in skipped)
