"""Tests for usage_report's atop subprocess plumbing and the native atop_agg
helper: command construction, line streaming, binary build/cache decisions and
the TSV rows the C helper emits.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

import _usage_report_parsing as parsing
from _usage_report_types import ProcAgg, _Progress
from typing_extensions import Self

if TYPE_CHECKING:
    import pytest


def _progress() -> _Progress:
    """A progress reporter that never draws, so stderr stays clean."""
    return _Progress(enabled=False, total_stages=1)


# --------------------------------------------------------------------------- #
# _run
# --------------------------------------------------------------------------- #
def test_run_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful command's stdout is returned verbatim."""
    monkeypatch.setattr(
        parsing.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, stdout="out", stderr=""),
    )

    assert parsing._run(["true"]) == "out"


def test_run_swallows_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing binary yields an empty string rather than raising."""

    def boom(*_a: object, **_k: object) -> None:
        msg = "no such binary"
        raise OSError(msg)

    monkeypatch.setattr(parsing.subprocess, "run", boom)

    assert parsing._run(["nope"]) == ""


def test_run_swallows_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command that overruns its timeout also yields an empty string."""

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="slow", timeout=60)

    monkeypatch.setattr(parsing.subprocess, "run", boom)

    assert parsing._run(["slow"]) == ""


# --------------------------------------------------------------------------- #
# _iter_atop_lines
# --------------------------------------------------------------------------- #
class _FakePopen:
    """Minimal Popen stand-in yielding canned stdout lines."""

    def __init__(self, lines: list[str] | None) -> None:
        self.stdout = lines
        self.stdin = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        """Never suppresses an exception."""


def test_iter_atop_lines_strips_newlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each streamed line comes back without its trailing newline."""
    monkeypatch.setattr(
        parsing.subprocess,
        "Popen",
        lambda *_a, **_k: _FakePopen(["a\n", "b\n"]),
    )

    assert list(parsing._iter_atop_lines(Path("log"), "PRC")) == ["a", "b"]


def test_iter_atop_lines_handles_no_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process with no stdout pipe yields nothing instead of raising."""
    monkeypatch.setattr(
        parsing.subprocess,
        "Popen",
        lambda *_a, **_k: _FakePopen(None),
    )

    assert list(parsing._iter_atop_lines(Path("log"), "PRC")) == []


# --------------------------------------------------------------------------- #
# _atop_agg_binary
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# _apply_native_name / _window_from_native
# --------------------------------------------------------------------------- #
def test_apply_native_name_fills_every_field() -> None:
    """An N row populates the ProcAgg, with pid_set sized to the PID count."""
    agg: dict[str, ProcAgg] = {}

    parsing._apply_native_name(
        ["N", "firefox", "1200", "8192", "4096", "10", "3"],
        agg,
    )

    entry = agg["firefox"]
    assert entry.cpu_ticks == 1200
    assert entry.peak_rss_kb == 8192
    assert entry.rss_kb_sum == 4096
    assert entry.rss_samples == 10
    assert entry.pid_count == 3


def test_window_from_native_zero_samples_is_empty() -> None:
    """A W row reporting no epochs yields the default empty window."""
    window = parsing._window_from_native(["W", "0", "0", "0", "0"])

    assert window.distinct_samples == 0
    assert window.start == "n/a"


def test_window_from_native_spans_the_reported_epochs() -> None:
    """A populated W row carries its bounds, count and interval through."""
    window = parsing._window_from_native(["W", "1000", "4600", "7", "600"])

    assert window.distinct_samples == 7
    assert window.interval_s == 600
    assert window.seconds == 3600
    assert window.start_epoch == 1000
    assert window.end_epoch == 4600


# --------------------------------------------------------------------------- #
# _aggregate_atop_native
# --------------------------------------------------------------------------- #
class _ClosableLines(list[str]):
    """A line list that also answers .close(), as a real pipe would."""

    def close(self) -> None:
        """Called on the producer's stdout by the pipeline code."""


class _FakePipeline:
    """Two-process context manager mimicking the atop | atop_agg pipeline."""

    def __init__(self, lines: list[str] | None) -> None:
        self.stdout = None if lines is None else _ClosableLines(lines)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        """Never suppresses an exception."""


def _fake_popen_factory(
    rows: list[str] | None,
) -> object:
    """Build a Popen replacement: first call is atop, second is atop_agg."""
    calls: list[int] = []

    def fake_popen(*_a: object, **_k: object) -> _FakePipeline:
        calls.append(1)
        # The first Popen is atop (its stdout is only closed by the caller);
        # the second is atop_agg, whose stdout the loop reads.
        return _FakePipeline(["x"] if len(calls) == 1 else rows)

    return fake_popen


def test_aggregate_atop_native_reads_n_and_w_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N rows become aggregates and a W row becomes the window."""
    rows = [
        "N\tfirefox\t900\t4096\t2048\t5\t2\n",
        "W\t100\t3700\t6\t600\n",
    ]
    monkeypatch.setattr(parsing.subprocess, "Popen", _fake_popen_factory(rows))

    agg, window = parsing._aggregate_atop_native(
        Path("log"),
        _progress(),
        Path("/bin/atop_agg"),
    )

    assert agg["firefox"].cpu_ticks == 900
    assert window.distinct_samples == 6
    assert window.interval_s == 600


def test_aggregate_atop_native_ignores_malformed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows with the wrong field count are skipped, not fatal."""
    rows = ["N\ttoo\tshort\n", "W\tbad\n", "Z\tunknown\ttag\n"]
    monkeypatch.setattr(parsing.subprocess, "Popen", _fake_popen_factory(rows))

    agg, window = parsing._aggregate_atop_native(
        Path("log"),
        _progress(),
        Path("/bin/atop_agg"),
    )

    assert agg == {}
    assert window.distinct_samples == 0


def test_aggregate_atop_native_handles_no_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A helper with no stdout pipe returns empty results rather than raising."""
    monkeypatch.setattr(parsing.subprocess, "Popen", _fake_popen_factory(None))

    agg, window = parsing._aggregate_atop_native(
        Path("log"),
        _progress(),
        Path("/bin/atop_agg"),
    )

    assert agg == {}
    assert window.distinct_samples == 0


def test_aggregate_atop_native_tolerates_producer_without_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When atop exposes no stdout there is nothing to close; the read proceeds."""
    calls: list[int] = []

    def fake_popen(*_a: object, **_k: object) -> _FakePipeline:
        calls.append(1)
        # First Popen is atop: give it no stdout, so the close() is skipped.
        return _FakePipeline(None if len(calls) == 1 else ["W\t0\t0\t0\t0\n"])

    monkeypatch.setattr(parsing.subprocess, "Popen", fake_popen)

    agg, window = parsing._aggregate_atop_native(
        Path("log"),
        _progress(),
        Path("/bin/atop_agg"),
    )

    assert agg == {}
    assert window.distinct_samples == 0
