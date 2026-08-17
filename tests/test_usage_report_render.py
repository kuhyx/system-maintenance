"""Tests for usage_report Markdown rendering: duration/escaping helpers, the
CPU/RAM/GPU tables, the RSS-bucket dedupe, the host and methodology sections,
and full report assembly.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import _usage_report_render as render
from _usage_report_types import _HZ, GpuAgg, ProcAgg, _Window
import pytest
from usage_report import _Aggregates

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _proc(
    name: str,
    *,
    cpu_ticks: int = 0,
    peak_rss_kb: int = 0,
    rss_kb_sum: int = 0,
    rss_samples: int = 0,
    pid_set: set[int] | None = None,
) -> ProcAgg:
    """Build a ProcAgg with only the fields a given test cares about."""
    return ProcAgg(
        name,
        cpu_ticks=cpu_ticks,
        peak_rss_kb=peak_rss_kb,
        rss_kb_sum=rss_kb_sum,
        rss_samples=rss_samples,
        pid_set=set() if pid_set is None else pid_set,
    )


def _cells(row: str) -> list[str]:
    """Split a Markdown table row into stripped cell values."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


# --------------------------------------------------------------------------- #
# _fmt_h
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0.0s"),
        (8.34, "8.3s"),
        (59.9, "59.9s"),
        (60.0, "1m 00s"),
        (252.0, "4m 12s"),
        (3599.0, "59m 59s"),
        (3600.0, "1h 00m"),
        (5000.0, "1h 23m"),
    ],
)
def test_fmt_h_picks_unit_by_magnitude(seconds: float, expected: str) -> None:
    """Durations switch h/m/s at exactly 3600 and 60 seconds."""
    assert render._fmt_h(seconds) == expected


# --------------------------------------------------------------------------- #
# _md_escape
# --------------------------------------------------------------------------- #
def test_md_escape_escapes_pipe_and_flattens_newline() -> None:
    """Pipes are backslash-escaped and newlines become spaces."""
    assert render._md_escape("a|b\nc") == r"a\|b c"


def test_md_escape_leaves_plain_names_untouched() -> None:
    """A name with no table-breaking characters passes through unchanged."""
    assert render._md_escape("firefox") == "firefox"


# --------------------------------------------------------------------------- #
# _cpu_table
# --------------------------------------------------------------------------- #
def test_cpu_table_sorts_by_ticks_and_honours_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows are ordered by CPU ticks descending and truncated to *top*."""
    monkeypatch.setattr(render.os, "cpu_count", lambda: 4)
    aggs = [
        _proc("low", cpu_ticks=100),
        _proc("high", cpu_ticks=900),
        _proc("mid", cpu_ticks=500),
    ]

    rows = render._cpu_table(aggs, window_s=60, top=2)

    assert len(rows) == 4  # header + separator + 2 data rows
    assert _cells(rows[2])[1] == "high"
    assert _cells(rows[3])[1] == "mid"


def test_cpu_table_divides_box_percentage_by_cpu_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Avg CPU% (of box)' is the single-core percentage divided by ncpu."""
    monkeypatch.setattr(render.os, "cpu_count", lambda: 4)
    # ProcAgg.cpu_seconds divides ticks by the real clock tick rate, so derive
    # the tick count from it: one full core busy for the whole 60 s window.
    aggs = [_proc("busy", cpu_ticks=60 * _HZ)]

    rows = render._cpu_table(aggs, window_s=60, top=1)

    cells = _cells(rows[2])
    assert cells[3] == "100.0%"
    assert cells[4] == "25.0%"


def test_cpu_table_zero_window_yields_zero_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zero-length window cannot be divided by, so percentages are 0."""
    monkeypatch.setattr(render.os, "cpu_count", lambda: 2)
    rows = render._cpu_table([_proc("x", cpu_ticks=500)], window_s=0, top=1)

    cells = _cells(rows[2])
    assert cells[3] == "0.0%"
    assert cells[4] == "0.0%"


def test_cpu_table_falls_back_to_one_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """When os.cpu_count() returns None the box percentage assumes one core."""
    monkeypatch.setattr(render.os, "cpu_count", lambda: None)
    rows = render._cpu_table([_proc("x", cpu_ticks=100)], window_s=60, top=1)

    cells = _cells(rows[2])
    assert cells[3] == cells[4]


def test_cpu_table_escapes_program_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pipe in a program name cannot break out of its table cell."""
    monkeypatch.setattr(render.os, "cpu_count", lambda: 1)
    rows = render._cpu_table([_proc("a|b", cpu_ticks=1)], window_s=60, top=1)

    assert r"a\|b" in rows[2]


