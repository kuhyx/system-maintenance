"""Read-only view of the home directory for the lint and the sweep.

Everything here is cheap and side-effect free: listing the visible root
entries, classifying them, and bounded size/count walks that stop as soon
as a guardrail limit is exceeded (so a 188 GB media tree is never fully
walked just to learn it is too big to sweep).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Entry:
    """One visible entry directly under ``~``."""

    name: str
    path: Path
    kind: str  # "git" | "dir" | "file" | "link"
    mtime: float


def kind_of(path: Path) -> str:
    """Classify without following a symlink."""
    if path.is_symlink():
        return "link"
    if path.is_dir():
        return "git" if (path / ".git").exists() else "dir"
    return "file"


def list_root(home: Path) -> list[Entry]:
    """Visible entries of ``home`` (dotfiles are never home-tidy's business)."""
    entries: list[Entry] = []
    for name in sorted(os.listdir(home)):
        if name.startswith("."):
            continue
        path = home / name
        entries.append(Entry(name, path, kind_of(path), path.lstat().st_mtime))
    return entries


def is_mountpoint_within(path: Path) -> bool:
    """True if ``path`` or anything below it is a mount point.

    A mounted tree cannot be renamed away from under its mount, so the
    migration refuses such entries up front instead of failing midway.
    """
    if path.is_symlink() or not path.is_dir():
        return False
    if os.path.ismount(path):
        return True
    for dirpath, dirnames, _ in os.walk(path):
        for d in dirnames:
            if os.path.ismount(os.path.join(dirpath, d)):
                return True
    return False


def bounded_stats(path: Path, max_bytes: int, max_count: int) -> tuple[int, int, bool]:
    """(bytes, entries, over_limit) with the walk cut short once over a limit."""
    if path.is_symlink() or not path.is_dir():
        size = 0 if path.is_symlink() else path.lstat().st_size
        return size, 1, size > max_bytes
    total, count = 0, 0
    for dirpath, _dirnames, filenames in os.walk(path):
        count += 1
        for f in filenames:
            count += 1
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                continue
            if total > max_bytes or count > max_count:
                return total, count, True
        if count > max_count:
            return total, count, True
    return total, count, False


def visible_children(path: Path) -> list[str]:
    """Names of a bucket's visible children (empty if it is not a directory)."""
    if not path.is_dir():
        return []
    return sorted(n for n in os.listdir(path) if not n.startswith("."))
