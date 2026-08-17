"""Tests for usage_report's pure-Python atop record parsing: process-name
extraction, PRM folding, epoch windows, per-PID collapsing, and the
aggregate_atop fallback used when the native helper is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import _usage_report_parsing as parsing
from _usage_report_types import _PidCpu, _PidRam, _Progress

if TYPE_CHECKING:
    import pytest


def _progress() -> _Progress:
    """A progress reporter that never draws, so stderr stays clean."""
    return _Progress(enabled=False, total_stages=1)


# --------------------------------------------------------------------------- #
# _parse_name
# --------------------------------------------------------------------------- #
def test_parse_name_reads_single_token() -> None:
    """The common `(name)` case returns the bare name and the next index."""
    assert parsing._parse_name(["x", "(firefox)"], 1) == ("firefox", 2)


def test_parse_name_rejoins_a_name_with_spaces() -> None:
    """A parenthesised name split across tokens is rejoined."""
    parts = ["x", "(Draw", "GameThread)", "S"]

    assert parsing._parse_name(parts, 1) == ("Draw GameThread", 3)


def test_parse_name_past_end_is_unknown() -> None:
    """An index beyond the record yields the unknown placeholder."""
    assert parsing._parse_name(["x"], 5) == ("unknown", 6)


def test_parse_name_empty_parens_is_unknown() -> None:
    """`()` carries no name, so the placeholder is used."""
    assert parsing._parse_name(["x", "()"], 1) == ("unknown", 2)


def test_parse_name_unparenthesised_token_passes_through() -> None:
    """A token with no leading paren is taken as the name verbatim."""
    assert parsing._parse_name(["x", "bash"], 1) == ("bash", 2)


def test_parse_name_unterminated_parens_consumes_the_rest() -> None:
    """A name whose closing paren never arrives stops at the record end."""
    name, nxt = parsing._parse_name(["x", "(never", "closed"], 1)

    assert name == "never close"
    assert nxt == 3


# --------------------------------------------------------------------------- #
# _parse_prm
# --------------------------------------------------------------------------- #
def _prm(pid: str, name: str, rsize: str) -> list[str]:
    """Build a PRM record: 6 header fields, then pid name state pagesz vsize rsize."""
    return [
        "PRM",
        "host",
        "1000",
        "d",
        "t",
        "600",
        pid,
        name,
        "S",
        "4096",
        "9999",
        rsize,
    ]


def test_parse_prm_records_rss() -> None:
    """A well-formed PRM row lands in the per-PID RSS map."""
    ram: dict[int, _PidRam] = {}

    parsing._parse_prm(_prm("42", "(firefox)", "8192"), ram)

    assert ram[42].name == "firefox"
    assert ram[42].peak_kb == 8192


def test_parse_prm_non_numeric_pid_is_ignored() -> None:
    """A row whose PID will not parse is dropped rather than raising."""
    ram: dict[int, _PidRam] = {}

    parsing._parse_prm(_prm("notapid", "(x)", "1"), ram)

    assert ram == {}


def test_parse_prm_truncated_row_is_ignored() -> None:
    """A row that ends before the RSS column is dropped."""
    ram: dict[int, _PidRam] = {}

    parsing._parse_prm(["PRM", "host", "1000", "d", "t", "600", "42", "(x)"], ram)

    assert ram == {}


# --------------------------------------------------------------------------- #
# _window_from_epochs
# --------------------------------------------------------------------------- #
def test_window_from_epochs_empty_is_default() -> None:
    """No samples yields the placeholder window."""
    window = parsing._window_from_epochs(set())

    assert window.distinct_samples == 0
    assert window.start == "n/a"


def test_window_from_epochs_single_sample_has_no_interval() -> None:
    """One sample cannot imply an interval, so it stays zero."""
    window = parsing._window_from_epochs({1000})

    assert window.distinct_samples == 1
    assert window.interval_s == 0
    assert window.seconds == 0


def test_window_from_epochs_uses_median_interval() -> None:
    """The interval is the median gap, so one long gap does not skew it."""
    window = parsing._window_from_epochs({0, 600, 1200, 6000})

    assert window.distinct_samples == 4
    assert window.interval_s == 600
    assert window.seconds == 6000
    assert window.start_epoch == 0
    assert window.end_epoch == 6000


# --------------------------------------------------------------------------- #
# _fold_pid_aggregates
# --------------------------------------------------------------------------- #
def test_fold_pid_aggregates_sums_cpu_across_pids() -> None:
    """Two PIDs of one program contribute their deltas to one entry."""
    cpu = {1: _PidCpu(), 2: _PidCpu()}
    cpu[1].observe("python", 100)
    cpu[1].observe("python", 400)
    cpu[2].observe("python", 50)

    agg = parsing._fold_pid_aggregates(cpu, {})

    # PID 1 seen twice contributes last-first = 300; PID 2 seen once gives 50.
    assert agg["python"].cpu_ticks == 350
    assert agg["python"].pid_count == 2


def test_fold_pid_aggregates_takes_peak_and_counts_rss_samples() -> None:
    """Peak RSS is the max across PIDs and each PID adds one RSS sample."""
    ram = {1: _PidRam(), 2: _PidRam()}
    ram[1].observe("firefox", 4096)
    ram[2].observe("firefox", 8192)

    agg = parsing._fold_pid_aggregates({}, ram)

    assert agg["firefox"].peak_rss_kb == 8192
    assert agg["firefox"].rss_samples == 2
    assert agg["firefox"].pid_count == 2


# --------------------------------------------------------------------------- #
# aggregate_atop (pure-Python fallback)
# --------------------------------------------------------------------------- #
def test_aggregate_atop_parses_prc_and_prm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no native helper, the Python parser folds PRC and PRM records."""
    log = tmp_path / "atop.log"
    log.write_bytes(b"x" * 100)
    lines = [
        "# a comment",
        "RESET",
        "SEP",
        "",
        "PRC host 1000 d t 600 42 (python) S 100 500 20 a b c",
        "PRC host 1600 d t 600 42 (python) S 100 900 40 a b c",
        "PRM host 1000 d t 600 42 (python) S 4096 9999 2048 a b c",
        "PRC short row",
    ]
    monkeypatch.setattr(parsing, "_atop_agg_binary", lambda: None)
    monkeypatch.setattr(parsing, "_iter_atop_lines", lambda *_a, **_k: iter(lines))

    agg, window = parsing.aggregate_atop(log, _progress())

    # utime+stime deltas: (900+40) - (500+20) = 420.
    assert agg["python"].cpu_ticks == 420
    assert agg["python"].peak_rss_kb == 2048
    assert window.distinct_samples == 2
    assert window.interval_s == 600


