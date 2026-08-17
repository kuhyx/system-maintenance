"""Log discovery, report state and per-day segment planning for usage_report.

Resolves which daily atop/pmon logs a reporting window covers, persists the
"last report generated" timestamp that drives the since-last-report mode, and
describes the logs actually consumed for the report's methodology section.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as _dt
import json
from pathlib import Path

from _usage_report_types import _PMON_INTERVAL_S

_ATOP_LOG_DIR = Path("/var/log/atop")
_PMON_LOG_DIR = Path.home() / ".local/share/gpu-log"
# Persisted marker of when the last report was generated. Lives under
# ~/.local/share (durable app state), not ~/.cache, so clearing caches does not
# silently reset the "since last report" window back to today-only.
_STATE_DIR = Path.home() / ".local/share/usage_report"
_STATE_FILE = _STATE_DIR / "last_report.json"


@dataclass
class _Segment:
    """One calendar day's resolved logs plus optional in-day start bounds.

    *atop_begin* is an atop ``-b`` argument (``YYYYMMDDhhmmss``) and
    *pmon_begin_epoch* the matching local epoch; both are set only for the first
    day of a "since last report" window so re-runs do not double-count.
    """

    atop_log: Path
    pmon_log: Path
    atop_begin: str | None = None
    pmon_begin_epoch: float | None = None


def _resolve_logs(date: str) -> tuple[Path, Path]:
    atop_log = _ATOP_LOG_DIR / f"atop_{date}"
    pmon_log = _PMON_LOG_DIR / f"pmon-{date}.log"
    return atop_log, pmon_log


def _read_last_generated() -> _dt.datetime | None:
    """Return the timestamp of the previous report run, or None if unknown."""
    try:
        raw = _STATE_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        stamp = json.loads(raw)["last_generated"]
        return _dt.datetime.fromisoformat(stamp).astimezone()
    except (ValueError, KeyError, TypeError):
        return None


def _write_last_generated(when: _dt.datetime) -> None:
    """Persist *when* as the last-report timestamp for the next run."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"last_generated": when.isoformat(timespec="seconds")})
    _STATE_FILE.write_text(payload + "\n", encoding="utf-8")


def _has_time_of_day(when: _dt.datetime) -> bool:
    """True when *when* is past local midnight, so a begin bound is needed."""
    return bool(when.hour or when.minute or when.second or when.microsecond)


def _plan_segments(start: _dt.datetime, end: _dt.datetime) -> list[_Segment]:
    """Resolve one `_Segment` per calendar day across ``[start, end]``.

    The first day is bounded at *start*'s time-of-day so a same-day re-run only
    covers the slice since the previous report; later days are covered in full.
    Returns an empty list when *start* is after *end* (e.g. a future state file).
    """
    segments: list[_Segment] = []
    day = start.date()
    while day <= end.date():
        atop_log, pmon_log = _resolve_logs(day.strftime("%Y%m%d"))
        if day == start.date() and _has_time_of_day(start):
            segments.append(
                _Segment(
                    atop_log,
                    pmon_log,
                    start.strftime("%Y%m%d%H%M%S"),
                    start.timestamp(),
                ),
            )
        else:
            segments.append(_Segment(atop_log, pmon_log))
        day += _dt.timedelta(days=1)
    return segments


def _describe_logs(paths: list[Path], how: str) -> str:
    """One-line Markdown description of the log files actually consumed."""
    if not paths:
        return f"_none found_ (`{how}`)"
    if len(paths) == 1:
        return f"`{paths[0]}` (`{how}`)"
    return (
        f"{len(paths)} daily logs `{paths[0].name}` … `{paths[-1].name}` "
        f"in `{paths[0].parent}` (`{how}`)"
    )


def _log_descriptions(segments: list[_Segment]) -> tuple[str, str]:
    """Return ``(atop_desc, pmon_desc)`` for the logs present in *segments*."""
    atop_present = [seg.atop_log for seg in segments if seg.atop_log.exists()]
    pmon_present = [seg.pmon_log for seg in segments if seg.pmon_log.exists()]
    return (
        _describe_logs(atop_present, "atop -r"),
        _describe_logs(pmon_present, f"nvidia-smi pmon -d {_PMON_INTERVAL_S}"),
    )
