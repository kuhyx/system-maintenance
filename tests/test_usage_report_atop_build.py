"""Tests for usage_report's native atop_agg helper: when the cached binary is
reused, when it is rebuilt, and every way the build can fail into the
pure-Python fallback.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import _usage_report_atop as parsing

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_atop_agg_binary_uses_fresh_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cached binary newer than the source is returned without rebuilding."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src_c = src_dir / "atop_agg.c"
    src_c.write_text("int main(void){return 0;}", encoding="utf-8")
    cache = tmp_path / "cached"
    cache.write_bytes(b"binary")
    monkeypatch.setattr(parsing, "_ATOP_AGG_SRC_DIR", src_dir)
    monkeypatch.setattr(parsing, "_ATOP_AGG_CACHE_BIN", cache)

    def fail(*_a: object, **_k: object) -> None:
        msg = "should not rebuild when the cache is fresh"
        raise AssertionError(msg)

    monkeypatch.setattr(parsing.subprocess, "run", fail)

    assert parsing._atop_agg_binary() == cache


def test_atop_agg_binary_none_without_compiler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With a stale cache and no `cc`, the caller falls back to Python."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "atop_agg.c").write_text("x", encoding="utf-8")
    monkeypatch.setattr(parsing, "_ATOP_AGG_SRC_DIR", src_dir)
    monkeypatch.setattr(parsing, "_ATOP_AGG_CACHE_BIN", tmp_path / "absent")
    monkeypatch.setattr(parsing.shutil, "which", lambda _name: None)

    assert parsing._atop_agg_binary() is None


def test_atop_agg_binary_none_when_build_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing `make` falls back rather than propagating the error."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "atop_agg.c").write_text("x", encoding="utf-8")
    monkeypatch.setattr(parsing, "_ATOP_AGG_SRC_DIR", src_dir)
    monkeypatch.setattr(parsing, "_ATOP_AGG_CACHE_BIN", tmp_path / "cache" / "bin")
    monkeypatch.setattr(parsing.shutil, "which", lambda _name: "/usr/bin/cc")

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(1, "make")

    monkeypatch.setattr(parsing.subprocess, "run", boom)

    assert parsing._atop_agg_binary() is None


def test_atop_agg_binary_none_when_make_produces_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A `make` that exits 0 without writing the binary still falls back."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "atop_agg.c").write_text("x", encoding="utf-8")
    monkeypatch.setattr(parsing, "_ATOP_AGG_SRC_DIR", src_dir)
    monkeypatch.setattr(parsing, "_ATOP_AGG_CACHE_BIN", tmp_path / "cache" / "bin")
    monkeypatch.setattr(parsing.shutil, "which", lambda _name: "/usr/bin/cc")
    monkeypatch.setattr(
        parsing.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0),
    )

    assert parsing._atop_agg_binary() is None


def test_atop_agg_binary_builds_and_caches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful build is copied into the cache and made executable."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "atop_agg.c").write_text("x", encoding="utf-8")
    cache = tmp_path / "cache" / "atop_agg"
    monkeypatch.setattr(parsing, "_ATOP_AGG_SRC_DIR", src_dir)
    monkeypatch.setattr(parsing, "_ATOP_AGG_CACHE_BIN", cache)
    monkeypatch.setattr(parsing.shutil, "which", lambda _name: "/usr/bin/cc")

    def fake_make(*_a: object, **_k: object) -> subprocess.CompletedProcess[bytes]:
        (src_dir / "atop_agg").write_bytes(b"ELF")
        return subprocess.CompletedProcess([], 0)

    monkeypatch.setattr(parsing.subprocess, "run", fake_make)

    result = parsing._atop_agg_binary()

    assert result == cache
    assert cache.read_bytes() == b"ELF"
    assert cache.stat().st_mode & 0o777 == parsing._ATOP_AGG_BIN_MODE


def test_atop_agg_binary_missing_source_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted C source tree yields None (Python fallback) even when a cached
    binary exists - never trust an orphaned, unverifiable build."""
    monkeypatch.setattr(parsing, "_ATOP_AGG_SRC_DIR", tmp_path / "gone")
    cache = tmp_path / "atop_agg"
    cache.write_text("stale binary", encoding="utf-8")
    monkeypatch.setattr(parsing, "_ATOP_AGG_CACHE_BIN", cache)

    assert parsing._atop_agg_binary() is None
