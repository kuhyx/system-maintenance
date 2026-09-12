"""The resurrection freshness comparison that makes the sweep fail closed.

The integration behaviour lives in ``test_home_tidy_sweep_manifest.py``;
these cover the helper's own edges, including the bounded-walk refusal that
``sweep`` itself cannot reach (``bounded_stats`` counts directories as well
as files, so it always trips its shared limit first -- the guard here is the
backstop for a future caller that passes a tighter bound).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from _home_tidy_freshness import counterpart_for, newer_than_counterpart
from _home_tidy_manifest import load_manifest
from _home_tidy_scan import Entry, kind_of
from _home_tidy_sweep import _freshness_block
from home_tidy_fixture import MANIFEST

OLD = 1_000_000
NEW = 2_000_000


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "home-tidy.toml").write_text(MANIFEST)
    for b in ("src", "vendor", "inbox"):
        (tmp_path / b).mkdir()
    return tmp_path


def _entry(home: Path, name: str) -> Entry:
    path = home / name
    return Entry(name, path, kind_of(path), path.lstat().st_mtime)


def test_counterpart_for_searches_buckets_in_order(home: Path) -> None:
    (home / "vendor" / "clone").mkdir()
    assert (
        counterpart_for(home, "clone", ("src", "vendor")) == home / "vendor" / "clone"
    )
    assert counterpart_for(home, "nothing", ("src", "vendor")) is None


def test_no_counterpart_is_not_a_block(home: Path) -> None:
    """A plain stray has nothing to be compared against; ordinary sweep applies."""
    (home / "stray").mkdir()
    (home / "stray" / "f").write_text("x")
    assert newer_than_counterpart(home / "stray", home / "src" / "stray", 100) == (
        None,
        False,
    )
    assert (
        _freshness_block(
            load_manifest(home / "home-tidy.toml", home), _entry(home, "stray"), 100
        )
        is None
    )


def test_missing_counterpart_file_blocks(home: Path) -> None:
    """A file with no opposite number is the only copy — never bury it."""
    (home / "src" / "todo").mkdir()
    (home / "todo").mkdir()
    (home / "todo" / "only-here.md").write_text("x")
    assert newer_than_counterpart(home / "todo", home / "src" / "todo", 100) == (
        "only-here.md",
        False,
    )


def test_nested_newer_file_is_named(home: Path) -> None:
    (home / "src" / "todo" / "sub").mkdir(parents=True)
    (home / "src" / "todo" / "sub" / "a.md").write_text("stale")
    os.utime(home / "src" / "todo" / "sub" / "a.md", (OLD, OLD))
    (home / "todo" / "sub").mkdir(parents=True)
    (home / "todo" / "sub" / "a.md").write_text("live")
    os.utime(home / "todo" / "sub" / "a.md", (NEW, NEW))
    assert newer_than_counterpart(home / "todo", home / "src" / "todo", 100) == (
        str(Path("sub") / "a.md"),
        False,
    )


def test_equal_mtime_within_tolerance_does_not_block(home: Path) -> None:
    """A timestamp-preserving copy lands a tick apart; only a real edit blocks."""
    (home / "src" / "todo").mkdir()
    (home / "src" / "todo" / "a.md").write_text("x")
    os.utime(home / "src" / "todo" / "a.md", (OLD, OLD))
    (home / "todo").mkdir()
    (home / "todo" / "a.md").write_text("x")
    os.utime(home / "todo" / "a.md", (OLD + 1, OLD + 1))
    assert newer_than_counterpart(home / "todo", home / "src" / "todo", 100) == (
        None,
        False,
    )


def test_file_entry_compared_directly(home: Path) -> None:
    """A resurrected *file* (not a directory) takes the non-walk path."""
    (home / "src" / "notes.md").write_text("stale")
    os.utime(home / "src" / "notes.md", (OLD, OLD))
    (home / "notes.md").write_text("live")
    os.utime(home / "notes.md", (NEW, NEW))
    assert newer_than_counterpart(
        home / "notes.md", home / "src" / "notes.md", 100
    ) == (
        "notes.md",
        False,
    )
    os.utime(home / "notes.md", (OLD, OLD))
    assert newer_than_counterpart(
        home / "notes.md", home / "src" / "notes.md", 100
    ) == (None, False)


def test_vanished_file_mid_walk_is_ignored(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file deleted between listing and stat holds nothing to preserve."""
    (home / "src" / "todo").mkdir()
    (home / "todo").mkdir()
    (home / "todo" / "gone.md").write_text("x")
    real = Path.lstat

    def flaky(self: Path):
        if self.name == "gone.md" and self.parent.name == "todo":
            raise OSError("vanished")
        return real(self)

    monkeypatch.setattr(Path, "lstat", flaky)
    assert newer_than_counterpart(home / "todo", home / "src" / "todo", 100) == (
        None,
        False,
    )


def test_bounded_walk_refuses_rather_than_passing(home: Path) -> None:
    """Cut the walk short and the answer is 'unknown', never 'safe'."""
    # Every file HAS a current counterpart, so the walk is only ever stopped
    # by the bound — otherwise a missing counterpart would answer first and
    # this would not test the limit at all.
    (home / "src" / "todo").mkdir()
    (home / "todo").mkdir()
    for i in range(6):
        (home / "todo" / f"f{i}").write_text("x")
        os.utime(home / "todo" / f"f{i}", (OLD, OLD))
        (home / "src" / "todo" / f"f{i}").write_text("x")
        os.utime(home / "src" / "todo" / f"f{i}", (NEW, NEW))
    assert newer_than_counterpart(home / "todo", home / "src" / "todo", 100) == (
        None,
        False,
    )
    assert newer_than_counterpart(home / "todo", home / "src" / "todo", 2) == (
        None,
        True,
    )

    m = load_manifest(home / "home-tidy.toml", home)
    reason = _freshness_block(m, _entry(home, "todo"), 2)
    assert reason is not None and "too large to compare against ~/src/todo" in reason
