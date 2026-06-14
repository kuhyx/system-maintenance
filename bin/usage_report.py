#!/usr/bin/env python3
"""Resource usage report from atop + nvidia-smi pmon logs.

Parses one or more daily `atop` binary logs via `atop -P PRC,PRM -r` and the
per-process nvidia-smi pmon logs, aggregates CPU seconds, peak/average RSS, and
GPU SM-% seconds per program, and prints a compact Markdown report intended to
be pasted into an LLM (Claude / Copilot) for further analysis.

Run with no arguments to report on **everything since the last report**: the
previous run's timestamp is persisted, and each run covers the whole window
from then until now, spanning as many daily logs as needed (so skipped days are
never lost). After a successful report the timestamp is advanced to "now".

    usage_report.py                       # since the last report (multi-day)
    usage_report.py --since 20260419      # ad hoc: from a date to now, no state
    usage_report.py --date 20260419       # one specific day (ad hoc, no state)
    usage_report.py --top 20              # keep 20 rows per table
    usage_report.py --no-update-state     # don't advance the saved timestamp
    usage_report.py > report.md           # redirect to a file

The output intentionally front-loads metadata (hostname, period, window, sample
count, HZ, machine specs) so the LLM never has to guess context.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as _dt
import json
from pathlib import Path
import shutil
import subprocess
import sys

from _usage_report_parsing import (
    aggregate_atop,
    merge_gpu_aggs,
    merge_proc_aggs,
    merge_windows,
)
from _usage_report_pmon import aggregate_pmon
from _usage_report_render import _fmt_h, _render_report
from _usage_report_types import _PMON_INTERVAL_S, GpuAgg, ProcAgg, _Progress, _Window

_ATOP_LOG_DIR = Path("/var/log/atop")
_PMON_LOG_DIR = Path.home() / ".local/share/gpu-log"
_DEFAULT_TOP = 15
_SEC_PER_DAY = 86_400

# Persisted marker of when the last report was generated. Lives under
# ~/.local/share (durable app state), not ~/.cache, so clearing caches does not
# silently reset the "since last report" window back to today-only.
_STATE_DIR = Path.home() / ".local/share/usage_report"
_STATE_FILE = _STATE_DIR / "last_report.json"


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
class _Segment:
    """One calendar day's resolved logs plus optional in-day start bounds.

    *atop_begin* is an atop ``-b`` argument (``YYYYMMDDhhmmss``) and
    *pmon_begin_epoch* the matching local epoch; both are set only for the first
    day of a "since last report" window so re-runs do not double-count.
    """

    atop_log: Path
    pmon_log: Path
    atop_begin: str | None = None
    pmon_begin_epoch: float | None = None


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


def _describe_logs(paths: list[Path], how: str) -> str:
    """One-line Markdown description of the log files actually consumed."""
    if not paths:
        return f"_none found_ (`{how}`)"
    if len(paths) == 1:
        return f"`{paths[0]}` (`{how}`)"
    return (
        f"{len(paths)} daily logs `{paths[0].name}` \u2026 `{paths[-1].name}` "
        f"in `{paths[0].parent}` (`{how}`)"
    )


def _log_descriptions(segments: list[_Segment]) -> tuple[str, str]:
    """Return ``(atop_desc, pmon_desc)`` for the logs present in *segments*."""
    atop_present = [seg.atop_log for seg in segments if seg.atop_log.exists()]
    pmon_present = [seg.pmon_log for seg in segments if seg.pmon_log.exists()]
    return (
        _describe_logs(atop_present, "atop -r"),
        _describe_logs(pmon_present, f"nvidia-smi pmon -d {_PMON_INTERVAL_S}"),
    )


def _resolve_logs(date: str) -> tuple[Path, Path]:
    atop_log = _ATOP_LOG_DIR / f"atop_{date}"
    pmon_log = _PMON_LOG_DIR / f"pmon-{date}.log"
    return atop_log, pmon_log


def _read_last_generated() -> _dt.datetime | None:
    """Return the timestamp of the previous report run, or None if unknown."""
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        stamp = json.loads(raw)["last_generated"]
        return _dt.datetime.fromisoformat(stamp).astimezone()
    except (ValueError, KeyError, TypeError):
        return None


def _write_last_generated(when: _dt.datetime) -> None:
    """Persist *when* as the last-report timestamp for the next run."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"last_generated": when.isoformat(timespec="seconds")})
    _STATE_FILE.write_text(payload + "\n", encoding="utf-8")


