"""Tests for usage_report's since-last-report run: the atop guard, the normal
report path, the no-data path, and when the saved timestamp advances.
"""

from __future__ import annotations

import datetime as _dt

from _usage_report_types import _Window
import pytest
import usage_report


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
# _run_since
# --------------------------------------------------------------------------- #
def test_run_since_exits_without_atop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The multi-day path also refuses to run without atop installed."""
    monkeypatch.setattr(usage_report.shutil, "which", lambda _n: None)

    with pytest.raises(SystemExit):
        usage_report._run_since(_args(), _dt.datetime.now().astimezone())


def test_run_since_reports_and_advances_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal run emits a report and moves the saved timestamp forward."""
    now = _dt.datetime.now().astimezone()
    monkeypatch.setattr(usage_report.shutil, "which", lambda _n: "/usr/bin/atop")
    monkeypatch.setattr(usage_report, "_resolve_start", lambda *_a: now)
    monkeypatch.setattr(usage_report, "_plan_segments", lambda *_a: [])
    monkeypatch.setattr(usage_report, "_aggregate_segments", lambda *_a: _aggregates())
    monkeypatch.setattr(usage_report, "_log_descriptions", lambda _s: ("a", "p"))
    monkeypatch.setattr(usage_report, "_render_report", lambda *_a, **_k: "report")
    emitted: list[str] = []
    monkeypatch.setattr(usage_report, "_emit", lambda _a, r: emitted.append(r))
    advanced: list[object] = []
    monkeypatch.setattr(usage_report, "_write_last_generated", advanced.append)

    rc = usage_report._run_since(_args(quiet=True), now)

    assert rc == 0
    assert emitted == ["report"]
    assert advanced == [now]


def test_run_since_with_no_data_still_advances_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gap days produce a stderr note, no report, but the state still moves."""
    now = _dt.datetime.now().astimezone()
    monkeypatch.setattr(usage_report.shutil, "which", lambda _n: "/usr/bin/atop")
    monkeypatch.setattr(usage_report, "_resolve_start", lambda *_a: now)
    monkeypatch.setattr(usage_report, "_plan_segments", lambda *_a: [])
    monkeypatch.setattr(
        usage_report,
        "_aggregate_segments",
        lambda *_a: _aggregates(days_with_data=0),
    )
    emitted: list[str] = []
    monkeypatch.setattr(usage_report, "_emit", lambda _a, r: emitted.append(r))
    advanced: list[object] = []
    monkeypatch.setattr(usage_report, "_write_last_generated", advanced.append)

    rc = usage_report._run_since(_args(quiet=True), now)

    assert rc == 0
    assert emitted == []
    assert advanced == [now]
    assert "nothing to report" in capsys.readouterr().err


def test_run_since_with_no_data_leaves_state_when_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-update-state keeps the saved timestamp even on the no-data path."""
    now = _dt.datetime.now().astimezone()
    monkeypatch.setattr(usage_report.shutil, "which", lambda _n: "/usr/bin/atop")
    monkeypatch.setattr(usage_report, "_resolve_start", lambda *_a: now)
    monkeypatch.setattr(usage_report, "_plan_segments", lambda *_a: [])
    monkeypatch.setattr(
        usage_report,
        "_aggregate_segments",
        lambda *_a: _aggregates(days_with_data=0),
    )
    advanced: list[object] = []
    monkeypatch.setattr(usage_report, "_write_last_generated", advanced.append)

    usage_report._run_since(_args(quiet=True, no_update_state=True), now)

    assert advanced == []


def test_run_since_defaults_an_empty_window_to_a_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window with no measured seconds is treated as a full day."""
    now = _dt.datetime.now().astimezone()
    aggs = _aggregates(seconds=0)
    monkeypatch.setattr(usage_report.shutil, "which", lambda _n: "/usr/bin/atop")
    monkeypatch.setattr(usage_report, "_resolve_start", lambda *_a: now)
    monkeypatch.setattr(usage_report, "_plan_segments", lambda *_a: [])
    monkeypatch.setattr(usage_report, "_aggregate_segments", lambda *_a: aggs)
    monkeypatch.setattr(usage_report, "_log_descriptions", lambda _s: ("a", "p"))
    monkeypatch.setattr(usage_report, "_render_report", lambda *_a, **_k: "report")
    monkeypatch.setattr(usage_report, "_emit", lambda *_a: None)
    monkeypatch.setattr(usage_report, "_write_last_generated", lambda *_a: None)

    usage_report._run_since(_args(quiet=True), now)

    assert aggs.window.seconds == usage_report._SEC_PER_DAY


def test_run_since_leaves_state_when_suppressed_after_a_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-update-state keeps the timestamp even when a report was emitted."""
    now = _dt.datetime.now().astimezone()
    monkeypatch.setattr(usage_report.shutil, "which", lambda _n: "/usr/bin/atop")
    monkeypatch.setattr(usage_report, "_resolve_start", lambda *_a: now)
    monkeypatch.setattr(usage_report, "_plan_segments", lambda *_a: [])
    monkeypatch.setattr(usage_report, "_aggregate_segments", lambda *_a: _aggregates())
    monkeypatch.setattr(usage_report, "_log_descriptions", lambda _s: ("a", "p"))
    monkeypatch.setattr(usage_report, "_render_report", lambda *_a, **_k: "report")
    emitted: list[str] = []
    monkeypatch.setattr(usage_report, "_emit", lambda _a, r: emitted.append(r))
    advanced: list[object] = []
    monkeypatch.setattr(usage_report, "_write_last_generated", advanced.append)

    usage_report._run_since(_args(quiet=True, no_update_state=True), now)

    assert emitted == ["report"]
    assert advanced == []
