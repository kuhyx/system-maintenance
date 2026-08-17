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
import datetime as _dt
from pathlib import Path

from _usage_report_run import _run_since, _run_single_day

_DEFAULT_TOP = 15


def _is_single_day_mode(args: argparse.Namespace) -> bool:
    """True when the user pinned an exact day or explicit log paths."""
    return (
        args.date is not None or args.atop_log is not None or args.pmon_log is not None
    )


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