def _has_time_of_day(when: _dt.datetime) -> bool:
    """True when *when* is past local midnight, so a begin bound is needed."""
    return bool(when.hour or when.minute or when.second or when.microsecond)


def _plan_segments(start: _dt.datetime, end: _dt.datetime) -> list[_Segment]:
    """Resolve one `_Segment` per calendar day across ``[start, end]``.

    The first day is bounded at *start*'s time-of-day so a same-day re-run only
    covers the slice since the previous report; later days are covered in full.
    Returns an empty list when *start* is after *end* (e.g. a future state file).
    """
    segments: list[_Segment] = []
    day = start.date()
    while day <= end.date():
        atop_log, pmon_log = _resolve_logs(day.strftime("%Y%m%d"))
        if day == start.date() and _has_time_of_day(start):
            segments.append(
                _Segment(
                    atop_log,
                    pmon_log,
                    start.strftime("%Y%m%d%H%M%S"),
                    start.timestamp(),
                ),
            )
        else:
            segments.append(_Segment(atop_log, pmon_log))
        day += _dt.timedelta(days=1)
    return segments


_INSTALL_SCRIPT = Path(__file__).with_name("install_usage_monitoring.sh")


def _preflight(atop_log: Path) -> None:
    if not shutil.which("atop"):
        sys.exit(
            f"error: `atop` is not installed.\nrun: {_INSTALL_SCRIPT}",
        )
    if not atop_log.exists():
        sys.exit(
            f"error: atop log not found: {atop_log}\n"
            f"run: {_INSTALL_SCRIPT} (enables atop.service), "
            "then wait for the first sample.",
        )


_CLIPBOARD_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("wl-copy", ("wl-copy",)),
    ("xclip", ("xclip", "-selection", "clipboard")),
    ("xsel", ("xsel", "--clipboard", "--input")),
)


def _copy_to_clipboard(text: str) -> None:
    """Copy `text` to the system clipboard using the first available tool.

    Prints a one-line status to stderr so the stdout report stays pristine
    for redirection.
    """
    for name, cmd in _CLIPBOARD_CANDIDATES:
        if not shutil.which(name):
            continue
        try:
            subprocess.run(cmd, input=text, text=True, check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            sys.stderr.write(f"clipboard: {name} failed: {exc}\n")
            return
        sys.stderr.write(f"clipboard: copied {len(text)} chars via {name}\n")
        return
    sys.stderr.write(
        "clipboard: no wl-copy/xclip/xsel found; skipping copy\n",
    )


def _emit(args: argparse.Namespace, report: str) -> None:
    """Write the report to stdout and (unless suppressed) the clipboard."""
    sys.stdout.write(report)
    if not args.no_clipboard:
        _copy_to_clipboard(report)


def _period_line(start: _dt.datetime, end: _dt.datetime) -> str:
    """Markdown bullet describing the requested reporting period."""
    span = _fmt_h(max((end - start).total_seconds(), 0.0))
    return (
        f"- **Reporting period**: {start.isoformat(timespec='seconds')} → "
        f"{end.isoformat(timespec='seconds')} ({span})"
    )


def _is_single_day_mode(args: argparse.Namespace) -> bool:
    """True when the user pinned an exact day or explicit log paths."""
    return (
        args.date is not None or args.atop_log is not None or args.pmon_log is not None
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


def _build_parser() -> argparse.ArgumentParser:
    """Construct the command-line argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=None,
        help="report on one specific day (YYYYMMDD); ad hoc, ignores state",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="ad-hoc: report from this date (YYYYMMDD) to now; leaves state",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=_DEFAULT_TOP,
        help=f"rows per table (default: {_DEFAULT_TOP})",
    )
    parser.add_argument(
        "--atop-log",
        type=Path,
        default=None,
        help="override atop log path (implies single-day mode)",
    )
    parser.add_argument(
        "--pmon-log",
        type=Path,
        default=None,
        help="override pmon log path (implies single-day mode)",
    )
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="skip copying the report to the X clipboard",
    )
    parser.add_argument(
        "--no-update-state",
        action="store_true",
        help="do not advance the saved last-report timestamp",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the progress line on stderr",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point; see module docstring for CLI."""
    args = _build_parser().parse_args(argv)
    now = _dt.datetime.now().astimezone()
    if _is_single_day_mode(args):
        return _run_single_day(args, now)
    return _run_since(args, now)


if __name__ == "__main__":
    raise SystemExit(main())
