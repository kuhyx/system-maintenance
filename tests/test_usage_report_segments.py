"""Tests for usage_report's per-day segment aggregation, the log-description
strings, and the deprecated _compute_window shim.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import _usage_report_run as run
from _usage_report_types import _Progress, _Window

if TYPE_CHECKING:
    import pytest


def _progress() -> _Progress:
    """A progress reporter that never draws, so stderr stays clean."""
    return _Progress(enabled=False, total_stages=1)


# --------------------------------------------------------------------------- #
# _compute_window (deprecated shim; no callers remain in-tree)
# --------------------------------------------------------------------------- #
def test_compute_window_returns_the_aggregate_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shim forwards to aggregate_atop and hands back its window."""
    window = _Window(distinct_samples=3, interval_s=600, seconds=1200)
    monkeypatch.setattr(run, "aggregate_atop", lambda *_a: ({}, window))

    result = run._compute_window(Path("/nonexistent/atop"), _progress())

    assert result is window
    assert result.seconds == 1200


def test_compute_window_defaults_an_empty_window_to_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window with no measured seconds is widened to a full day."""
    window = _Window(distinct_samples=1)
    monkeypatch.setattr(run, "aggregate_atop", lambda *_a: ({}, window))

    assert (
        run._compute_window(Path("/nonexistent/atop"), _progress()).seconds
        == run._SEC_PER_DAY
    )


# --------------------------------------------------------------------------- #
# _aggregate_segments / _log_descriptions
# --------------------------------------------------------------------------- #
def test_aggregate_segments_skips_missing_atop_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A gap day contributes no atop data but its pmon log is still read."""
    present = tmp_path / "atop-present"
    present.write_text("x", encoding="utf-8")
    segments = [
        run._Segment(present, tmp_path / "pmon-a"),
        run._Segment(tmp_path / "atop-absent", tmp_path / "pmon-b"),
    ]
    monkeypatch.setattr(
        run,
        "aggregate_atop",
        lambda *_a: ({}, _Window(distinct_samples=2, seconds=600)),
    )
    pmon_calls: list[Path] = []

    def fake_pmon(log: Path, *_a: object) -> tuple[dict[str, object], int]:
        pmon_calls.append(log)
        return {}, 5

    monkeypatch.setattr(run, "aggregate_pmon", fake_pmon)

    aggs = run._aggregate_segments(segments, _progress())

    assert aggs.days_with_data == 1
    assert aggs.gpu_samples == 10
    assert len(pmon_calls) == 2


def test_log_descriptions_names_only_existing_logs(tmp_path: Path) -> None:
    """Descriptions cover the logs that are actually on disk."""
    atop = tmp_path / "atop"
    atop.write_text("x", encoding="utf-8")
    segments = [run._Segment(atop, tmp_path / "pmon-absent")]

    atop_desc, pmon_desc = run._log_descriptions(segments)

    assert str(atop) in atop_desc
    assert "none" in pmon_desc.lower() or pmon_desc


def test_aggregate_segments_ignores_a_log_with_no_samples(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A present atop log that yields no samples does not count as a data day."""
    present = tmp_path / "atop-empty"
    present.write_text("x", encoding="utf-8")
    monkeypatch.setattr(run, "aggregate_atop", lambda *_a: ({}, _Window()))
    monkeypatch.setattr(run, "aggregate_pmon", lambda *_a: ({}, 0))

    aggs = run._aggregate_segments(
        [run._Segment(present, tmp_path / "pmon")],
        _progress(),
    )

    assert aggs.days_with_data == 0
