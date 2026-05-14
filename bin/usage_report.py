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
import contextlib
from dataclasses import dataclass, field
import datetime as _dt
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_ATOP_LOG_DIR = Path("/var/log/atop")
_PMON_LOG_DIR = Path.home() / ".local/share/gpu-log"
_DEFAULT_TOP = 15
_HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE_KB = os.sysconf("SC_PAGESIZE") // 1024 if hasattr(os, "sysconf") else 4
_SEC_PER_DAY = 86_400
_SEC_PER_HOUR = 3600
_SEC_PER_MIN = 60
_MIN_SAMPLES_FOR_WINDOW = 2
# atop parseable output layout (atop 2.x, same on Arch/Debian/Ubuntu):
# 0 label, 1 host, 2 epoch, 3 YYYY/MM/DD, 4 HH:MM:SS, 5 interval_s,
# then per-process fields starting at index 6.
# PRC per-proc: pid name(parens) state utime_ticks stime_ticks ...
_PRC_PID_IDX = 6
_PRC_NAME_IDX = 7
_PRC_MIN_LEN = 11
# PRM per-proc: pid name state pagesz_b vsize_kb rsize_kb ...
_PRM_PID_IDX = 6
_PRM_NAME_IDX = 7
_PRM_MIN_LEN = 12
_PMON_MIN_FIELDS = 11
_CPU_RECORD_MIN_LEN = 5
_PAREN_PAIR_MIN = 2
_ETA_MIN_FRACTION = 0.01
_ATOP_AGG_CACHE_BIN = Path.home() / ".cache" / "usage_report" / "atop_agg"
_ATOP_AGG_BIN_MODE = 0o755
# Repo layout: linux_configuration/scripts/system-maintenance/bin/usage_report.py
# -> parents[4] is the repo root which hosts the C/ source tree.
_ATOP_AGG_SRC_DIR = Path(__file__).resolve().parents[4] / "C" / "atop_agg"
_ATOP_AGG_BUILD_TIMEOUT_S = 60
_NATIVE_TSV_NAME_LEN = 7
_NATIVE_TSV_WIN_LEN = 5


@dataclass
class _PidCpu:
    """Per-PID cumulative-ticks tracker across atop samples."""

    name: str = ""
    first_ticks: int = -1
    last_ticks: int = 0
    samples: int = 0

    def observe(self, name: str, ticks: int) -> None:
        """Record one observation for this PID."""
        self.name = name  # last-seen name wins (stable for one PID)
        if self.first_ticks < 0:
            self.first_ticks = ticks
        self.last_ticks = ticks
        self.samples += 1

    @property
    def delta_ticks(self) -> int:
        """CPU ticks consumed during the observation window.

        For PIDs seen in >=2 samples the value is `last - first`, which is the
        actual CPU consumed between the first and last atop tick. For PIDs seen
        only once (short-lived processes that existed during exactly one tick)
        the cumulative value itself is used — this is close to the true
        lifetime cost for a short-lived process.
        """
        if self.samples >= _MIN_SAMPLES_FOR_WINDOW:
            return max(self.last_ticks - self.first_ticks, 0)
        return self.last_ticks


@dataclass
class _PidRam:
    """Per-PID peak/avg RSS tracker across atop samples."""

    name: str = ""
    peak_kb: int = 0
    sum_kb: int = 0
    samples: int = 0

    def observe(self, name: str, rss_kb: int) -> None:
        """Record one RSS observation for this PID."""
        self.name = name
        self.peak_kb = max(self.peak_kb, rss_kb)
        self.sum_kb += rss_kb
        self.samples += 1

    @property
    def avg_kb(self) -> float:
        """Mean RSS across the samples where this PID appeared."""
        return self.sum_kb / self.samples if self.samples else 0.0


