"""The two reporting runs usage_report dispatches between.

`_run_single_day` covers one calendar day ad hoc; `_run_since` spans every
daily log from the last report to now and advances the saved timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as _dt
import shutil
import sys
from typing import TYPE_CHECKING

from _usage_report_emit import _INSTALL_SCRIPT, _emit, _preflight
from _usage_report_format import _fmt_h
from _usage_report_logs import (
    _log_descriptions,
    _plan_segments,
    _read_last_generated,
    _resolve_logs,
    _Segment,
    _write_last_generated,
)
from _usage_report_parsing import (
    aggregate_atop,
    merge_gpu_aggs,
    merge_proc_aggs,
    merge_windows,
)
from _usage_report_pmon import aggregate_pmon
from _usage_report_render import _render_report
from _usage_report_types import GpuAgg, ProcAgg, _Progress, _Window

_SEC_PER_DAY = 86_400

if TYPE_CHECKING:
    import argparse
    from pathlib import Path


def _compute_window(atop_log: Path, progress: _Progress) -> _Window:
    """Deprecated helper kept for backwards import compatibility.

    New code should call :func:`aggregate_atop`, which returns the window
    alongside the per-process aggregates from a single atop subprocess.
    """
    _, window = aggregate_atop(atop_log, progress)
    if not window.seconds:
        window.seconds = _SEC_PER_DAY
    return window


_REPORT_STAGES = 2


@dataclass
class _Aggregates:
    """Merged CPU/GPU aggregates and coverage window for a reporting window.

    *days_with_data* is the number of daily logs that actually yielded atop
    samples (gap days where the machine was off contribute nothing).
    """

    cpu: dict[str, ProcAgg]
    gpu: dict[str, GpuAgg]
    window: _Window
    gpu_samples: int
    days_with_data: int


def _aggregate_segments(
    segments: list[_Segment],
    progress: _Progress,
) -> _Aggregates:
    """Aggregate and merge every existing daily log in *segments*.

    Missing daily logs (gap days) are skipped silently.
    """
    cpu_total: dict[str, ProcAgg] = {}
    gpu_total: dict[str, GpuAgg] = {}
    windows: list[_Window] = []
    gpu_samples = 0
    days_with_data = 0
    for seg in segments:
        if seg.atop_log.exists():
            cpu, window = aggregate_atop(seg.atop_log, progress, seg.atop_begin)
            merge_proc_aggs(cpu_total, cpu)
            if window.distinct_samples:
                windows.append(window)
                days_with_data += 1
        gpu, samples = aggregate_pmon(seg.pmon_log, progress, seg.pmon_begin_epoch)
        merge_gpu_aggs(gpu_total, gpu)
        gpu_samples += samples
    return _Aggregates(
        cpu_total,
        gpu_total,
        merge_windows(windows),
        gpu_samples,
        days_with_data,
    )


def _period_line(start: _dt.datetime, end: _dt.datetime) -> str:
    """Markdown bullet describing the requested reporting period."""
    span = _fmt_h(max((end - start).total_seconds(), 0.0))
    return (
        f"- **Reporting period**: {start.isoformat(timespec='seconds')} → "
        f"{end.isoformat(timespec='seconds')} ({span})"
    )


def _should_advance_state(args: argparse.Namespace) -> bool:
    """Advance the saved timestamp only for genuine since-last-report runs.

    An explicit ``--since`` is treated as a read-only ad-hoc query (like
    ``--date``) so "let me look from date X" never silently re-baselines the
    saved tracking point.
    """
    return args.since is None and not args.no_update_state


def _run_single_day(args: argparse.Namespace, now: _dt.datetime) -> int:
    """Report on one specific day (legacy behaviour); never touches state."""
    date = args.date or now.strftime("%Y%m%d")
    atop_default, pmon_default = _resolve_logs(date)
    atop_log = args.atop_log or atop_default
    pmon_log = args.pmon_log or pmon_default
    _preflight(atop_log)
    segment = _Segment(atop_log, pmon_log)
    progress = _Progress(enabled=not args.quiet, total_stages=_REPORT_STAGES)
    aggs = _aggregate_segments([segment], progress)
    progress.finish()
    if not aggs.window.seconds:
        aggs.window.seconds = _SEC_PER_DAY
    atop_desc, pmon_desc = _log_descriptions([segment])
    _emit(
        args,
        _render_report(
            aggs,
            top=args.top,
            atop_desc=atop_desc,
            pmon_desc=pmon_desc,
            period_line=f"- **Reporting period**: {date} (single day)",
        ),
    )
    return 0


def _resolve_start(args: argparse.Namespace, now: _dt.datetime) -> _dt.datetime:
    """Pick the window start: --since, else last report, else today midnight."""
    if args.since is not None:
        return _dt.datetime.strptime(args.since, "%Y%m%d").astimezone()
    last = _read_last_generated()
    if last is not None:
        return last
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _run_since(args: argparse.Namespace, now: _dt.datetime) -> int:
    """Report on everything since the last run, spanning multiple daily logs."""
    if not shutil.which("atop"):
        sys.exit(f"error: `atop` is not installed.\nrun: {_INSTALL_SCRIPT}")
    start = _resolve_start(args, now)
    segments = _plan_segments(start, now)
    progress = _Progress(
        enabled=not args.quiet,
        total_stages=max(2 * len(segments), 1),
    )
    aggs = _aggregate_segments(segments, progress)
    progress.finish()
    if aggs.days_with_data == 0:
        sys.stderr.write(
            f"no atop logs with data for {start.date()} … {now.date()}; "
            "nothing to report.\n",
        )
        if _should_advance_state(args):
            _write_last_generated(now)
        return 0
    if not aggs.window.seconds:
        aggs.window.seconds = _SEC_PER_DAY
    atop_desc, pmon_desc = _log_descriptions(segments)
    _emit(
        args,
        _render_report(
            aggs,
            top=args.top,
            atop_desc=atop_desc,
            pmon_desc=pmon_desc,
            period_line=_period_line(start, now),
        ),
    )
    if _should_advance_state(args):
        _write_last_generated(now)
    return 0
