"""Tests for usage_report's CPU and RAM Markdown tables, including the
peak-RSS bucket dedupe that collapses thread rows onto their parent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import _usage_report_tables as tables
from _usage_report_types import _HZ, ProcAgg

if TYPE_CHECKING:
    import pytest


def _proc(
    name: str,
    *,
    cpu_ticks: int = 0,
    peak_rss_kb: int = 0,
    pid_set: set[int] | None = None,
) -> ProcAgg:
    """Build a ProcAgg with only the fields these table tests care about."""
    return ProcAgg(
        name,
        cpu_ticks=cpu_ticks,
        peak_rss_kb=peak_rss_kb,
        pid_set=set() if pid_set is None else pid_set,
    )


def _cells(row: str) -> list[str]:
    """Split a Markdown table row into stripped cell values."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


# --------------------------------------------------------------------------- #
# _cpu_table
# --------------------------------------------------------------------------- #
def test_cpu_table_sorts_by_ticks_and_honours_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows are ordered by CPU ticks descending and truncated to *top*."""
    monkeypatch.setattr(tables.os, "cpu_count", lambda: 4)
    aggs = [
        _proc("low", cpu_ticks=100),
        _proc("high", cpu_ticks=900),
        _proc("mid", cpu_ticks=500),
    ]

    rows = tables._cpu_table(aggs, window_s=60, top=2)

    assert len(rows) == 4  # header + separator + 2 data rows
    assert _cells(rows[2])[1] == "high"
    assert _cells(rows[3])[1] == "mid"


def test_cpu_table_divides_box_percentage_by_cpu_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Avg CPU% (of box)' is the single-core percentage divided by ncpu."""
    monkeypatch.setattr(tables.os, "cpu_count", lambda: 4)
    # ProcAgg.cpu_seconds divides ticks by the real clock tick rate, so derive
    # the tick count from it: one full core busy for the whole 60 s window.
    aggs = [_proc("busy", cpu_ticks=60 * _HZ)]

    rows = tables._cpu_table(aggs, window_s=60, top=1)

    cells = _cells(rows[2])
    assert cells[3] == "100.0%"
    assert cells[4] == "25.0%"


def test_cpu_table_zero_window_yields_zero_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-length window cannot be divided by, so percentages are 0."""
    monkeypatch.setattr(tables.os, "cpu_count", lambda: 2)
    rows = tables._cpu_table([_proc("x", cpu_ticks=500)], window_s=0, top=1)

    cells = _cells(rows[2])
    assert cells[3] == "0.0%"
    assert cells[4] == "0.0%"


def test_cpu_table_falls_back_to_one_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """When os.cpu_count() returns None the box percentage assumes one core."""
    monkeypatch.setattr(tables.os, "cpu_count", lambda: None)
    rows = tables._cpu_table([_proc("x", cpu_ticks=100)], window_s=60, top=1)

    cells = _cells(rows[2])
    assert cells[3] == cells[4]


def test_cpu_table_escapes_program_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pipe in a program name cannot break out of its table cell."""
    monkeypatch.setattr(tables.os, "cpu_count", lambda: 1)
    rows = tables._cpu_table([_proc("a|b", cpu_ticks=1)], window_s=60, top=1)

    assert r"a\|b" in rows[2]