@dataclass
class ProcAgg:
    """Aggregated metrics for one program name across all atop samples."""

    name: str
    cpu_ticks: int = 0
    peak_rss_kb: int = 0
    rss_kb_sum: int = 0
    rss_samples: int = 0
    pid_set: set[int] = field(default_factory=set)

    @property
    def cpu_seconds(self) -> float:
        """CPU-seconds consumed (sum of user + system time)."""
        return self.cpu_ticks / _HZ

    @property
    def peak_rss_mb(self) -> float:
        """Peak resident memory observed across the window, in MiB."""
        return self.peak_rss_kb / 1024

    @property
    def avg_rss_mb(self) -> float:
        """Average resident memory across samples where the program appeared."""
        if not self.rss_samples:
            return 0.0
        return (self.rss_kb_sum / self.rss_samples) / 1024


@dataclass
class GpuAgg:
    """Aggregated GPU metrics for one program name from pmon logs."""

    name: str
    sm_pct_sum: float = 0.0
    mem_pct_sum: float = 0.0
    samples: int = 0
    peak_sm_pct: float = 0.0
    peak_mem_pct: float = 0.0
    pid_set: set[int] = field(default_factory=set)

    @property
    def gpu_seconds(self) -> float:
        """SM-seconds (single-GPU equivalent); sm% * seconds_per_sample / 100."""
        return self.sm_pct_sum * _PMON_INTERVAL_S / 100.0

    @property
    def avg_sm_pct(self) -> float:
        """Mean SM utilization across samples where the process was present."""
        if not self.samples:
            return 0.0
        return self.sm_pct_sum / self.samples


# Default pmon interval is 10 s (matches the systemd service we set up).
_PMON_INTERVAL_S = 10
_PROGRESS_MIN_UPDATE_S = 0.1


class _Progress:
    """Minimal stage+percent+ETA reporter on stderr.

    Disabled automatically when stderr is not a TTY or when the caller
    constructs with `enabled=False`, so redirected output stays clean.
    """

    def __init__(self, *, enabled: bool, total_stages: int) -> None:
        self._enabled = enabled and sys.stderr.isatty()
        self._total_stages = total_stages
        self._stage_idx = 0
        self._stage_label = ""
        self._stage_start = 0.0
        self._t0 = _time.monotonic()
        self._last_draw = 0.0
        self._max_width = 0

    def start_stage(self, label: str) -> None:
        """Begin a new stage with its human label."""
        self._stage_idx += 1
        self._stage_label = label
        self._stage_start = _time.monotonic()
        self.update(0.0)

    def update(self, fraction: float) -> None:
        """Redraw the progress line for the current stage (0.0..1.0)."""
        if not self._enabled:
            return
        now = _time.monotonic()
        if now - self._last_draw < _PROGRESS_MIN_UPDATE_S and fraction < 1.0:
            return
        self._last_draw = now
        elapsed = now - self._stage_start
        pct = max(0.0, min(fraction, 1.0))
        if pct > _ETA_MIN_FRACTION:
            eta = elapsed * (1 - pct) / pct
            eta_str = f"~{eta:4.1f}s left"
        else:
            eta_str = "estimating…"
        msg = (
            f"[{self._stage_idx}/{self._total_stages}] "
            f"{self._stage_label:<22} {pct * 100:5.1f}%  "
            f"{elapsed:5.1f}s elapsed, {eta_str}"
        )
        self._max_width = max(self._max_width, len(msg))
        sys.stderr.write("\r" + msg.ljust(self._max_width))
        sys.stderr.flush()

    def finish(self) -> None:
        """Clear the progress line and print total elapsed time."""
        if not self._enabled:
            return
        total = _time.monotonic() - self._t0
        sys.stderr.write("\r" + " " * self._max_width + "\r")
        sys.stderr.write(f"done in {total:.1f}s\n")
        sys.stderr.flush()


