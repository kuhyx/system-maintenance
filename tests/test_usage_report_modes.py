"""Tests for usage_report run modes and field parsing: start resolution,
single-day vs multi-day dispatch, the report period fragments, the PRC
HZ-field regression, and native-helper selection.
"""

from __future__ import annotations

import argparse
import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING

import _usage_report_parsing as parsing
import usage_report

if TYPE_CHECKING:
    from _usage_report_types import _PidCpu
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
