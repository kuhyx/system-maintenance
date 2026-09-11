"""Scan helpers, the lint rules and the report renderings."""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import pytest
from home_tidy_fixture import MANIFEST

from _home_tidy_check import Violation, format_report, run_check, write_report
from _home_tidy_manifest import load_manifest, write_bridges
from _home_tidy_scan import bounded_stats, is_mountpoint_within, kind_of, list_root, visible_children


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home-tidy.toml").write_text(MANIFEST)
    for b in ("src", "vendor", "inbox", "Downloads"):
        (tmp_path / b).mkdir()
    return tmp_path


def test_kind_and_list_root(home: Path) -> None:
    (home / "src" / "repo").mkdir()
    (home / "src" / "repo" / ".git").mkdir()
    (home / "f.txt").write_text("x")
    os.symlink("f.txt", home / "ln")
    (home / ".hidden").mkdir()
    assert kind_of(home / "src" / "repo") == "git"
    assert kind_of(home / "src") == "dir"
    assert kind_of(home / "f.txt") == "file"
    assert kind_of(home / "ln") == "link"
    names = [e.name for e in list_root(home)]
    assert names == ["Downloads", "f.txt", "home-tidy.toml", "inbox", "ln", "src", "vendor"]
    assert visible_children(home / "src") == ["repo"] and visible_children(home / "missing") == []


def test_mountpoint(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "src" / "a" / "b").mkdir(parents=True)
    assert not is_mountpoint_within(home / "src")
    assert not is_mountpoint_within(home / "home-tidy.toml")
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p).endswith("/b"))
    assert is_mountpoint_within(home / "src")
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p).endswith("/src"))
    assert is_mountpoint_within(home / "src")


def test_bounded_stats(home: Path) -> None:
    d = home / "src" / "d"
    d.mkdir()
    for i in range(5):
        (d / f"{i}.txt").write_bytes(b"x" * 10)
    assert bounded_stats(d, 1000, 100) == (50, 6, False)
    assert bounded_stats(d, 25, 100)[2] is True
    assert bounded_stats(d, 1000, 3)[2] is True
    (d / "sub").mkdir()
    assert bounded_stats(d, 1000, 6)[2] is True  # directory count pushes it over
    assert bounded_stats(home / "home-tidy.toml", 1, 1)[2] is True
    os.symlink("nowhere", home / "src" / "ln")
    assert bounded_stats(home / "src" / "ln", 1, 1) == (0, 1, False)
    (d / "gone.txt").write_text("x")
    real_lstat = os.lstat

    def flaky(p):  # an entry vanishing mid-walk is skipped, not fatal
        if str(p).endswith("gone.txt"):
            raise OSError("gone")
        return real_lstat(p)

    import _home_tidy_scan

    _home_tidy_scan.os.lstat = flaky
    try:
        assert bounded_stats(d, 1000, 100)[0] == 50
    finally:
        _home_tidy_scan.os.lstat = real_lstat


def test_run_check_rules(home: Path) -> None:
    m = load_manifest(home / "home-tidy.toml", home)
    (home / "stray").mkdir()
    (home / "src" / "notgit").mkdir()
    (home / ".config").mkdir()
    (home / ".config" / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/"\nXDG_MUSIC_DIR="$HOME/m"\n')
    old = 1_000_000
    (home / "inbox" / "old.txt").write_text("")
    os.utime(home / "inbox" / "old.txt", (old, old))
    (home / "inbox" / "keep").mkdir()
    os.utime(home / "inbox" / "keep", (old, old))
    (home / "inbox" / "fresh.txt").write_text("")
    for i in range(4):
        (home / "Downloads" / str(i)).write_text("")
    os.symlink("src", home / "bridge-live")
    os.symlink("src", home / "bridge-old")
    write_bridges(home / "home-tidy.toml", "2030-01-01", ["bridge-live", "bridge-old", "bridge-gone"])
    m = load_manifest(home / "home-tidy.toml", home)
    rules = [(v.rule, v.path, v.warn) for v in run_check(m, now=_dt.datetime(2029, 1, 1).timestamp())]
    assert ("root-allow", "stray", False) in rules
    assert ("root-allow", "home-tidy.toml", False) in rules
    assert ("bridge", "bridge-live", True) in rules
    assert ("git-only", "src/notgit", False) in rules
    assert ("xdg-home", "XDG_DESKTOP_DIR", False) in rules and ("xdg-home", "XDG_MUSIC_DIR", False) not in rules
    assert ("inbox-stale", "inbox/old.txt", True) in rules and not any(p == "inbox/keep" for _, p, _ in rules)
    for i in range(4):
        (home / "src" / f"r{i}").mkdir()
    assert ("bucket-size", "Downloads", True) in rules and not any(r == "bucket-size" and p == "src" for r, p, _ in rules)
    rules = [(v.rule, v.path) for v in run_check(m, now=_dt.datetime(2031, 1, 1).timestamp())]
    assert ("bridge-expired", "bridge-live") in rules and ("bridge", "bridge-gone") not in rules
    assert run_check(m)  # default "now" path


def test_format_and_write_report(tmp_path: Path) -> None:
    assert format_report([]) == "" and format_report([], nag=True) == ""
    v = [
        Violation("root-allow", "x", "bad"),
        Violation("inbox-stale", "inbox/a", "3 days old", warn=True),
        Violation("inbox-stale", "inbox/b", "9 days old", warn=True),
        Violation("bucket-size", "src", "30 entries > 21", warn=True),
    ]
    full = format_report(v)
    assert full.startswith("home-tidy: 1 failure(s), 3 warning(s)\n[FAIL] root-allow: x — bad\n") and full.count("\n") == 5
    nag = format_report(v, nag=True)
    assert "[FAIL] root-allow: x" in nag and "inbox: 2 item(s)" in nag and "oldest inbox/b, 9 days old" in nag
    assert "1 more (bucket-size)" in nag and "inbox/a" not in nag
    assert format_report([v[0]], nag=True).count("\n") == 2
    out = write_report(tmp_path / "state", "text\n")
    assert out.read_text() == "text\n"
