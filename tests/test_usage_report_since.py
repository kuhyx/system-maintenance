"""Tests for the "since last report" multi-day aggregation in usage_report.

Covers the helpers added to span and merge several daily logs: aggregate
merging, window merging, PID-count carry-over, pmon timestamp filtering,
atop command bounding, the persisted last-report state, day-segment planning,
and the run-mode dispatch logic.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING

import _usage_report_parsing as parsing
from _usage_report_types import GpuAgg, ProcAgg, _PidCpu, _Progress, _Window
import usage_report

if TYPE_CHECKING:
    import pytest

# Aware timezone matching how the parser localizes naive timestamps, so epochs
# computed here line up with `_pmon_row_epoch`'s `.astimezone()` conversion.
_LOCAL_TZ = _dt.datetime.now().astimezone().tzinfo


def _at(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0
) -> _dt.datetime:
    """Build an aware local datetime for tests."""
    return _dt.datetime(year, month, day, hour, minute, tzinfo=_LOCAL_TZ)


# --------------------------------------------------------------------------- #
# PID-count carry-over (types)
# --------------------------------------------------------------------------- #
def test_proc_pid_count_combines_set_and_extra() -> None:
    """`pid_count` adds the live set length and merged-in extras."""
    agg = ProcAgg("x", pid_set={1, 2, 3}, extra_pids=2)

    assert agg.pid_count == 5


def test_gpu_pid_count_combines_set_and_extra() -> None:
    """GpuAgg exposes the same combined PID count."""
    agg = GpuAgg("x", pid_set={9}, extra_pids=4)

    assert agg.pid_count == 5


# --------------------------------------------------------------------------- #
# Aggregate merging (parsing)
# --------------------------------------------------------------------------- #
def test_merge_proc_aggs_sums_and_takes_peak() -> None:
    """CPU/RSS sums accumulate, peak RSS is the max, PID counts add."""
    dst: dict[str, ProcAgg] = {}
    parsing.merge_proc_aggs(
        dst,
        {
            "a": ProcAgg(
                "a",
                cpu_ticks=100,
                peak_rss_kb=200,
                rss_kb_sum=50,
                rss_samples=2,
                pid_set={1, 2},
            )
        },
    )
    parsing.merge_proc_aggs(
        dst,
        {
            "a": ProcAgg(
                "a",
                cpu_ticks=10,
                peak_rss_kb=500,
                rss_kb_sum=5,
                rss_samples=1,
                pid_set={3},
            )
        },
    )

    entry = dst["a"]
    assert entry.cpu_ticks == 110
    assert entry.peak_rss_kb == 500
    assert entry.rss_kb_sum == 55
    assert entry.rss_samples == 3
    assert entry.pid_count == 3


def test_merge_gpu_aggs_sums_and_takes_peak() -> None:
    """GPU sample sums accumulate and peaks take the max across days."""
    dst: dict[str, GpuAgg] = {}
    parsing.merge_gpu_aggs(
        dst,
        {
            "g": GpuAgg(
                "g",
                sm_pct_sum=30.0,
                mem_pct_sum=10.0,
                samples=3,
                peak_sm_pct=40.0,
                peak_mem_pct=20.0,
                pid_set={1},
            )
        },
    )
    parsing.merge_gpu_aggs(
        dst,
        {
            "g": GpuAgg(
                "g",
                sm_pct_sum=5.0,
                mem_pct_sum=2.0,
                samples=1,
                peak_sm_pct=80.0,
                peak_mem_pct=15.0,
                pid_set={2, 3},
            )
        },
    )

    entry = dst["g"]
    assert entry.sm_pct_sum == 35.0
    assert entry.samples == 4
    assert entry.peak_sm_pct == 80.0
    assert entry.peak_mem_pct == 20.0
    assert entry.pid_count == 3


# --------------------------------------------------------------------------- #
# Window merging (parsing)
# --------------------------------------------------------------------------- #
def test_merge_windows_empty_returns_default() -> None:
    """Merging no real windows yields the empty default window."""
    assert parsing.merge_windows([]).distinct_samples == 0
    assert parsing.merge_windows([_Window()]).distinct_samples == 0


def test_merge_windows_spans_and_sums() -> None:
    """Span uses min start / max end; samples and seconds sum; interval is modal."""
    w_empty = _Window()  # distinct_samples == 0, must be ignored
    w1 = _Window(
        start="s1",
        end="e1",
        distinct_samples=5,
        interval_s=600,
        seconds=100,
        start_epoch=1000,
        end_epoch=2000,
    )
    w2 = _Window(
        start="s2",
        end="e2",
        distinct_samples=3,
        interval_s=600,
        seconds=50,
        start_epoch=500,
        end_epoch=3000,
    )

    merged = parsing.merge_windows([w_empty, w1, w2])

    assert merged.start == "s2"  # earliest start_epoch (500)
    assert merged.end == "e2"  # latest end_epoch (3000)
    assert merged.distinct_samples == 8
    assert merged.seconds == 150
    assert merged.interval_s == 600


# --------------------------------------------------------------------------- #
# pmon timestamp helpers (parsing)
# --------------------------------------------------------------------------- #
def test_pmon_row_epoch_parses_valid_row() -> None:
    """A well-formed pmon row yields the matching local epoch."""
    row = ["20260604", "10:30:00", "0", "100", "G", "5", "1"]

    assert parsing._pmon_row_epoch(row) == _at(2026, 6, 4, 10, 30).timestamp()


def test_pmon_row_epoch_returns_none_on_bad_input() -> None:
    """Malformed or short rows return None rather than raising."""
    assert parsing._pmon_row_epoch([]) is None
    assert parsing._pmon_row_epoch(["nope", "alsonope"]) is None


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

    _, samples = parsing.aggregate_pmon(log, _Progress(enabled=False, total_stages=1))

    assert samples == 2


def test_aggregate_pmon_filters_rows_before_begin(tmp_path: Path) -> None:
    """Rows timestamped before begin_epoch are skipped."""
    log = tmp_path / "pmon.log"
    _write_pmon(log)
    cutoff = _at(2026, 6, 4, 10, 30).timestamp()

    agg, samples = parsing.aggregate_pmon(
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
    cmd = parsing._atop_read_cmd(
        Path("/var/log/atop/atop_20260604"), "PRC,PRM", None, None
    )

    assert cmd == ["atop", "-r", "/var/log/atop/atop_20260604", "-P", "PRC,PRM"]


def test_atop_read_cmd_with_begin_and_end() -> None:
    """Begin/end inject -b/-e before the -P selector."""
    cmd = parsing._atop_read_cmd(Path("/x"), "PRC", "202606041400", "202606042000")

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
    monkeypatch.setattr(usage_report, "_STATE_DIR", state.parent)
    monkeypatch.setattr(usage_report, "_STATE_FILE", state)
    when = _at(2026, 6, 2, 9, 0)

    usage_report._write_last_generated(when)

    assert usage_report._read_last_generated() == when


def test_state_missing_file_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No state file yet means "unknown", so the caller falls back to today."""
    monkeypatch.setattr(usage_report, "_STATE_FILE", tmp_path / "absent.json")

    assert usage_report._read_last_generated() is None


