"""atop record parsing and per-day aggregate merging for usage_report."""

from __future__ import annotations

import contextlib
import datetime as _dt
from typing import TYPE_CHECKING

from _usage_report_atop import (
    _aggregate_atop_native,
    _atop_agg_binary,
    _iter_atop_lines,
)
from _usage_report_types import (
    _MIN_SAMPLES_FOR_WINDOW,
    GpuAgg,
    ProcAgg,
    _PidCpu,
    _PidRam,
    _Window,
)

if TYPE_CHECKING:
    from pathlib import Path

    from _usage_report_types import _Progress

# atop parseable output layout (atop 2.x, same on Arch/Debian/Ubuntu):
# 0 label, 1 host, 2 epoch, 3 YYYY/MM/DD, 4 HH:MM:SS, 5 interval_s,
# then per-process fields starting at index 6.
# PRC per-proc: pid name(parens) state HZ utime_ticks stime_ticks ...
# NOTE: atop inserts its clock-tick rate (HZ) between `state` and `utime`
# (the PRC analogue of the pagesize field PRM inserts before its memory
# columns); utime/stime therefore live two and three slots past `state`.
_PRC_PID_IDX = 6
_PRC_NAME_IDX = 7
_PRC_MIN_LEN = 12
# PRM per-proc: pid name state pagesz_b vsize_kb rsize_kb ...
_PRM_PID_IDX = 6
_PRM_NAME_IDX = 7
_PRM_MIN_LEN = 12
_CPU_RECORD_MIN_LEN = 5
_PAREN_PAIR_MIN = 2


def _parse_name(parts: list[str], name_idx: int) -> tuple[str, int]:
    """Extract `(name, next_index)` from atop parseable output.

    atop wraps process names in parentheses and the name itself may contain
    spaces, so we rejoin until we hit the closing `)`. Fast-paths the common
    case where the name is a single token (no embedded spaces).
    """
    if name_idx >= len(parts):
        return "unknown", name_idx + 1
    token = parts[name_idx]
    # Fast path: `(name)` fully in one token.
    if len(token) >= _PAREN_PAIR_MIN and token[0] == "(" and token[-1] == ")":
        return token[1:-1] or "unknown", name_idx + 1
    if token.startswith("("):
        buf = [token]
        idx = name_idx
        while not buf[-1].endswith(")") and idx + 1 < len(parts):
            idx += 1
            buf.append(parts[idx])
        name = " ".join(buf)[1:-1] or "unknown"
        return name, idx + 1
    return token, name_idx + 1


def _parse_prc(parts: list[str], pid_cpu: dict[int, _PidCpu]) -> None:
    """Fold one PRC record into the per-PID CPU-ticks map."""
    try:
        pid = int(parts[_PRC_PID_IDX])
    except (ValueError, IndexError):
        return
    name, after = _parse_name(parts, _PRC_NAME_IDX)
    # After name comes: state HZ utime stime ...  (HZ is atop's clock-tick
    # rate; skipping it is what keeps a constant 100 from being charged as
    # CPU to every record — the bug that made cpu-seconds collapse to PID
    # count for short-lived processes).
    try:
        utime = int(parts[after + 2])
        stime = int(parts[after + 3])
    except (ValueError, IndexError):
        return
    pid_cpu.setdefault(pid, _PidCpu()).observe(name, utime + stime)


def _parse_prm(parts: list[str], pid_ram: dict[int, _PidRam]) -> None:
    """Fold one PRM record into the per-PID RSS map."""
    try:
        pid = int(parts[_PRM_PID_IDX])
    except (ValueError, IndexError):
        return
    name, after = _parse_name(parts, _PRM_NAME_IDX)
    # After name: state pagesz_b vsize_kb rsize_kb ...
    try:
        rsize_kb = int(parts[after + 3])
    except (ValueError, IndexError):
        return
    pid_ram.setdefault(pid, _PidRam()).observe(name, rsize_kb)


