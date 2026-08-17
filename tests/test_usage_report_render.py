"""Tests for usage_report report assembly: the methodology section and the
full Markdown report that stitches every section together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import _usage_report_render as render
from _usage_report_run import _Aggregates
from _usage_report_types import GpuAgg, ProcAgg, _Window

if TYPE_CHECKING:
    import pytest


# --------------------------------------------------------------------------- #
# _methodology_section
# --------------------------------------------------------------------------- #
def test_methodology_section_reports_observed_interval() -> None:
    """A known sample interval is stated as an observed value."""
    window = _Window(interval_s=10, seconds=3600)

    lines = render._methodology_section("atop.log", "pmon.log", window)

    assert "- **atop sample interval (observed)**: 10s" in lines
    assert "- **atop log(s)**: atop.log" in lines
    assert "- **pmon log(s)**: pmon.log" in lines


def test_methodology_section_flags_unknown_interval() -> None:
    """A zero interval means only one sample, and says so instead of '0s'."""
    lines = render._methodology_section("a", "p", _Window(interval_s=0))

    assert any("interval unknown" in line for line in lines)
    assert not any("(observed)" in line for line in lines)


def test_methodology_section_states_coverage_window_in_human_units() -> None:
    """The coverage window is formatted with _fmt_h, not raw seconds."""
    lines = render._methodology_section("a", "p", _Window(interval_s=10, seconds=5000))

    assert any("1h 23m" in line for line in lines)


# --------------------------------------------------------------------------- #
# _render_report
# --------------------------------------------------------------------------- #
def _stub_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the host section so reports do not depend on the real machine."""
    monkeypatch.setattr(render, "_host_profile", lambda: {"hostname": "box"})
    monkeypatch.setattr(render.os, "cpu_count", lambda: 4)


def _aggregates(
    *,
    cpu: dict[str, ProcAgg] | None = None,
    gpu: dict[str, GpuAgg] | None = None,
    window: _Window | None = None,
    gpu_samples: int = 0,
) -> _Aggregates:
    """Build an _Aggregates with only the fields a given test cares about."""
    return _Aggregates(
        cpu={} if cpu is None else cpu,
        gpu={} if gpu is None else gpu,
        window=_Window() if window is None else window,
        gpu_samples=gpu_samples,
        days_with_data=1,
    )


def test_render_report_includes_every_section(monkeypatch: pytest.MonkeyPatch) -> None:
    """The assembled report carries all five headings and the LLM prompt."""
    _stub_sections(monkeypatch)
    aggs = _aggregates(
        cpu={"firefox": ProcAgg("firefox", cpu_ticks=1000, peak_rss_kb=4096)},
        gpu={"game": GpuAgg("game", sm_pct_sum=50.0, samples=5)},
        window=_Window(
            start="A",
            end="B",
            distinct_samples=7,
            interval_s=10,
            seconds=60,
        ),
        gpu_samples=5,
    )

    out = render._render_report(
        aggs,
        top=5,
        atop_desc="atop.log",
        pmon_desc="pmon.log",
        period_line="- **Period**: today",
    )

    assert out.startswith("# System resource usage report\n")
    for heading in (
        "## Host",
        "## Methodology",
        "## Top CPU consumers",
        "## Top RAM consumers",
        "## Top GPU consumers",
        "## Suggested LLM prompt",
    ):
        assert heading in out
    assert "- **Period**: today" in out
    assert "- **atop window**: A → B" in out
    assert "7 distinct" in out
    assert "firefox" in out
    assert "game" in out
    assert out.endswith("\n")


def test_render_report_notes_absent_gpu_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no GPU aggregates the GPU section is a placeholder, not a table."""
    _stub_sections(monkeypatch)

    out = render._render_report(
        _aggregates(window=_Window(interval_s=10, seconds=60)),
        top=5,
        atop_desc="a",
        pmon_desc="p",
        period_line="- **Period**: x",
    )

    assert "_No GPU pmon data found._" in out
    # The phrase "GPU SM-seconds" also appears in the methodology prose, so
    # assert on the table header cell instead to prove no table was emitted.
    assert "| GPU SM-seconds |" not in out


def test_render_report_marks_single_sample_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero interval renders as 'n/a (single sample)' in the header block."""
    _stub_sections(monkeypatch)

    out = render._render_report(
        _aggregates(window=_Window(interval_s=0)),
        top=5,
        atop_desc="a",
        pmon_desc="p",
        period_line="- **Period**: x",
    )

    assert "sample interval ≈ n/a (single sample)" in out
