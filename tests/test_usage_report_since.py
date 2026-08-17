"""Tests for spanning several days in usage_report: pmon timestamp filtering,
atop command bounding, the persisted last-report state, and day-segment
planning.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING

import _usage_report_atop as atop_io
import _usage_report_logs as logs
import _usage_report_pmon as pmon
from _usage_report_types import _Progress

if TYPE_CHECKING:
    import pytest


# Aware timezone matching how the parser localizes naive timestamps, so
# epochs computed here line up with `_pmon_row_epoch`'s `.astimezone()`.
_LOCAL_TZ = _dt.datetime.now().astimezone().tzinfo


def _at(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0
) -> _dt.datetime:
    """Build an aware local datetime for tests."""
    return _dt.datetime(year, month, day, hour, minute, tzinfo=_LOCAL_TZ)


# --------------------------------------------------------------------------- #
# pmon timestamp helpers (parsing)
# --------------------------------------------------------------------------- #
def test_pmon_row_epoch_parses_valid_row() -> None:
    """A well-formed pmon row yields the matching local epoch."""
    row = ["20260604", "10:30:00", "0", "100", "G", "5", "1"]

    assert pmon._pmon_row_epoch(row) == _at(2026, 6, 4, 10, 30).timestamp()


def test_pmon_row_epoch_returns_none_on_bad_input() -> None:
    """Malformed or short rows return None rather than raising."""
    assert pmon._pmon_row_epoch([]) is None
    assert pmon._pmon_row_epoch(["nope", "alsonope"]) is None


def _write_pmon(path: Path) -> None:
    """Write a tiny pmon log with two rows ten minutes apart."""
    path.write_text(
        "#Date Time gpu pid type sm mem enc dec jpg ofa command\n"
        " 20260604 10:00:00 0 100 G 5 1 - - - - Xorg\n"
        " 20260604 11:00:00 0 101 G 7 2 - - - - thorium\n",
        encoding="utf-8",
    )


def test_aggregate_pmon_without_bound_keeps_all_rows(tmp_path: Path) -> None:
    """No begin_epoch means every data row counts."""
    log = tmp_path / "pmon.log"
    _write_pmon(log)

    _, samples = pmon.aggregate_pmon(log, _Progress(enabled=False, total_stages=1))

    assert samples == 2


def test_aggregate_pmon_filters_rows_before_begin(tmp_path: Path) -> None:
    """Rows timestamped before begin_epoch are skipped."""
    log = tmp_path / "pmon.log"
    _write_pmon(log)
    cutoff = _at(2026, 6, 4, 10, 30).timestamp()

    agg, samples = pmon.aggregate_pmon(
        log,
        _Progress(enabled=False, total_stages=1),
        begin_epoch=cutoff,
    )

    assert samples == 1
    assert "thorium" in agg
    assert "Xorg" not in agg


# --------------------------------------------------------------------------- #
# atop command bounding (parsing)
# --------------------------------------------------------------------------- #
def test_atop_read_cmd_unbounded() -> None:
    """Without bounds the command is a plain replay."""
    cmd = atop_io._atop_read_cmd(
        Path("/var/log/atop/atop_20260604"), "PRC,PRM", None, None
    )

    assert cmd == ["atop", "-r", "/var/log/atop/atop_20260604", "-P", "PRC,PRM"]


def test_atop_read_cmd_with_begin_and_end() -> None:
    """Begin/end inject -b/-e before the -P selector."""
    cmd = atop_io._atop_read_cmd(Path("/x"), "PRC", "202606041400", "202606042000")

    assert cmd == [
        "atop",
        "-r",
        "/x",
        "-b",
        "202606041400",
        "-e",
        "202606042000",
        "-P",
        "PRC",
    ]


# --------------------------------------------------------------------------- #
# Persisted last-report state (usage_report)
# --------------------------------------------------------------------------- #
def test_state_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A written timestamp reads back as an equal aware datetime."""
    state = tmp_path / "state" / "last_report.json"
    monkeypatch.setattr(logs, "_STATE_DIR", state.parent)
    monkeypatch.setattr(logs, "_STATE_FILE", state)
    when = _at(2026, 6, 2, 9, 0)

    logs._write_last_generated(when)

    assert logs._read_last_generated() == when


def test_state_missing_file_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No state file yet means "unknown", so the caller falls back to today."""
    monkeypatch.setattr(logs, "_STATE_FILE", tmp_path / "absent.json")

    assert logs._read_last_generated() is None


def test_state_corrupt_file_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt or partial JSON is treated as unknown, not a crash."""
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(logs, "_STATE_FILE", bad)
    assert logs._read_last_generated() is None

    bad.write_text("{}", encoding="utf-8")  # valid JSON, missing key
    assert logs._read_last_generated() is None


# --------------------------------------------------------------------------- #
# Day-segment planning (usage_report)
# --------------------------------------------------------------------------- #
def test_has_time_of_day() -> None:
    """Midnight needs no begin bound; any later time does."""
    assert logs._has_time_of_day(_at(2026, 6, 4, 14, 30)) is True
    assert logs._has_time_of_day(_at(2026, 6, 4, 0, 0)) is False


def test_plan_segments_single_day_midnight_unbounded() -> None:
    """A start at local midnight covers the whole first day (no -b bound)."""
    segments = logs._plan_segments(_at(2026, 6, 4), _at(2026, 6, 4, 12))

    assert len(segments) == 1
    assert segments[0].atop_begin is None
    assert segments[0].pmon_begin_epoch is None


def test_plan_segments_bounds_only_first_day() -> None:
    """A mid-day start bounds the first day only; later days are full."""
    start = _at(2026, 6, 2, 14, 0)
    segments = logs._plan_segments(start, _at(2026, 6, 4, 10, 0))

    assert len(segments) == 3
    assert segments[0].atop_begin == "20260602140000"
    assert segments[0].pmon_begin_epoch == start.timestamp()
    assert all(seg.atop_begin is None for seg in segments[1:])
    assert segments[-1].atop_log.name == "atop_20260604"


def test_plan_segments_start_after_end_is_empty() -> None:
    """A future state file (start past end) yields no segments."""
    assert logs._plan_segments(_at(2026, 6, 5), _at(2026, 6, 4)) == []