def _run(cmd: list[str]) -> str:
    """Run *cmd* and return stdout (empty string on failure)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout


def _iter_atop_lines(log: Path, labels: str) -> Iterator[str]:
    """Stream `atop -r LOG -P LABELS` stdout line-by-line.

    Uses `Popen` so the report can show progress while atop is still
    decoding its binary log, rather than buffering the whole output.
    """
    cmd = ["atop", "-r", str(log), "-P", labels]
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as proc:
        stdout = proc.stdout
        if stdout is None:
            return
        for raw in stdout:
            yield raw.rstrip("\n")


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
    # After name comes: state utime stime ...
    try:
        utime = int(parts[after + 1])
        stime = int(parts[after + 2])
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
    )


def _atop_agg_binary() -> Path | None:
    """Return a cached `atop_agg` binary path, auto-building if missing/stale.

    Falls back to ``None`` when the C source tree or a system C compiler
    is unavailable, in which case callers use the pure-Python parser.
    """
    src_c = _ATOP_AGG_SRC_DIR / "atop_agg.c"
    if _ATOP_AGG_CACHE_BIN.exists() and (
        not src_c.exists()
        or src_c.stat().st_mtime <= _ATOP_AGG_CACHE_BIN.stat().st_mtime
    ):
        return _ATOP_AGG_CACHE_BIN
    if not src_c.exists() or shutil.which("cc") is None:
        return None
    _ATOP_AGG_CACHE_BIN.parent.mkdir(parents=True, exist_ok=True)
    make_cmd = ["make", "-s", "-C", str(_ATOP_AGG_SRC_DIR), "atop_agg"]
    try:
        subprocess.run(
            make_cmd,
            check=True,
            capture_output=True,
            timeout=_ATOP_AGG_BUILD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    built = _ATOP_AGG_SRC_DIR / "atop_agg"
    if not built.exists():
        return None
    shutil.copy2(built, _ATOP_AGG_CACHE_BIN)
    _ATOP_AGG_CACHE_BIN.chmod(_ATOP_AGG_BIN_MODE)
    return _ATOP_AGG_CACHE_BIN


def _apply_native_name(parts: list[str], agg_map: dict[str, ProcAgg]) -> None:
    r"""Fold one `N\\t<name>\\t<cpu>\\t<peak>\\t<sum_avg>\\t<ram_n>\\t<pids>` row."""
    _, name, cpu_s, peak_s, sum_avg_s, rss_n_s, pids_s = parts
    entry = agg_map.setdefault(name, ProcAgg(name=name))
    entry.cpu_ticks = int(cpu_s)
    entry.peak_rss_kb = int(peak_s)
    entry.rss_kb_sum = int(sum_avg_s)
    entry.rss_samples = int(rss_n_s)
    # The C helper pre-aggregates by name; pid_set is unused in the native
    # path but `len(pid_set)` drives the "PIDs" column in the report.
    entry.pid_set = set(range(int(pids_s)))


def _window_from_native(parts: list[str]) -> _Window:
    r"""Build a `_Window` from a `W\\t<start>\\t<end>\\t<n>\\t<interval>` row."""
    _, start_s, end_s, n_s, interval_s = parts
    n_epochs = int(n_s)
    if not n_epochs:
        return _Window()
    start_epoch = int(start_s)
    end_epoch = int(end_s)
    start_dt = _dt.datetime.fromtimestamp(start_epoch).astimezone()
    end_dt = _dt.datetime.fromtimestamp(end_epoch).astimezone()
    return _Window(
        start=start_dt.isoformat(timespec="seconds"),
        end=end_dt.isoformat(timespec="seconds"),
        distinct_samples=n_epochs,
        interval_s=int(interval_s),
        seconds=end_epoch - start_epoch,
    )


def _aggregate_atop_native(
    log: Path,
    progress: _Progress,
    binary: Path,
) -> tuple[dict[str, ProcAgg], _Window]:
    """Aggregate via `atop | atop_agg`; return `(by_name, window)`."""
    progress.start_stage("atop: parse PRC+PRM (native)")
    agg_map: dict[str, ProcAgg] = {}
    window = _Window()
    atop_cmd = ["atop", "-r", str(log), "-P", "PRC,PRM"]
    agg_cmd = [str(binary)]
    with (
        subprocess.Popen(
            atop_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as atop,
        subprocess.Popen(
            agg_cmd,
            stdin=atop.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as agg,
    ):
        if atop.stdout is not None:
            atop.stdout.close()
        stdout = agg.stdout
        if stdout is None:
            return agg_map, window
        for raw in stdout:
            parts = raw.rstrip("\n").split("\t")
            tag = parts[0]
            if tag == "N" and len(parts) == _NATIVE_TSV_NAME_LEN:
                _apply_native_name(parts, agg_map)
            elif tag == "W" and len(parts) == _NATIVE_TSV_WIN_LEN:
                window = _window_from_native(parts)
    progress.update(1.0)
    return agg_map, window


def aggregate_atop(
    log: Path,
    progress: _Progress,
) -> tuple[dict[str, ProcAgg], _Window]:
    """Stream PRC+PRM records, fold them into `{name: ProcAgg}`, return window.

    Prefers the native `atop_agg` C helper (auto-built into
    ``~/.cache/usage_report/``) for ~7\u00d7 speedup on full-day logs, falling
    back to an inline Python parser when the helper is unavailable.
    """
    binary = _atop_agg_binary()
    if binary is not None:
        return _aggregate_atop_native(log, progress, binary)
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
    for raw in _iter_atop_lines(log, "PRC,PRM"):
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


def _pmon_fields(line: str) -> list[str] | None:
    """Return stripped fields of a pmon data line, or None for headers/blanks."""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    return s.split()


def _normalize_pmon_command(command_fields: list[str]) -> str:
    """Normalize pmon command fields into a stable process-ish name.

    `nvidia-smi pmon -o DT` emits fixed numeric columns followed by a command
    field that can include whitespace. We prefer the *first* non-option token
    (usually executable) and normalize it to a basename.
    """
    tokens = [token.strip().strip("\"'") for token in command_fields if token.strip()]
    if not tokens:
        return "unknown"

    selected = tokens[0]
    if selected.startswith("-"):
        for candidate in tokens[1:]:
            if not candidate.startswith("-"):
                selected = candidate
                break

    name = Path(selected).name.strip(";,:")
    if not name:
        return "unknown"
    return name


def _pid_comm_name(pid: int) -> str | None:
    """Return `/proc/<pid>/comm` basename when available."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(comm).name if comm else None