def test_state_corrupt_file_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupt or partial JSON is treated as unknown, not a crash."""
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(usage_report, "_STATE_FILE", bad)
    assert usage_report._read_last_generated() is None

    bad.write_text("{}", encoding="utf-8")  # valid JSON, missing key
    assert usage_report._read_last_generated() is None


# --------------------------------------------------------------------------- #
# Day-segment planning (usage_report)
# --------------------------------------------------------------------------- #
def test_has_time_of_day() -> None:
    """Midnight needs no begin bound; any later time does."""
    assert usage_report._has_time_of_day(_at(2026, 6, 4, 14, 30)) is True
    assert usage_report._has_time_of_day(_at(2026, 6, 4, 0, 0)) is False


def test_plan_segments_single_day_midnight_unbounded() -> None:
    """A start at local midnight covers the whole first day (no -b bound)."""
    segments = usage_report._plan_segments(_at(2026, 6, 4), _at(2026, 6, 4, 12))

    assert len(segments) == 1
    assert segments[0].atop_begin is None
    assert segments[0].pmon_begin_epoch is None


def test_plan_segments_bounds_only_first_day() -> None:
    """A mid-day start bounds the first day only; later days are full."""
    start = _at(2026, 6, 2, 14, 0)
    segments = usage_report._plan_segments(start, _at(2026, 6, 4, 10, 0))

    assert len(segments) == 3
    assert segments[0].atop_begin == "20260602140000"
    assert segments[0].pmon_begin_epoch == start.timestamp()
    assert all(seg.atop_begin is None for seg in segments[1:])
    assert segments[-1].atop_log.name == "atop_20260604"


def test_plan_segments_start_after_end_is_empty() -> None:
    """A future state file (start past end) yields no segments."""
    assert usage_report._plan_segments(_at(2026, 6, 5), _at(2026, 6, 4)) == []


# --------------------------------------------------------------------------- #
# Start resolution and mode dispatch (usage_report)
# --------------------------------------------------------------------------- #
def _args(**overrides: object) -> argparse.Namespace:
    """Build a Namespace with the usage_report CLI defaults."""
    base: dict[str, object] = {
        "date": None,
        "since": None,
        "atop_log": None,
        "pmon_log": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_resolve_start_prefers_since(monkeypatch: pytest.MonkeyPatch) -> None:
    """--since wins over any saved state and starts at local midnight."""
    monkeypatch.setattr(usage_report, "_read_last_generated", lambda: _at(2026, 1, 1))
    start = usage_report._resolve_start(_args(since="20260604"), _at(2026, 6, 4, 12))

    assert start.date() == _dt.date(2026, 6, 4)
    assert (start.hour, start.minute) == (0, 0)


def test_resolve_start_uses_last_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --since, the saved last-report timestamp is the start."""
    last = _at(2026, 6, 2, 9, 0)
    monkeypatch.setattr(usage_report, "_read_last_generated", lambda: last)

    assert usage_report._resolve_start(_args(), _at(2026, 6, 4, 12)) == last


