"""Tests for usage_report's GPU Markdown table."""

from __future__ import annotations

import _usage_report_tables as tables
from _usage_report_types import GpuAgg


def _cells(row: str) -> list[str]:
    """Split a Markdown table row into stripped cell values."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def test_gpu_table_sorts_by_gpu_seconds_and_honours_top() -> None:
    """Rows are ordered by SM-seconds descending and truncated to *top*."""
    aggs = {
        "low": GpuAgg("low", sm_pct_sum=10.0, samples=1),
        "high": GpuAgg("high", sm_pct_sum=900.0, samples=1),
        "mid": GpuAgg("mid", sm_pct_sum=100.0, samples=1),
    }

    rows = tables._gpu_table(aggs, total_samples=10, top=2)

    assert len(rows) == 4
    assert _cells(rows[2])[1] == "high"
    assert _cells(rows[3])[1] == "mid"


def test_gpu_table_reports_sample_presence_percentage() -> None:
    """The Samples column shows the share of total samples the process was in."""
    aggs = {"g": GpuAgg("g", sm_pct_sum=50.0, samples=5)}

    cells = _cells(tables._gpu_table(aggs, total_samples=20, top=1)[2])

    assert cells[6] == "5 (25%)"


def test_gpu_table_zero_total_samples_yields_zero_presence() -> None:
    """A zero sample total cannot be divided by, so presence renders as 0%."""
    aggs = {"g": GpuAgg("g", sm_pct_sum=50.0, samples=5)}

    cells = _cells(tables._gpu_table(aggs, total_samples=0, top=1)[2])

    assert cells[6] == "5 (0%)"


def test_gpu_table_escapes_program_names() -> None:
    """A pipe in a GPU process name cannot break out of its cell."""
    aggs = {"a|b": GpuAgg("a|b", sm_pct_sum=1.0, samples=1)}

    assert r"a\|b" in tables._gpu_table(aggs, total_samples=1, top=1)[2]


def test_gpu_table_empty_input_is_header_only() -> None:
    """No GPU aggregates means header and separator only."""
    assert len(tables._gpu_table({}, total_samples=0, top=5)) == 2
