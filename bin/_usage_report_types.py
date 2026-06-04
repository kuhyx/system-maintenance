"""Shared data-class types and progress reporter for usage_report."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import sys
import time as _time

_HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_MIN_SAMPLES_FOR_WINDOW = 2
# Default pmon interval is 10 s (matches the systemd service we set up).
_PMON_INTERVAL_S = 10
_PROGRESS_MIN_UPDATE_S = 0.1
_ETA_MIN_FRACTION = 0.01


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
    # PID counts folded in when merging per-day aggregates. Tracked as a plain
    # integer (not by extending `pid_set`) because the native parser stores a
    # synthetic `range(n)` set whose union across days would collapse counts.
    extra_pids: int = 0

    @property
    def pid_count(self) -> int:
        """Distinct PIDs seen, including those merged from other day windows."""
        return len(self.pid_set) + self.extra_pids

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
    # PID counts folded in when merging per-day aggregates (see ProcAgg).
    extra_pids: int = 0

    @property
    def pid_count(self) -> int:
        """Distinct PIDs seen, including those merged from other day windows."""
        return len(self.pid_set) + self.extra_pids

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


@dataclass
class _Window:
    """Observed atop coverage window."""

    start: str = "n/a"
    end: str = "n/a"
    distinct_samples: int = 0
    interval_s: int = 0
    seconds: int = 0
    # Raw epoch bounds, kept so multiple per-day windows can be merged by
    # min(start)/max(end) without re-parsing the ISO strings above.
    start_epoch: int = 0
    end_epoch: int = 0
