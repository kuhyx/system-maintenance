"""Tests for merging usage_report daily aggregates: process and GPU aggregate
merging, window merging, and PID-count carry-over across days.
"""

from __future__ import annotations

import _usage_report_parsing as parsing
from _usage_report_types import GpuAgg, ProcAgg, _Window


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