def _window_from_epochs(epochs: set[int]) -> _Window:
    """Build a `_Window` from a set of sample epoch timestamps."""
    if not epochs:
        return _Window()
    ordered = sorted(epochs)
    start_dt = _dt.datetime.fromtimestamp(ordered[0]).astimezone()
    end_dt = _dt.datetime.fromtimestamp(ordered[-1]).astimezone()
    interval = 0
    if len(ordered) >= _MIN_SAMPLES_FOR_WINDOW:
        deltas = sorted(ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1))
        interval = deltas[len(deltas) // 2]
    return _Window(
        start=start_dt.isoformat(timespec="seconds"),
        end=end_dt.isoformat(timespec="seconds"),
        distinct_samples=len(ordered),
        interval_s=interval,
        seconds=ordered[-1] - ordered[0],
        start_epoch=ordered[0],
        end_epoch=ordered[-1],
    )


def aggregate_atop(
    log: Path,
    progress: _Progress,
    begin: str | None = None,
    end: str | None = None,
) -> tuple[dict[str, ProcAgg], _Window]:
    """Stream PRC+PRM records, fold them into `{name: ProcAgg}`, return window.

    Prefers the native `atop_agg` C helper (auto-built into
    ``~/.cache/usage_report/``) for ~7x speedup on full-day logs, falling
    back to an inline Python parser when the helper is unavailable.

    *begin*/*end* are optional atop `-b`/`-e` arguments that bound replay to a
    sub-window of the day's log (used by the "since last report" mode).
    """
    binary = _atop_agg_binary()
    if binary is not None:
        return _aggregate_atop_native(log, progress, binary, begin, end)
    progress.start_stage("atop: parse PRC+PRM")
    pid_cpu: dict[int, _PidCpu] = {}
    pid_ram: dict[int, _PidRam] = {}
    epochs: set[int] = set()
    log_size = max(log.stat().st_size, 1)
    bytes_seen = 0
    # Empirical: `atop -P PRC,PRM` stdout is ~11x the binary log size on a
    # 10-min-interval log. The fraction is only used for the progress bar,
    # so a rough calibration is fine; it caps at 99% if we underestimate.
    est_total_bytes = log_size * 11 or 1
    for raw in _iter_atop_lines(log, "PRC,PRM", begin, end):
        bytes_seen += len(raw) + 1
        if not raw or raw[0] == "#" or raw.startswith("RESET") or raw == "SEP":
            continue
        parts = raw.split()
        if not parts:
            continue
        label = parts[0]
        if label == "PRC" and len(parts) >= _PRC_MIN_LEN:
            with contextlib.suppress(ValueError):
                # atop always emits an integer epoch here; guard is defensive.
                epochs.add(int(parts[2]))
            progress.update(min(bytes_seen / est_total_bytes, 0.99))
            _parse_prc(parts, pid_cpu)
        elif label == "PRM" and len(parts) >= _PRM_MIN_LEN:
            _parse_prm(parts, pid_ram)
    progress.update(1.0)
    return _fold_pid_aggregates(pid_cpu, pid_ram), _window_from_epochs(epochs)


def _fold_pid_aggregates(
    pid_cpu: dict[int, _PidCpu],
    pid_ram: dict[int, _PidRam],
) -> dict[str, ProcAgg]:
    """Collapse per-PID CPU/RAM trackers into per-program `ProcAgg` entries."""
    agg: dict[str, ProcAgg] = {}
    for pid, cpu in pid_cpu.items():
        entry = agg.setdefault(cpu.name, ProcAgg(name=cpu.name))
        entry.cpu_ticks += cpu.delta_ticks
        entry.pid_set.add(pid)
    for pid, ram in pid_ram.items():
        entry = agg.setdefault(ram.name, ProcAgg(name=ram.name))
        entry.peak_rss_kb = max(entry.peak_rss_kb, ram.peak_kb)
        entry.rss_kb_sum += int(ram.avg_kb)
        entry.rss_samples += 1
        entry.pid_set.add(pid)
    return agg


def merge_proc_aggs(dst: dict[str, ProcAgg], src: dict[str, ProcAgg]) -> None:
    """Fold one day's CPU/RAM aggregates (*src*) into the running *dst*.

    CPU-seconds and RSS sample counts add across days; peak RSS is the max;
    PID counts add (each day contributes its own distinct PIDs).
    """
    for name, item in src.items():
        entry = dst.setdefault(name, ProcAgg(name=name))
        entry.cpu_ticks += item.cpu_ticks
        entry.peak_rss_kb = max(entry.peak_rss_kb, item.peak_rss_kb)
        entry.rss_kb_sum += item.rss_kb_sum
        entry.rss_samples += item.rss_samples
        entry.extra_pids += item.pid_count


def merge_gpu_aggs(dst: dict[str, GpuAgg], src: dict[str, GpuAgg]) -> None:
    """Fold one day's GPU aggregates (*src*) into the running *dst*."""
    for name, item in src.items():
        entry = dst.setdefault(name, GpuAgg(name=name))
        entry.sm_pct_sum += item.sm_pct_sum
        entry.mem_pct_sum += item.mem_pct_sum
        entry.samples += item.samples
        entry.peak_sm_pct = max(entry.peak_sm_pct, item.peak_sm_pct)
        entry.peak_mem_pct = max(entry.peak_mem_pct, item.peak_mem_pct)
        entry.extra_pids += item.pid_count


def merge_windows(windows: list[_Window]) -> _Window:
    """Combine per-day coverage *windows* into one spanning window.

    Start/end span the earliest and latest samples; ``seconds`` sums the
    per-day coverage (not wall-clock end-start) so the denominator for average
    CPU% reflects only the time actually monitored, excluding gap days.
    """
    real = [w for w in windows if w.distinct_samples]
    if not real:
        return _Window()
    first = min(real, key=lambda w: w.start_epoch)
    last = max(real, key=lambda w: w.end_epoch)
    intervals = [w.interval_s for w in real if w.interval_s]
    # Representative interval = the most common per-day interval, if any.
    interval = max(set(intervals), key=intervals.count) if intervals else 0
    return _Window(
        start=first.start,
        end=last.end,
        distinct_samples=sum(w.distinct_samples for w in real),
        interval_s=interval,
        seconds=sum(w.seconds for w in real),
        start_epoch=first.start_epoch,
        end_epoch=last.end_epoch,
    )
