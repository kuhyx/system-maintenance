"""Tests for the `atop | atop_agg` pipeline: how usage_report reads the native
helper's TSV rows and copes with a producer or consumer that has no stdout.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Self

import _usage_report_atop as parsing
from _usage_report_types import _Progress

if TYPE_CHECKING:
    import pytest


def _progress() -> _Progress:
    """A progress reporter that never draws, so stderr stays clean."""
    return _Progress(enabled=False, total_stages=1)


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
