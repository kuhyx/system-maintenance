"""Tests for aggregate_atop, usage_report's pure-Python fallback parser used
when the native atop_agg helper is unavailable.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import _usage_report_parsing as parsing
from _usage_report_types import _Progress

if TYPE_CHECKING:
    import pytest


def _progress() -> _Progress:
    """A progress reporter that never draws, so stderr stays clean."""
    return _Progress(enabled=False, total_stages=1)


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
