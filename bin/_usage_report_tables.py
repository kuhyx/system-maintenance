"""Markdown table builders for usage_report (CPU, RAM and GPU sections)."""

from __future__ import annotations

from collections import defaultdict
import os
from typing import TYPE_CHECKING

from _usage_report_format import _fmt_h, _md_escape

if TYPE_CHECKING:
    from collections.abc import Iterable

    from _usage_report_types import GpuAgg, ProcAgg

_RAM_BUCKET_MIB = 1  # dedupe rows whose peak RSS rounds to the same MiB
_MAX_SIBLINGS_SHOWN = 6


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