def test_cpu_table_empty_input_is_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No aggregates means header and separator with no data rows."""
    monkeypatch.setattr(tables.os, "cpu_count", lambda: 1)
    assert len(tables._cpu_table([], window_s=60, top=5)) == 2


# --------------------------------------------------------------------------- #
# _dedupe_ram
# --------------------------------------------------------------------------- #
def test_dedupe_ram_groups_by_bucket_and_keeps_top_cpu() -> None:
    """Same-RSS rows collapse to the highest-CPU representative."""
    same_rss = 4096
    rows = tables._dedupe_ram(
        [
            _proc("worker", peak_rss_kb=same_rss, cpu_ticks=10),
            _proc("parent", peak_rss_kb=same_rss, cpu_ticks=99),
            _proc("other", peak_rss_kb=same_rss, cpu_ticks=5),
        ],
    )

    assert len(rows) == 1
    rep, siblings = rows[0]
    assert rep.name == "parent"
    assert sorted(siblings) == ["other", "worker"]


def test_dedupe_ram_skips_non_positive_rss() -> None:
    """Rows with no measured RSS are dropped entirely."""
    assert tables._dedupe_ram([_proc("ghost", peak_rss_kb=0)]) == []


def test_dedupe_ram_orders_buckets_by_rss_descending() -> None:
    """Distinct RSS buckets come back largest-first."""
    rows = tables._dedupe_ram(
        [
            _proc("small", peak_rss_kb=1024),
            _proc("large", peak_rss_kb=10 * 1024),
        ],
    )

    assert [rep.name for rep, _ in rows] == ["large", "small"]


def test_dedupe_ram_breaks_cpu_ties_on_pid_count() -> None:
    """Equal CPU ticks fall back to the higher PID count for the rep."""
    rows = tables._dedupe_ram(
        [
            _proc("one_pid", peak_rss_kb=2048, cpu_ticks=7, pid_set={1}),
            _proc("many_pids", peak_rss_kb=2048, cpu_ticks=7, pid_set={1, 2, 3}),
        ],
    )

    assert rows[0][0].name == "many_pids"


# --------------------------------------------------------------------------- #
# _ram_table
# --------------------------------------------------------------------------- #
def test_ram_table_shows_dash_when_no_siblings() -> None:
    """A lone row in its bucket renders an em dash in the sibling column."""
    rows = tables._ram_table([_proc("solo", peak_rss_kb=2048)], top=5)

    assert _cells(rows[2])[-1] == "—"


def test_ram_table_lists_siblings_without_overflow_note() -> None:
    """Up to the display cap, every sibling name is listed and no '+N more'."""
    aggs = [_proc("rep", peak_rss_kb=4096, cpu_ticks=100)]
    aggs += [_proc(f"s{i}", peak_rss_kb=4096, cpu_ticks=1) for i in range(3)]

    sib = _cells(tables._ram_table(aggs, top=5)[2])[-1]

    assert "more" not in sib
    assert sorted(sib.split(", ")) == ["s0", "s1", "s2"]


def test_ram_table_truncates_sibling_list_with_count() -> None:
    """Beyond the cap, extra siblings collapse into a '(+N more)' suffix."""
    over = tables._MAX_SIBLINGS_SHOWN + 2
    aggs = [_proc("rep", peak_rss_kb=4096, cpu_ticks=100)]
    aggs += [_proc(f"s{i}", peak_rss_kb=4096, cpu_ticks=1) for i in range(over)]

    sib = _cells(tables._ram_table(aggs, top=5)[2])[-1]

    assert sib.endswith(f"(+{over - tables._MAX_SIBLINGS_SHOWN} more)")
    # Exactly _MAX_SIBLINGS_SHOWN names are listed, joined by ", ".
    assert sib.count(",") == tables._MAX_SIBLINGS_SHOWN - 1


def test_ram_table_escapes_sibling_and_rep_names() -> None:
    """Pipes are escaped in both the representative and sibling columns."""
    aggs = [
        _proc("re|p", peak_rss_kb=4096, cpu_ticks=100),
        _proc("si|b", peak_rss_kb=4096, cpu_ticks=1),
    ]

    row = tables._ram_table(aggs, top=5)[2]

    assert r"re\|p" in row
    assert r"si\|b" in row


def test_ram_table_honours_top_limit() -> None:
    """Only *top* buckets are rendered."""
    aggs = [_proc(f"p{i}", peak_rss_kb=(i + 1) * 4096) for i in range(5)]

    assert len(tables._ram_table(aggs, top=2)) == 4


def test_ram_table_empty_input_is_header_only() -> None:
    """No aggregates means header and separator only."""
    assert len(tables._ram_table([], top=5)) == 2
