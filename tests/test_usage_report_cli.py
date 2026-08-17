"""Tests for usage_report's command-line surface: argument parsing, the
single-day and since-last-report entry points, and main's dispatch between
them.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import TYPE_CHECKING

from _usage_report_types import _Progress, _Window
import usage_report

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


def _progress() -> _Progress:
    """A progress reporter that never draws, so stderr stays clean."""
    return _Progress(enabled=False, total_stages=1)


def _args(**overrides: object) -> object:
    """Build a parsed-args namespace with the parser's own defaults."""
    args = usage_report._build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _aggregates(*, days_with_data: int = 1, seconds: int = 600) -> object:
    """Build an _Aggregates with a window that looks like real coverage."""
    return usage_report._Aggregates(
        cpu={},
        gpu={},
        window=_Window(distinct_samples=2, interval_s=600, seconds=seconds),
        gpu_samples=0,
        days_with_data=days_with_data,
    )


# --------------------------------------------------------------------------- #
# _build_parser
# --------------------------------------------------------------------------- #
def test_parser_defaults() -> None:
    """With no flags every option takes its documented default."""
    args = usage_report._build_parser().parse_args([])

    assert args.date is None
    assert args.since is None
    assert args.top == usage_report._DEFAULT_TOP
    assert args.atop_log is None
    assert args.pmon_log is None
    assert args.no_clipboard is False
    assert args.no_update_state is False
    assert args.quiet is False


def test_parser_reads_every_flag() -> None:
    """Each flag lands on the namespace with the right type."""
    args = usage_report._build_parser().parse_args(
        [
            "--date",
            "20260817",
            "--since",
            "20260801",
            "--top",
            "20",
            "--atop-log",
            "/nonexistent/a",
            "--pmon-log",
            "/nonexistent/p",
            "--no-clipboard",
            "--no-update-state",
            "--quiet",
        ],
    )

    assert args.date == "20260817"
    assert args.since == "20260801"
    assert args.top == 20
    assert args.atop_log == Path("/nonexistent/a")
    assert args.pmon_log == Path("/nonexistent/p")
    assert args.no_clipboard is True
    assert args.no_update_state is True
    assert args.quiet is True


# --------------------------------------------------------------------------- #
# main dispatch
# --------------------------------------------------------------------------- #
def test_main_routes_single_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """A --date run goes to the single-day path."""
    seen: list[str] = []

    def record(label: str) -> Callable[..., int]:
        def run(*_a: object) -> int:
            seen.append(label)
            return 0

        return run

    monkeypatch.setattr(usage_report, "_run_single_day", record("single"))
    monkeypatch.setattr(usage_report, "_run_since", record("since"))

    assert usage_report.main(["--date", "20260817"]) == 0
    assert seen == ["single"]


def test_main_routes_since_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no day-selecting flag the since-last-report path runs."""
    seen: list[str] = []

    def record(label: str) -> Callable[..., int]:
        def run(*_a: object) -> int:
            seen.append(label)
            return 0

        return run

    monkeypatch.setattr(usage_report, "_run_single_day", record("single"))
    monkeypatch.setattr(usage_report, "_run_since", record("since"))

    assert usage_report.main([]) == 0
    assert seen == ["since"]


# --------------------------------------------------------------------------- #
# _run_single_day
# --------------------------------------------------------------------------- #
def test_run_single_day_emits_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-day path renders once and writes the result out."""
    monkeypatch.setattr(usage_report, "_preflight", lambda _log: None)
    monkeypatch.setattr(
        usage_report,
        "_resolve_logs",
        lambda _date: (Path("/nonexistent/atop"), Path("/nonexistent/pmon")),
    )
    monkeypatch.setattr(usage_report, "_aggregate_segments", lambda *_a: _aggregates())
    monkeypatch.setattr(usage_report, "_log_descriptions", lambda _s: ("a", "p"))
    monkeypatch.setattr(
        usage_report, "_render_report", lambda *_a, **k: k["period_line"]
    )
    emitted: list[str] = []
    monkeypatch.setattr(usage_report, "_emit", lambda _args, r: emitted.append(r))

    rc = usage_report._run_single_day(
        _args(date="20260817", quiet=True),
        _dt.datetime.now().astimezone(),
    )

    assert rc == 0
    assert emitted == ["- **Reporting period**: 20260817 (single day)"]


def test_run_single_day_defaults_an_empty_window_to_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window with no measured seconds is treated as a full day."""
    aggs = _aggregates(seconds=0)
    monkeypatch.setattr(usage_report, "_preflight", lambda _log: None)
    monkeypatch.setattr(
        usage_report,
        "_resolve_logs",
        lambda _date: (Path("/nonexistent/atop"), Path("/nonexistent/pmon")),
    )
    monkeypatch.setattr(usage_report, "_aggregate_segments", lambda *_a: aggs)
    monkeypatch.setattr(usage_report, "_log_descriptions", lambda _s: ("a", "p"))
    monkeypatch.setattr(usage_report, "_render_report", lambda *_a, **_k: "report")
    monkeypatch.setattr(usage_report, "_emit", lambda *_a: None)

    usage_report._run_single_day(
        _args(date="20260817", quiet=True),
        _dt.datetime.now().astimezone(),
    )

    assert aggs.window.seconds == usage_report._SEC_PER_DAY
