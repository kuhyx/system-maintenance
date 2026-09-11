"""Enumerate the files and symlinks the reference rewrite must look at.

Three sources: user-owned config roots (walked, with caches pruned),
root-owned roots (walked, written back through sudo), and every moved
repository (``git ls-files`` plus the untracked-but-live globs from the
manifest — venv shebangs, ``.git/hooks``, ``.env``, ``local.properties``).
Enumeration is read-only; the rewrite decides what actually changes.
"""

from __future__ import annotations

import glob
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from _home_tidy_manifest import Manifest



@dataclass(frozen=True)
class Located:
    """A file or symlink to inspect, plus whether writing it needs sudo."""

    path: Path
    sudo: bool = False


def _walk(root: Path, sudo: bool, prune: frozenset[str], suffixes: tuple[str, ...]) -> list[Located]:
    out: list[Located] = []
    if root.is_file() or root.is_symlink():
        return [Located(root, sudo)]
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in prune)
        for name in sorted(filenames):
            if not name.endswith(suffixes):
                out.append(Located(Path(dirpath) / name, sudo))
        # os.walk lists symlinks-to-dirs under dirnames; keep them as links.
        for name in list(dirnames):
            p = Path(dirpath) / name
            if p.is_symlink():
                out.append(Located(p, sudo))
                dirnames.remove(name)
    return out


def user_locations(manifest: Manifest) -> list[Located]:
    """Files under the user-owned roots, files and globs from the manifest."""
    out: list[Located] = []
    for root in manifest.refs.user_roots:
        for hit in sorted(glob.glob(str(manifest.expand(root)))):
            out.extend(_walk(Path(hit), False, manifest.refs.prune_dirs, manifest.refs.prune_suffixes))
    for f in manifest.refs.user_files:
        p = manifest.expand(f)
        if p.is_file():
            out.append(Located(p))
    for pattern in manifest.refs.user_globs:
        for hit in sorted(glob.glob(str(manifest.expand(pattern)))):
            out.append(Located(Path(hit)))
    return out


def sudo_locations(manifest: Manifest) -> list[Located]:
    """Files under the root-owned roots (read as the user, written via sudo)."""
    out: list[Located] = []
    for root in manifest.refs.sudo_roots:
        out.extend(_walk(Path(root), True, manifest.refs.prune_dirs, manifest.refs.prune_suffixes))
    return out


def tracked_files(repo: Path) -> list[Path]:
    if not (repo / ".git").exists():
        return []
    res = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        return []
    return [repo / p.decode("utf-8", errors="surrogateescape") for p in res.stdout.split(b"\0") if p]


def repo_locations(repo: Path, untracked_globs: tuple[str, ...]) -> list[Located]:
    """Tracked files plus the untracked-but-live globs, deduplicated."""
    seen: set[Path] = set()
    out: list[Located] = []
    candidates = tracked_files(repo)
    for pattern in untracked_globs:
        candidates.extend(Path(h) for h in sorted(glob.glob(str(repo / pattern), recursive=False)))
    for p in candidates:
        if p in seen or p.is_dir() and not p.is_symlink():
            continue
        seen.add(p)
        out.append(Located(p))
    return out


def locate(manifest: Manifest, old: str, done: set[str]) -> Path | None:
    """Where ``old`` currently lives: old path, or new path once moved."""
    src = manifest.home / old
    if src.exists() or src.is_symlink():
        return src
    dst = manifest.home / manifest.mapping[old]
    if f"move:{old}" in done and (dst.exists() or dst.is_symlink()):
        return dst
    return None


def moved_dirs(manifest: Manifest, done: set[str]) -> list[tuple[str, Path, Path]]:
    """(old_rel, current_abs, new_abs) for every mapping entry that is a directory."""
    triples = []
    for old, new in manifest.ordered_mapping():
        now = locate(manifest, old, done)
        if now is not None and now.is_dir() and not now.is_symlink():
            triples.append((old, now, manifest.home / new))
    return triples