def test_aggregate_atop_ignores_unparseable_epoch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A PRC row with a non-numeric epoch still parses, minus the timestamp."""
    log = tmp_path / "atop.log"
    log.write_bytes(b"x" * 10)
    lines = ["PRC host notanepoch d t 600 42 (python) S 100 500 20 a b c"]
    monkeypatch.setattr(parsing, "_atop_agg_binary", lambda: None)
    monkeypatch.setattr(parsing, "_iter_atop_lines", lambda *_a, **_k: iter(lines))

    agg, window = parsing.aggregate_atop(log, _progress())

    assert agg["python"].pid_count == 1
    assert window.distinct_samples == 0


def test_aggregate_atop_delegates_to_native_when_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the C helper builds, the native path is used instead."""
    log = tmp_path / "atop.log"
    log.write_bytes(b"x")
    monkeypatch.setattr(parsing, "_atop_agg_binary", lambda: Path("/bin/atop_agg"))
    called: list[Path] = []

    def fake_native(
        log_arg: Path,
        _progress_arg: _Progress,
        binary: Path,
        _begin: str | None = None,
        _end: str | None = None,
    ) -> tuple[dict[str, object], object]:
        called.append(binary)
        return {}, parsing._Window()

    monkeypatch.setattr(parsing, "_aggregate_atop_native", fake_native)

    parsing.aggregate_atop(log, _progress())

    assert called == [Path("/bin/atop_agg")]


def test_parse_prc_non_numeric_pid_is_ignored() -> None:
    """A PRC row whose PID will not parse is dropped rather than raising."""
    cpu: dict[int, _PidCpu] = {}

    parsing._parse_prc(
        ["PRC", "host", "1000", "d", "t", "600", "notapid", "(x)", "S", "1", "2", "3"],
        cpu,
    )

    assert cpu == {}


def test_aggregate_atop_skips_whitespace_only_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A row of only whitespace splits to nothing and is skipped."""
    log = tmp_path / "atop.log"
    log.write_bytes(b"x")
    monkeypatch.setattr(parsing, "_atop_agg_binary", lambda: None)
    monkeypatch.setattr(parsing, "_iter_atop_lines", lambda *_a, **_k: iter(["   "]))

    agg, window = parsing.aggregate_atop(log, _progress())

    assert agg == {}
    assert window.distinct_samples == 0