def test_cpu_table_empty_input_is_header_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No aggregates means header and separator with no data rows."""
    monkeypatch.setattr(render.os, "cpu_count", lambda: 1)
    assert len(render._cpu_table([], window_s=60, top=5)) == 2


# --------------------------------------------------------------------------- #
# _dedupe_ram
# --------------------------------------------------------------------------- #
def test_dedupe_ram_groups_by_bucket_and_keeps_top_cpu() -> None:
    """Same-RSS rows collapse to the highest-CPU representative."""
    same_rss = 4096
    rows = render._dedupe_ram(
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
    assert render._dedupe_ram([_proc("ghost", peak_rss_kb=0)]) == []


def test_dedupe_ram_orders_buckets_by_rss_descending() -> None:
    """Distinct RSS buckets come back largest-first."""
    rows = render._dedupe_ram(
        [
            _proc("small", peak_rss_kb=1024),
            _proc("large", peak_rss_kb=10 * 1024),
        ],
    )

    assert [rep.name for rep, _ in rows] == ["large", "small"]


def test_dedupe_ram_breaks_cpu_ties_on_pid_count() -> None:
    """Equal CPU ticks fall back to the higher PID count for the rep."""
    rows = render._dedupe_ram(
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
    rows = render._ram_table([_proc("solo", peak_rss_kb=2048)], top=5)

    assert _cells(rows[2])[-1] == "—"


def test_ram_table_lists_siblings_without_overflow_note() -> None:
    """Up to the display cap, every sibling name is listed and no '+N more'."""
    aggs = [_proc("rep", peak_rss_kb=4096, cpu_ticks=100)]
    aggs += [_proc(f"s{i}", peak_rss_kb=4096, cpu_ticks=1) for i in range(3)]

    sib = _cells(render._ram_table(aggs, top=5)[2])[-1]

    assert "more" not in sib
    assert sorted(sib.split(", ")) == ["s0", "s1", "s2"]


def test_ram_table_truncates_sibling_list_with_count() -> None:
    """Beyond the cap, extra siblings collapse into a '(+N more)' suffix."""
    over = render._MAX_SIBLINGS_SHOWN + 2
    aggs = [_proc("rep", peak_rss_kb=4096, cpu_ticks=100)]
    aggs += [_proc(f"s{i}", peak_rss_kb=4096, cpu_ticks=1) for i in range(over)]

    sib = _cells(render._ram_table(aggs, top=5)[2])[-1]

    assert sib.endswith(f"(+{over - render._MAX_SIBLINGS_SHOWN} more)")
    # Exactly _MAX_SIBLINGS_SHOWN names are listed, joined by ", ".
    assert sib.count(",") == render._MAX_SIBLINGS_SHOWN - 1


def test_ram_table_escapes_sibling_and_rep_names() -> None:
    """Pipes are escaped in both the representative and sibling columns."""
    aggs = [
        _proc("re|p", peak_rss_kb=4096, cpu_ticks=100),
        _proc("si|b", peak_rss_kb=4096, cpu_ticks=1),
    ]

    row = render._ram_table(aggs, top=5)[2]

    assert r"re\|p" in row
    assert r"si\|b" in row


def test_ram_table_honours_top_limit() -> None:
    """Only *top* buckets are rendered."""
    aggs = [_proc(f"p{i}", peak_rss_kb=(i + 1) * 4096) for i in range(5)]

    assert len(render._ram_table(aggs, top=2)) == 4


def test_ram_table_empty_input_is_header_only() -> None:
    """No aggregates means header and separator only."""
    assert len(render._ram_table([], top=5)) == 2


# --------------------------------------------------------------------------- #
# _gpu_table
# --------------------------------------------------------------------------- #
def test_gpu_table_sorts_by_gpu_seconds_and_honours_top() -> None:
    """Rows are ordered by SM-seconds descending and truncated to *top*."""
    aggs = {
        "low": GpuAgg("low", sm_pct_sum=10.0, samples=1),
        "high": GpuAgg("high", sm_pct_sum=900.0, samples=1),
        "mid": GpuAgg("mid", sm_pct_sum=100.0, samples=1),
    }

    rows = render._gpu_table(aggs, total_samples=10, top=2)

    assert len(rows) == 4
    assert _cells(rows[2])[1] == "high"
    assert _cells(rows[3])[1] == "mid"


def test_gpu_table_reports_sample_presence_percentage() -> None:
    """The Samples column shows the share of total samples the process was in."""
    aggs = {"g": GpuAgg("g", sm_pct_sum=50.0, samples=5)}

    cells = _cells(render._gpu_table(aggs, total_samples=20, top=1)[2])

    assert cells[6] == "5 (25%)"


def test_gpu_table_zero_total_samples_yields_zero_presence() -> None:
    """A zero sample total cannot be divided by, so presence renders as 0%."""
    aggs = {"g": GpuAgg("g", sm_pct_sum=50.0, samples=5)}

    cells = _cells(render._gpu_table(aggs, total_samples=0, top=1)[2])

    assert cells[6] == "5 (0%)"


def test_gpu_table_escapes_program_names() -> None:
    """A pipe in a GPU process name cannot break out of its cell."""
    aggs = {"a|b": GpuAgg("a|b", sm_pct_sum=1.0, samples=1)}

    assert r"a\|b" in render._gpu_table(aggs, total_samples=1, top=1)[2]


def test_gpu_table_empty_input_is_header_only() -> None:
    """No GPU aggregates means header and separator only."""
    assert len(render._gpu_table({}, total_samples=0, top=5)) == 2


# --------------------------------------------------------------------------- #
# _host_profile
# --------------------------------------------------------------------------- #
def _fake_proc_files(
    monkeypatch: pytest.MonkeyPatch,
    contents: dict[str, str | OSError],
) -> None:
    """Route Path.open for /proc/cpuinfo and /proc/meminfo to canned text.

    _host_profile only ever opens those two paths, so every caller supplies
    both and there is no real-filesystem fallback to keep here.
    """

    def fake_open(
        self: Path,
        *_args: object,
        **_kwargs: object,
    ) -> io.StringIO:
        value = contents[str(self)]
        if isinstance(value, OSError):
            raise value
        return io.StringIO(value)

    monkeypatch.setattr(render.Path, "open", fake_open)


def test_host_profile_reads_cpu_model_and_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU model and total memory are lifted out of the /proc files."""
    monkeypatch.setattr(render.platform, "node", lambda: "box")
    monkeypatch.setattr(render.platform, "release", lambda: "6.1.0")
    monkeypatch.setattr(render.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(
        monkeypatch,
        {
            "/proc/cpuinfo": "flags\t: x\nmodel name\t: Ryzen 9\nmodel name\t: other\n",
            "/proc/meminfo": "SwapTotal: 1 kB\nMemTotal:       32768000 kB\n",
        },
    )

    info = render._host_profile()

    assert info["hostname"] == "box"
    assert info["kernel"] == "6.1.0"
    assert info["cpus_online"] == "8"
    assert info["cpu_model"] == "Ryzen 9"
    assert info["memory_total_gib"] == "31.2"
    assert "gpu" not in info


def test_host_profile_includes_gpu_when_nvidia_smi_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi-line nvidia-smi output is flattened onto one line."""
    monkeypatch.setattr(render.platform, "node", lambda: "box")
    monkeypatch.setattr(render.platform, "release", lambda: "6.1.0")
    monkeypatch.setattr(render.os, "cpu_count", lambda: 1)
    monkeypatch.setattr(render, "_run", lambda _cmd: "GPU0, 8 GiB\nGPU1, 8 GiB\n")
    _fake_proc_files(monkeypatch, {"/proc/cpuinfo": "", "/proc/meminfo": ""})

    assert render._host_profile()["gpu"] == "GPU0, 8 GiB; GPU1, 8 GiB"


def test_host_profile_survives_unreadable_proc_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError on either /proc file leaves that key absent, not raising."""
    monkeypatch.setattr(render.platform, "node", lambda: "box")
    monkeypatch.setattr(render.platform, "release", lambda: "6.1.0")
    monkeypatch.setattr(render.os, "cpu_count", lambda: 1)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(
        monkeypatch,
        {
            "/proc/cpuinfo": OSError("nope"),
            "/proc/meminfo": OSError("nope"),
        },
    )

    info = render._host_profile()

    assert "cpu_model" not in info
    assert "memory_total_gib" not in info


def test_host_profile_survives_meminfo_without_digits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MemTotal line with no parseable number is skipped, not fatal."""
    monkeypatch.setattr(render.platform, "node", lambda: "box")
    monkeypatch.setattr(render.platform, "release", lambda: "6.1.0")
    monkeypatch.setattr(render.os, "cpu_count", lambda: 1)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(
        monkeypatch,
        {"/proc/cpuinfo": "", "/proc/meminfo": "MemTotal:       kB\n"},
    )

    assert "memory_total_gib" not in render._host_profile()


def test_host_profile_defaults_cpu_count_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """os.cpu_count() returning None renders as '0' rather than 'None'."""
    monkeypatch.setattr(render.platform, "node", lambda: "box")
    monkeypatch.setattr(render.platform, "release", lambda: "6.1.0")
    monkeypatch.setattr(render.os, "cpu_count", lambda: None)
    monkeypatch.setattr(render, "_run", lambda _cmd: "")
    _fake_proc_files(monkeypatch, {"/proc/cpuinfo": "", "/proc/meminfo": ""})

    assert render._host_profile()["cpus_online"] == "0"


# --------------------------------------------------------------------------- #
# _fingerprint_section
# --------------------------------------------------------------------------- #
def test_fingerprint_section_renders_each_fact_as_a_bullet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every host-profile key becomes one bold Markdown bullet."""
    monkeypatch.setattr(
        render, "_host_profile", lambda: {"hostname": "box", "kernel": "6"}
    )

    assert render._fingerprint_section() == [
        "## Host",
        "",
        "- **hostname**: box",
        "- **kernel**: 6",
        "",
    ]


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
        cpu={"firefox": _proc("firefox", cpu_ticks=1000, peak_rss_kb=4096)},
        gpu={"game": GpuAgg("game", sm_pct_sum=50.0, samples=5)},
        window=_Window(
            start="A", end="B", distinct_samples=7, interval_s=10, seconds=60
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
