"""Markdown report rendering helpers for usage_report."""

from __future__ import annotations

from collections import defaultdict
import datetime as _dt
import os
from pathlib import Path
import platform
import re
from typing import TYPE_CHECKING

from _usage_report_parsing import _run
from _usage_report_types import _HZ, _PMON_INTERVAL_S, GpuAgg, ProcAgg, _Window

if TYPE_CHECKING:
    from collections.abc import Iterable

    from usage_report import _Aggregates

_SEC_PER_HOUR = 3600
_SEC_PER_MIN = 60
_PAGE_KB = os.sysconf("SC_PAGESIZE") // 1024 if hasattr(os, "sysconf") else 4
_RAM_BUCKET_MIB = 1  # dedupe rows whose peak RSS rounds to the same MiB
_MAX_SIBLINGS_SHOWN = 6


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
            f"{item.peak_rss_mb:,.0f} MiB | {item.pid_count} |",
        )
    return rows


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
        bucket.sort(key=lambda a: (a.cpu_ticks, a.pid_count), reverse=True)
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
            sib = "—"
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
            f"{item.pid_count} | {sib} |",
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
            f"{item.pid_count} |",
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


def _methodology_section(
    atop_desc: str,
    pmon_desc: str,
    window: _Window,
) -> list[str]:
    window_note = (
        f"- **Coverage window**: {_fmt_h(window.seconds)} "
        f"(sum of per-day atop coverage from first to last sample; excludes "
        f"any gap days where atop was not logging, and the final partial tick)."
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
        f"- **atop log(s)**: {atop_desc}",
        f"- **pmon log(s)**: {pmon_desc}",
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


_LLM_PROMPT = [
    "> Below is aggregated resource usage for my Linux workstation over the",
    "> reporting period shown above. Identify which programs are the biggest",
    "> hogs, flag anything that looks abnormal for a typical developer/gaming",
    "> setup, and suggest concrete optimisations (config tweaks, process limits,",
    "> alternative tools). Be specific.",
]


def _render_report(
    aggs: _Aggregates,
    *,
    top: int,
    atop_desc: str,
    pmon_desc: str,
    period_line: str,
) -> str:
    """Assemble the Markdown report from already-aggregated data."""
    window = aggs.window
    gpu_section = (
        _gpu_table(aggs.gpu, aggs.gpu_samples, top)
        if aggs.gpu
        else ["_No GPU pmon data found._"]
    )
    generated = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    interval = f"{window.interval_s}s" if window.interval_s else "n/a (single sample)"
    lines: list[str] = [
        "# System resource usage report",
        "",
        f"- **Generated**: {generated}",
        period_line,
        f"- **atop window**: {window.start} → {window.end}",
        f"- **atop samples**: {window.distinct_samples} distinct "
        f"timestamps (sample interval ≈ {interval})",
        f"- **GPU pmon samples**: {aggs.gpu_samples} (≈{_PMON_INTERVAL_S}s each)",
        "",
        *_fingerprint_section(),
        *_methodology_section(atop_desc, pmon_desc, window),
        "## Top CPU consumers",
        "",
        *_cpu_table(aggs.cpu.values(), window.seconds, top),
        "",
        "## Top RAM consumers (by peak RSS, deduped by shared-memory bucket)",
        "",
        *_ram_table(aggs.cpu.values(), top),
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
