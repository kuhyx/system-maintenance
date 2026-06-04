"""atop + pmon log parsing and aggregation helpers for usage_report."""

from __future__ import annotations

import contextlib
import datetime as _dt
from pathlib import Path
import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

from _usage_report_types import (
    _MIN_SAMPLES_FOR_WINDOW,
    GpuAgg,
    ProcAgg,
    _PidCpu,
    _PidRam,
    _Progress,
    _Window,
)

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
_PMON_MIN_FIELDS = 11
_CPU_RECORD_MIN_LEN = 5
_PAREN_PAIR_MIN = 2
_ATOP_AGG_CACHE_BIN = Path.home() / ".cache" / "usage_report" / "atop_agg"
_ATOP_AGG_BIN_MODE = 0o755
# Repo layout: linux_configuration/scripts/system-maintenance/bin/usage_report.py
# -> parents[4] is the repo root which hosts the C/ source tree.
_ATOP_AGG_SRC_DIR = Path(__file__).resolve().parents[4] / "C" / "atop_agg"
_ATOP_AGG_BUILD_TIMEOUT_S = 60
_NATIVE_TSV_NAME_LEN = 7
_NATIVE_TSV_WIN_LEN = 5


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


def _atop_read_cmd(
    log: Path,
    labels: str,
    begin: str | None,
    end: str | None,
) -> list[str]:
    """Build an `atop -r` command, optionally bounded by begin/end times.

    *begin*/*end* are atop `-b`/`-e` arguments (`[YYYYMMDD]hhmm[ss]`) used to
    restrict replay to a sub-window of the day's log, so a "since last report"
    run does not double-count the part of the first day already reported.
    """
    cmd = ["atop", "-r", str(log)]
    if begin is not None:
        cmd += ["-b", begin]
    if end is not None:
        cmd += ["-e", end]
    cmd += ["-P", labels]
    return cmd


def _iter_atop_lines(
    log: Path,
    labels: str,
    begin: str | None = None,
    end: str | None = None,
) -> Iterator[str]:
    """Stream `atop -r LOG -P LABELS` stdout line-by-line.

    Uses `Popen` so the report can show progress while atop is still
    decoding its binary log, rather than buffering the whole output.
    """
    cmd = _atop_read_cmd(log, labels, begin, end)
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


def _atop_agg_binary() -> Path | None:
    """Return a cached `atop_agg` binary path, auto-building if missing/stale.

    Falls back to ``None`` when the C source tree or a system C compiler
    is unavailable, in which case callers use the pure-Python parser.
    """
    src_c = _ATOP_AGG_SRC_DIR / "atop_agg.c"
    if not src_c.exists():
        # Source tree is gone (relocated/extracted): never trust an orphaned
        # cached binary whose provenance we can no longer verify against
        # source — a stale build can silently carry parsing bugs. Fall back to
        # the pure-Python parser instead.
        return None
    if (
        _ATOP_AGG_CACHE_BIN.exists()
        and src_c.stat().st_mtime <= _ATOP_AGG_CACHE_BIN.stat().st_mtime
    ):
        return _ATOP_AGG_CACHE_BIN
    if shutil.which("cc") is None:
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
    r"""Fold one `N\t<name>\t<cpu>\t<peak>\t<sum_avg>\t<ram_n>\t<pids>` row."""
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
    r"""Build a `_Window` from a `W\t<start>\t<end>\t<n>\t<interval>` row."""
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
        start_epoch=start_epoch,
        end_epoch=end_epoch,
    )


def _aggregate_atop_native(
    log: Path,
    progress: _Progress,
    binary: Path,
    begin: str | None = None,
    end: str | None = None,
) -> tuple[dict[str, ProcAgg], _Window]:
    """Aggregate via `atop | atop_agg`; return `(by_name, window)`."""
    progress.start_stage("atop: parse PRC+PRM (native)")
    agg_map: dict[str, ProcAgg] = {}
    window = _Window()
    atop_cmd = _atop_read_cmd(log, "PRC,PRM", begin, end)
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


def _pmon_row_epoch(parts: list[str]) -> float | None:
    """Local-time epoch of a pmon row from its `date`/`time` columns, or None.

    pmon timestamps are naive local time (`YYYYMMDD HH:MM:SS`); `.astimezone()`
    attaches the local offset so the result is comparable to a `begin_epoch`
    derived the same way.
    """
    try:
        stamp = _dt.datetime.strptime(
            f"{parts[0]} {parts[1]}",
            "%Y%m%d %H:%M:%S",
        ).astimezone()
    except (ValueError, IndexError):
        return None
    return stamp.timestamp()


def aggregate_pmon(
    log: Path,
    progress: _Progress,
    begin_epoch: float | None = None,
) -> tuple[dict[str, GpuAgg], int]:
    """Return `({program: GpuAgg}, sample_count)` from the pmon *log*.

    When *begin_epoch* is set, rows timestamped before it are skipped so the
    first day of a "since last report" window starts at the previous run time.
    """
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
            if begin_epoch is not None:
                row_epoch = _pmon_row_epoch(parts)
                if row_epoch is not None and row_epoch < begin_epoch:
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
