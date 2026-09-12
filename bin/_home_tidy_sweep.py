"""Automatic sweep of ``~`` root into ``~/inbox`` and its undo journal.

Guardrails, all from the manifest: entries younger than the grace period
are left alone (something may still be writing them), anything over the
size/count limit is reported instead of moved, dotfiles and allowlisted
buckets are never touched, and a lock file keeps the timer out of the way
of a running migration. Nothing is ever deleted; every move is appended to
``moves.jsonl`` so ``undo`` can replay it backwards.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from _home_tidy_check import RESURRECTION_BUCKETS
from _home_tidy_freshness import counterpart_for, newer_than_counterpart
from _home_tidy_manifest import Manifest
from _home_tidy_scan import Entry, bounded_stats, list_root

LOCK_NAME = "migrate.lock"
MOVES_NAME = "moves.jsonl"
# The one-shot migration records its moves here, NOT in moves.jsonl: `undo`
# must never be able to rename 171 entries back under references that all
# point at the new paths.
MIGRATION_MOVES_NAME = "migration-moves.jsonl"
INSTALL_MARK = "installed-at"


@dataclass(frozen=True)
class Move:
    """One journaled relocation."""

    src: str
    dst: str
    ts: str
    reason: str
    batch: str = ""


def journal_path(state_dir: Path, name: str = MOVES_NAME) -> Path:
    """Where a moves journal lives."""
    return state_dir / name


def append_moves(state_dir: Path, moves: list[Move], name: str = MOVES_NAME) -> None:
    """Append to a journal (one JSON object per line)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with journal_path(state_dir, name).open("a", encoding="utf-8") as fh:
        for m in moves:
            fh.write(json.dumps(m.__dict__) + "\n")


def read_moves(state_dir: Path) -> list[Move]:
    """Every journaled move, oldest first."""
    p = journal_path(state_dir)
    if not p.exists():
        return []
    return [
        Move(**json.loads(line))
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def unique_destination(dst: Path) -> Path:
    """``dst`` or ``dst.1``, ``dst.2``... so a sweep never overwrites."""
    if not dst.exists() and not dst.is_symlink():
        return dst
    n = 1
    while True:
        cand = dst.with_name(f"{dst.name}.{n}")
        if not cand.exists() and not cand.is_symlink():
            return cand
        n += 1


def forced_dry_run(state_dir: Path, hours: int, now: float) -> bool:
    """True while the post-install dry-run window is still open."""
    mark = state_dir / INSTALL_MARK
    if not mark.exists() or hours <= 0:
        return False
    try:
        installed = float(mark.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    return now - installed < hours * 3600


def _freshness_block(manifest: Manifest, entry: Entry, max_count: int) -> str | None:
    """Why ``entry`` must not be swept, or ``None`` if sweeping is safe.

    A root entry colliding with a repo under ``src/``/``vendor/`` is a
    resurrected pre-2026-09-11 path, and the live copy of its data may be
    the one in ``~`` rather than the one in ``~/src``. Filing that under
    ``inbox/`` leaves every reader on the stale file — which is what nearly
    happened to ``~/todo/BACKLOG.md`` on 2026-09-12. Fail closed, and name
    the file that blocked the sweep so reconciling is a diff, not a hunt.
    """
    counterpart = counterpart_for(manifest.home, entry.name, RESURRECTION_BUCKETS)
    if counterpart is None:
        return None
    rel = counterpart.relative_to(manifest.home)
    newer, over = newer_than_counterpart(entry.path, counterpart, max_count)
    if over:
        return (
            f"{entry.name}: too large to compare against ~/{rel} — reconcile by hand "
            "(refusing to sweep an unverified resurrection)"
        )
    if newer is not None:
        return (
            f"{entry.name}: holds data newer than ~/{rel} ({entry.name}/{newer}) — "
            "reconcile by hand, then fix the tool still writing the old path"
        )
    return None


def sweep(
    manifest: Manifest, state_dir: Path, now: float, dry_run: bool
) -> tuple[list[Move], list[str]]:
    """Move stray root entries to inbox/. Returns (moves, skipped reasons)."""
    if (state_dir / LOCK_NAME).exists():
        return [], ["migration lock present; sweep skipped"]
    policy = manifest.sweep
    inbox = manifest.home / "inbox"
    bridges = set(manifest.bridges_paths)
    expired = False
    if manifest.bridges_expires:
        expired = _dt.date.fromtimestamp(now) >= _dt.date.fromisoformat(
            manifest.bridges_expires
        )
    moves: list[Move] = []
    skipped: list[str] = []
    ts = _dt.datetime.fromtimestamp(now).isoformat(timespec="seconds")
    batch = uuid.uuid4().hex
    for entry in list_root(manifest.home):
        if entry.name in manifest.allow:
            continue
        if entry.kind == "link" and entry.name in bridges:
            if expired:
                skipped.append(f"{entry.name}: expired bridge symlink removed")
                if not dry_run:
                    entry.path.unlink()
            continue
        if now - entry.mtime < policy.grace_minutes * 60:
            skipped.append(
                f"{entry.name}: younger than {policy.grace_minutes} min grace"
            )
            continue
        if entry.kind != "link":
            size, count, over = bounded_stats(
                entry.path, policy.max_entry_bytes, policy.max_entry_count
            )
            if over:
                skipped.append(
                    f"{entry.name}: over sweep limit ({size} B, {count} entries) — move it by hand"
                )
                continue
        blocked = _freshness_block(manifest, entry, policy.max_entry_count)
        if blocked is not None:
            skipped.append(blocked)
            continue
        dst = unique_destination(inbox / entry.name)
        moves.append(Move(str(entry.path), str(dst), ts, "root-allow", batch))
        if not dry_run:
            inbox.mkdir(exist_ok=True)
            os.rename(entry.path, dst)
    if moves and not dry_run:
        append_moves(state_dir, moves)
    return moves, skipped


def undo(state_dir: Path, which: str) -> list[Move]:
    """Reverse journaled moves: ``last`` batch, ``all``, or an ISO timestamp prefix."""
    moves = read_moves(state_dir)
    if not moves:
        return []
    if which == "last":
        selected = [m for m in moves if m.batch == moves[-1].batch]
    elif which == "all":
        selected = list(moves)
    else:
        selected = [m for m in moves if m.ts.startswith(which)]
    undone: list[Move] = []
    for m in reversed(selected):
        src, dst = Path(m.src), Path(m.dst)
        if not (dst.exists() or dst.is_symlink()) or src.exists() or src.is_symlink():
            continue
        src.parent.mkdir(parents=True, exist_ok=True)
        os.rename(dst, src)
        undone.append(m)
    remaining = [m for m in moves if m not in undone]
    p = journal_path(state_dir)
    p.write_text(
        "".join(json.dumps(m.__dict__) + "\n" for m in remaining), encoding="utf-8"
    )
    return undone