def aggregate_pmon(
    log: Path,
    progress: _Progress,
) -> tuple[dict[str, GpuAgg], int]:
    """Return `({program: GpuAgg}, sample_count)` from the pmon *log*."""
    progress.start_stage("pmon log scan")
    agg: dict[str, GpuAgg] = {}
    samples = 0
    if not log.exists():
        progress.update(1.0)
        return agg, 0
    total_bytes = max(log.stat().st_size, 1)
    bytes_read = 0
    with log.open(encoding="utf-8") as fh:
        for line in fh:
            bytes_read += len(line)
            progress.update(min(bytes_read / total_bytes, 0.99))
            parts = _pmon_fields(line)
            if parts is None or len(parts) < _PMON_MIN_FIELDS:
                continue
            samples += _ingest_pmon_row(parts, agg)
    progress.update(1.0)
    return agg, samples


def _ingest_pmon_row(parts: list[str], agg: dict[str, GpuAgg]) -> int:
    """Fold a single pmon data row into *agg*; return 1 if consumed else 0."""
    # pmon -o DT fields:
    # date time gpu pid type sm mem enc dec jpg ofa command
    try:
        pid = int(parts[3])
    except ValueError:
        return 0
    sm_raw = parts[5]
    mem_raw = parts[6]
    command_fields = parts[11:]
    name = _normalize_pmon_command(command_fields)
    if name == "unknown":
        name = _pid_comm_name(pid) or "unknown"
    sm = float(sm_raw) if sm_raw != "-" else 0.0
    mem = float(mem_raw) if mem_raw != "-" else 0.0
    entry = agg.setdefault(name, GpuAgg(name=name))
    entry.sm_pct_sum += sm
    entry.mem_pct_sum += mem
    entry.samples += 1
    entry.pid_set.add(pid)
    entry.peak_sm_pct = max(entry.peak_sm_pct, sm)
    entry.peak_mem_pct = max(entry.peak_mem_pct, mem)
    return 1


@dataclass
class _Window:
    """Observed atop coverage window."""

    start: str = "n/a"
    end: str = "n/a"
    distinct_samples: int = 0
    interval_s: int = 0
    seconds: int = 0


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
