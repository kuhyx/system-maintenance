"""Tests for usage_report's atop subprocess plumbing and the native atop_agg
helper: command construction, line streaming, binary build/cache decisions and
the TSV rows the C helper emits.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import TYPE_CHECKING

import _usage_report_atop as parsing
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