def test_resolve_start_first_run_is_today_midnight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-ever run (no state) covers today from local midnight."""
    monkeypatch.setattr(usage_report, "_read_last_generated", lambda: None)
    now = _at(2026, 6, 4, 12, 30)

    assert usage_report._resolve_start(_args(), now) == _at(2026, 6, 4, 0, 0)


def test_is_single_day_mode() -> None:
    """Pinning a date or explicit log path selects single-day mode."""
    assert usage_report._is_single_day_mode(_args(date="20260604")) is True
    assert usage_report._is_single_day_mode(_args(atop_log=Path("/x"))) is True
    assert usage_report._is_single_day_mode(_args(pmon_log=Path("/x"))) is True
    assert usage_report._is_single_day_mode(_args()) is False


def test_should_advance_state_only_for_default_run() -> None:
    """Only a plain since-last-report run re-baselines the saved timestamp."""
    assert usage_report._should_advance_state(_args(no_update_state=False)) is True
    assert usage_report._should_advance_state(_args(no_update_state=True)) is False
    # --since is an ad-hoc query and must never advance state.
    assert (
        usage_report._should_advance_state(
            _args(since="20260510", no_update_state=False),
        )
        is False
    )


# --------------------------------------------------------------------------- #
# Report fragments (usage_report)
# --------------------------------------------------------------------------- #
def test_period_line_contains_both_bounds() -> None:
    """The period bullet shows start, end, and the span."""
    line = usage_report._period_line(_at(2026, 6, 2, 9), _at(2026, 6, 4, 9))

    assert "2026-06-02T09:00:00" in line
    assert "2026-06-04T09:00:00" in line
    assert "→" in line


def test_describe_logs_counts() -> None:
    """Log description switches between none / single / multiple wording."""
    assert "none found" in usage_report._describe_logs([], "atop -r")
    assert usage_report._describe_logs(
        [Path("/var/log/atop/atop_20260604")], "atop -r"
    ).startswith(
        "`/var/log/atop/atop_20260604`",
    )
    many = usage_report._describe_logs(
        [Path("/v/atop_20260601"), Path("/v/atop_20260604")],
        "atop -r",
    )
    assert "2 daily logs" in many


# --------------------------------------------------------------------------- #
# PRC field parsing — HZ-field regression (parsing)
# --------------------------------------------------------------------------- #
def test_parse_prc_does_not_charge_hz_as_cpu() -> None:
    """atop emits `... pid (name) state HZ utime stime`; the HZ column must be
    skipped, never summed as CPU.

    Regression for the off-by-one that read HZ (100) as utime, which inflated
    every process's CPU-seconds to its record/PID count (xset showing 67h).
    """
    pid_cpu: dict[int, _PidCpu] = {}
    # 6 generic fields, pid, (name), state, HZ=100, utime=7, stime=3, + tail.
    line = "PRC host 1000 2026/06/04 12:00:00 600 4242 (xset) E 100 7 3 0 0 0"

    parsing._parse_prc(line.split(), pid_cpu)

    entry = pid_cpu[4242]
    assert entry.name == "xset"
    assert entry.delta_ticks == 10  # utime+stime, never the HZ constant (100)


def test_parse_prc_skips_hz_with_multiword_name() -> None:
    """The HZ skip stays aligned when the name spans several tokens."""
    pid_cpu: dict[int, _PidCpu] = {}
    line = "PRC h 1000 d t 600 99 (Web Content) S 100 40 2 0 0"

    parsing._parse_prc(line.split(), pid_cpu)

    assert pid_cpu[99].name == "Web Content"
    assert pid_cpu[99].delta_ticks == 42  # 40+2, HZ(100) skipped


def test_parse_prc_too_short_is_ignored() -> None:
    """A truncated PRC record (missing stime) is skipped, not a crash."""
    pid_cpu: dict[int, _PidCpu] = {}
    # Tokens run out at utime — no stime at after+3, so the record is dropped.
    line = "PRC h 1000 d t 600 7 (x) S 100 5"

    parsing._parse_prc(line.split(), pid_cpu)

    assert pid_cpu == {}


# --------------------------------------------------------------------------- #
# Native helper selection (parsing)
# --------------------------------------------------------------------------- #
def test_atop_agg_binary_missing_source_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted C source tree yields None (Python fallback) even when a cached
    binary exists — never trust an orphaned, unverifiable build."""
    monkeypatch.setattr(parsing, "_ATOP_AGG_SRC_DIR", tmp_path / "gone")
    cache = tmp_path / "atop_agg"
    cache.write_text("stale binary", encoding="utf-8")
    monkeypatch.setattr(parsing, "_ATOP_AGG_CACHE_BIN", cache)

    assert parsing._atop_agg_binary() is None
