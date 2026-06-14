"""nvidia-smi pmon log parsing and aggregation helpers for usage_report."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from _usage_report_types import GpuAgg, _Progress

_PMON_MIN_FIELDS = 11


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
