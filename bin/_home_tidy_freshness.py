"""Does a stray root entry hold data newer than its ``~/src`` counterpart?

The sweep's job is to move clutter out of ``~``. That is safe for a stray
download and catastrophic for a *resurrected* path: on 2026-09-12 the todo
desktop wrapper was still exporting to the pre-reorganisation
``~/todo/BACKLOG.md`` while the ``todo`` MCP read ``~/src/todo/BACKLOG.md``.
Sweeping would have filed the only current copy of the backlog under
``inbox/todo/`` and left every reader on a stale file.

So before moving an entry whose name collides with a repo under ``~/src``,
compare the two trees. Any file that is newer than its counterpart -- or has
no counterpart at all -- makes the entry unsweepable, and the caller reports
it for manual reconciliation instead.

The walk is bounded exactly like :func:`bounded_stats`: this runs on an
hourly timer against an arbitrary root entry, so "too big to compare" is
answered the same way as "too big to sweep" -- refuse, report, walk no
further.
"""

from __future__ import annotations

import os
from pathlib import Path

# mtime slack, in seconds. A copy that preserves timestamps can land a few
# filesystem ticks apart; only a real edit should pin the sweep.
TOLERANCE = 2.0


def newer_than_counterpart(
    entry: Path,
    counterpart: Path,
    max_count: int,
    tolerance: float = TOLERANCE,
) -> tuple[str | None, bool]:
    """Find the first file in ``entry`` that ``counterpart`` does not have current.

    Returns ``(relative_path, over_limit)``. ``relative_path`` is ``None``
    when every file under ``entry`` is matched by a counterpart at least as
    new -- i.e. nothing would be lost by sweeping. ``over_limit`` is True
    when the walk was cut short, which the caller must also treat as
    unsweepable: an unfinished comparison is not a clean bill of health.
    """
    if not counterpart.exists():
        # Nothing to compare against; the entry is the only copy of whatever
        # it holds, so the generic root-allow sweep is the right handler.
        return None, False
    if entry.is_symlink() or not entry.is_dir():
        return (
            (None, False)
            if _counterpart_is_current(entry, counterpart, tolerance)
            else (entry.name, False)
        )

    count = 0
    for dirpath, _dirnames, filenames in os.walk(entry):
        for name in filenames:
            count += 1
            if count > max_count:
                return None, True
            src = Path(dirpath) / name
            rel = src.relative_to(entry)
            if not _counterpart_is_current(src, counterpart / rel, tolerance):
                return str(rel), False
    return None, False


def _counterpart_is_current(src: Path, dst: Path, tolerance: float) -> bool:
    """True when ``dst`` exists and is no older than ``src``."""
    try:
        src_mtime = src.lstat().st_mtime
    except OSError:
        return True  # vanished mid-walk; nothing to preserve
    try:
        dst_mtime = dst.lstat().st_mtime
    except OSError:
        return False  # no counterpart: sweeping would hide the only copy
    return dst_mtime >= src_mtime - tolerance


def counterpart_for(home: Path, name: str, buckets: tuple[str, ...]) -> Path | None:
    """Where ``~/<name>`` would live now, if a bucket already holds that name.

    A hit means the root entry is a resurrection of a pre-reorganisation
    path rather than a new stray, which is what both the check rule and the
    sweep guard key off.
    """
    for bucket in buckets:
        candidate = home / bucket / name
        if candidate.exists():
            return candidate
    return None
