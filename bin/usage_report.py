#!/usr/bin/env python3
"""End-of-day resource usage report from atop + nvidia-smi pmon logs.

Parses the current-day (or given) `atop` binary log via `atop -P PRC,PRM -r`
and the per-process nvidia-smi pmon log, aggregates CPU seconds, peak/average
RSS, and GPU SM-% seconds per program, and prints a compact Markdown report
intended to be pasted into an LLM (Claude / Copilot) for further analysis.

Run with no arguments to report on today's logs:

    usage_report.py                       # today
    usage_report.py --date 20260419       # specific day
    usage_report.py --top 20              # keep 20 rows per table
    usage_report.py > report.md           # redirect to a file

The output intentionally front-loads metadata (hostname, window, sample
count, HZ, machine specs) so the LLM never has to guess context.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as _dt
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

from _usage_report_parsing import _run, aggregate_atop, aggregate_pmon
from _usage_report_types import (
    _HZ,
    _PMON_INTERVAL_S,
    GpuAgg,
    ProcAgg,
    _Progress,
    _Window,
)

_ATOP_LOG_DIR = Path("/var/log/atop")
_PMON_LOG_DIR = Path.home() / ".local/share/gpu-log"
_DEFAULT_TOP = 15
_PAGE_KB = os.sysconf("SC_PAGESIZE") // 1024 if hasattr(os, "sysconf") else 4
_SEC_PER_DAY = 86_400
_SEC_PER_HOUR = 3600
_SEC_PER_MIN = 60


def _host_profile() -> dict[str, str]:
    """Collect a small bag of identifying facts about the host."""
    info: dict[str, str] = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "cpus_online": str(os.cpu_count() or 0),
    }
    try:
        with Path("/proc/cpuinfo").open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    info["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with Path("/proc/meminfo").open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    kb = int(re.findall(r"\d+", line)[0])
                    info["memory_total_gib"] = f"{kb / 1024 / 1024:.1f}"
                    break
    except (OSError, IndexError, ValueError):
        pass
    gpu = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader",
        ],
    ).strip()
    if gpu:
        info["gpu"] = gpu.replace("\n", "; ")
    return info


def _md_escape(name: str) -> str:
    """Escape characters that would break a Markdown table cell."""
    return name.replace("|", r"\|").replace("\n", " ")


def _fmt_h(seconds: float) -> str:
    """Human-friendly duration: `"1h 23m"` / `"4m 12s"` / `"8.3s"`."""
    if seconds >= _SEC_PER_HOUR:
        h = int(seconds // _SEC_PER_HOUR)
        m = int((seconds % _SEC_PER_HOUR) // _SEC_PER_MIN)
        return f"{h}h {m:02d}m"
    if seconds >= _SEC_PER_MIN:
        m = int(seconds // _SEC_PER_MIN)
        s = int(seconds % _SEC_PER_MIN)
        return f"{m}m {s:02d}s"
    return f"{seconds:.1f}s"


def _cpu_table(aggs: Iterable[ProcAgg], window_s: int, top: int) -> list[str]:
    ncpu = os.cpu_count() or 1
    header = (
        "| # | Program | CPU-seconds | Avg CPU% (of 1 core) |"
        " Avg CPU% (of box) | Peak RSS | PIDs |"
    )
    sep = (
        "|---|---------|------------:|---------------------:|"
        "------------------:|---------:|-----:|"
    )
    rows: list[str] = [header, sep]
    top_items = sorted(aggs, key=lambda a: a.cpu_ticks, reverse=True)[:top]
    for idx, item in enumerate(top_items, start=1):
        single = (item.cpu_seconds / window_s * 100) if window_s else 0.0
        box = single / ncpu
        rows.append(
            "| "
            f"{idx} | {_md_escape(item.name)} | "
            f"{item.cpu_seconds:,.0f}s ({_fmt_h(item.cpu_seconds)}) | "
            f"{single:.1f}% | {box:.1f}% | "
            f"{item.peak_rss_mb:,.0f} MiB | {len(item.pid_set)} |",
        )
    return rows


_RAM_BUCKET_MIB = 1  # dedupe rows whose peak RSS rounds to the same MiB
_MAX_SIBLINGS_SHOWN = 6


def _dedupe_ram(aggs: Iterable[ProcAgg]) -> list[tuple[ProcAgg, list[str]]]:
    """Group rows by peak-RSS bucket; keep the top-CPU row per bucket.

    Returns a list of `(representative, sibling_names)` ordered by peak RSS
    descending. Siblings are the other names that shared the same RSS bucket
    (likely threads of the same parent process).
    """
    buckets: dict[int, list[ProcAgg]] = defaultdict(list)
    for item in aggs:
        if item.peak_rss_kb <= 0:
            continue
        key = round(item.peak_rss_kb / 1024 / _RAM_BUCKET_MIB)
        buckets[key].append(item)
    result: list[tuple[ProcAgg, list[str]]] = []
    for bucket in buckets.values():
        bucket.sort(key=lambda a: (a.cpu_ticks, len(a.pid_set)), reverse=True)
        rep = bucket[0]
        siblings = [b.name for b in bucket[1:]]
        result.append((rep, siblings))
    result.sort(key=lambda t: t[0].peak_rss_kb, reverse=True)
    return result


def _ram_table(aggs: Iterable[ProcAgg], top: int) -> list[str]:
    header = (
        "| # | Program | Peak RSS | Avg RSS | CPU-seconds | PIDs |"
        " Sibling names (shared RSS) |"
    )
    sep = (
        "|---|---------|---------:|--------:|------------:|-----:|"
        "----------------------------|"
    )
    rows: list[str] = [header, sep]
    for idx, (item, siblings) in enumerate(_dedupe_ram(aggs)[:top], start=1):
        if not siblings:
            sib = "\u2014"
        else:
            shown = ", ".join(_md_escape(s) for s in siblings[:_MAX_SIBLINGS_SHOWN])
            extra = (
                f" (+{len(siblings) - _MAX_SIBLINGS_SHOWN} more)"
                if len(siblings) > _MAX_SIBLINGS_SHOWN
                else ""
            )
            sib = f"{shown}{extra}"
        rows.append(
            "| "
            f"{idx} | {_md_escape(item.name)} | "
            f"{item.peak_rss_mb:,.0f} MiB | "
            f"{item.avg_rss_mb:,.0f} MiB | "
            f"{item.cpu_seconds:,.0f}s | "
            f"{len(item.pid_set)} | {sib} |",
        )
    return rows


def _gpu_table(aggs: dict[str, GpuAgg], total_samples: int, top: int) -> list[str]:
    header = (
        "| # | Program | GPU SM-seconds | Avg SM% (when present) |"
        " Peak SM% | Peak MEM% | Samples | PIDs |"
    )
    sep = (
        "|---|---------|---------------:|-----------------------:|"
        "---------:|----------:|--------:|-----:|"
    )
    rows: list[str] = [header, sep]
    top_items = sorted(aggs.values(), key=lambda a: a.gpu_seconds, reverse=True)[:top]
    for idx, item in enumerate(top_items, start=1):
        presence = (item.samples / total_samples * 100) if total_samples else 0.0
        rows.append(
            "| "
            f"{idx} | {_md_escape(item.name)} | "
            f"{item.gpu_seconds:,.0f}s ({_fmt_h(item.gpu_seconds)}) | "
            f"{item.avg_sm_pct:.1f}% | "
            f"{item.peak_sm_pct:.0f}% | "
            f"{item.peak_mem_pct:.0f}% | "
            f"{item.samples} ({presence:.0f}%) | "
            f"{len(item.pid_set)} |",
        )
    return rows


def _fingerprint_section() -> list[str]:
    info = _host_profile()
    return [
        "## Host",
        "",
        *[f"- **{k}**: {v}" for k, v in info.items()],
        "",
    ]


def _methodology_section(atop_log: Path, pmon_log: Path, window: _Window) -> list[str]:
    window_note = (
        f"- **Coverage window**: {_fmt_h(window.seconds)} "
        f"(from first to last atop sample; window may be shorter than wall "
        f"clock since the next atop tick has not yet fired)."
    )
    interval_note = (
        f"- **atop sample interval (observed)**: {window.interval_s}s"
        if window.interval_s
        else "- **atop sample interval**: only one sample so far; interval unknown."
    )
    task_note = (
        "- atop's parseable output is **task-level** (threads get their own "
        "rows keyed by `/proc/<tid>/comm`); names like 'Main Thread' or "
        "'dxvk-frame' are usually Wine/game worker threads of one parent."
    )
    rss_note = (
        "- RSS is shared across threads of one process, so multiple rows "
        "with identical 'Peak RSS' almost certainly belong to a single "
        "parent. The RAM table dedupes by peak-RSS bucket and lists "
        "sibling thread names under `(+ siblings)`."
    )
    cpu_note = (
        "- **CPU-seconds** are computed per-PID as "
        "`last_cumulative_ticks - first_cumulative_ticks` (or the cumulative "
        "value itself for PIDs seen only once). They reflect CPU consumed "
        "during the coverage window only, not since process start."
    )
    gpu_note = (
        "- GPU SM-seconds = sum(sm% per sample) \u00d7 sample interval / 100; "
        "single-GPU equivalent."
    )
    prog_note = (
        "- 'Program' = executable/thread name; rows with the same name "
        "are summed across their distinct PIDs."
    )
    return [
        "## Methodology",
        "",
        f"- **atop log**: `{atop_log}` (binary, replay with `atop -r`)",
        f"- **pmon log**: `{pmon_log}` (`nvidia-smi pmon -d {_PMON_INTERVAL_S}`)",
        f"- **HZ**: {_HZ} ticks/s; **page size**: {_PAGE_KB} KiB",
        window_note,
        interval_note,
        cpu_note,
        task_note,
        rss_note,
        gpu_note,
        prog_note,
        "",
    ]


def _compute_window(atop_log: Path, progress: _Progress) -> _Window:
    """Deprecated helper kept for backwards import compatibility.

    New code should call :func:`aggregate_atop`, which returns the window
    alongside the per-process aggregates from a single atop subprocess.
    """
    _, window = aggregate_atop(atop_log, progress)
    if not window.seconds:
        window.seconds = _SEC_PER_DAY
    return window


_LLM_PROMPT = [
    "> Below is a day's worth of aggregated resource usage for my Linux workstation.",
    "> Identify which programs are the biggest hogs, flag anything that looks abnormal",
    "> for a typical developer/gaming setup, and suggest concrete optimisations",
    "> (config tweaks, process limits, alternative tools). Be specific.",
]


_REPORT_STAGES = 2


def _build_report(
    args: argparse.Namespace,
    atop_log: Path,
    pmon_log: Path,
) -> str:
    progress = _Progress(
        enabled=not args.quiet,
        total_stages=_REPORT_STAGES,
    )
    cpu_aggs, window = aggregate_atop(atop_log, progress)
    if not window.seconds:
        window.seconds = _SEC_PER_DAY
    gpu_aggs, gpu_samples = aggregate_pmon(pmon_log, progress)
    progress.finish()

    gpu_section = (
        _gpu_table(gpu_aggs, gpu_samples, args.top)
        if gpu_aggs
        else ["_No GPU pmon data found._"]
    )
    generated = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    interval = f"{window.interval_s}s" if window.interval_s else "n/a (single sample)"
    lines: list[str] = [
        "# System resource usage report",
        "",
        f"- **Generated**: {generated}",
        f"- **atop window**: {window.start} \u2192 {window.end}",
        f"- **atop samples**: {window.distinct_samples} distinct "
        f"timestamps (sample interval \u2248 {interval})",
        f"- **GPU pmon samples**: {gpu_samples} (\u2248{_PMON_INTERVAL_S}s each)",
        "",
        *_fingerprint_section(),
        *_methodology_section(atop_log, pmon_log, window),
        "## Top CPU consumers",
        "",
        *_cpu_table(cpu_aggs.values(), window.seconds, args.top),
        "",
        "## Top RAM consumers (by peak RSS, deduped by shared-memory bucket)",
        "",
        *_ram_table(cpu_aggs.values(), args.top),
        "",
        "## Top GPU consumers",
        "",
        *gpu_section,
        "",
        "## Suggested LLM prompt",
        "",
        *_LLM_PROMPT,
        "",
    ]
    return "\n".join(lines) + "\n"


def _resolve_logs(date: str) -> tuple[Path, Path]:
    atop_log = _ATOP_LOG_DIR / f"atop_{date}"
    pmon_log = _PMON_LOG_DIR / f"pmon-{date}.log"
    return atop_log, pmon_log


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


def main(argv: list[str] | None = None) -> int:
    """Entry point; see module docstring for CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=_dt.datetime.now().astimezone().strftime("%Y%m%d"),
        help="YYYYMMDD to report on (default: today)",
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
        help="override atop log path",
    )
    parser.add_argument(
        "--pmon-log",
        type=Path,
        default=None,
        help="override pmon log path",
    )
    parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="skip copying the report to the X clipboard",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the progress line on stderr",
    )
    args = parser.parse_args(argv)

    atop_default, pmon_default = _resolve_logs(args.date)
    atop_log = args.atop_log or atop_default
    pmon_log = args.pmon_log or pmon_default
    _preflight(atop_log)
    report = _build_report(args, atop_log, pmon_log)
    sys.stdout.write(report)
    if not args.no_clipboard:
        _copy_to_clipboard(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
