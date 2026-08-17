"""Markdown report assembly for usage_report (host, methodology, full report)."""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
import platform
import re
from typing import TYPE_CHECKING

from _usage_report_atop import _run
from _usage_report_format import _fmt_h
from _usage_report_tables import _cpu_table, _gpu_table, _ram_table
from _usage_report_types import _HZ, _PMON_INTERVAL_S, _Window

if TYPE_CHECKING:
    from usage_report import _Aggregates

_PAGE_KB = os.sysconf("SC_PAGESIZE") // 1024 if hasattr(os, "sysconf") else 4


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
