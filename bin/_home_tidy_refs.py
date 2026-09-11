"""Rewrite absolute references to moved entries (old path -> new path).

One regex covers every spelling of a home-relative path (``~/x``,
``$HOME/x``, ``${HOME}/x``, systemd's ``%h/x`` and the literal
``/home/<user>/x``) and rewrites only the entry component, so the original
spelling is preserved. A word boundary after the entry name keeps ``~/src/todo``
from matching ``~/todo-desktop-profile...``. Files are read and written
whole through the resolved path — never ``sed -i`` — so a symlinked file is
edited in place instead of being silently forked.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_BOUNDARY = r"""(?=[/\s"'`:;,)\]}>|&=]|$)"""
_PROBE_BYTES = 8192


@dataclass(frozen=True)
class FileChange:
    """A pending text rewrite of one file (nothing written until applied)."""

    path: Path
    new_text: str
    count: int
    diff: str
    sudo: bool = False


@dataclass(frozen=True)
class LinkChange:
    """A pending symlink retarget."""

    path: Path
    old_target: str
    new_target: str
    sudo: bool = False


class Rewriter:
    """Compiled mapping of old entry names to new home-relative paths."""

    def __init__(self, mapping: dict[str, str], home: Path) -> None:
        self.mapping = dict(mapping)
        self.home = home
        names = sorted(mapping, key=len, reverse=True)
        alternatives = "|".join(re.escape(n) for n in names)
        prefixes = "|".join(
            (r"~", r"\$\{HOME\}", r"\$HOME", r"%h", re.escape(str(home)))
        )
        self.pattern = re.compile(
            rf"(?P<prefix>{prefixes})/(?P<name>{alternatives}){_BOUNDARY}"
        )
        # Code that builds the path piecewise: ``Path.home() / "src/todo"``,
        # ``_HOME / "src/kuhylog"``, ``os.path.join(HOME, "src/utils")``.
        # Quoted segments of a nested key ("a/b") may be split across
        # ``/ "a" / "b"``; the replacement is one quoted relative path.
        pieces = []
        for n in names:
            segs = [re.escape(s) for s in n.split("/")]
            pieces.append("[\"']" + "[\"']\\s*/\\s*[\"']".join(segs) + "(?=[\"'/])")
        self.code_pattern = re.compile(
            r"(?P<prefix>(?:\.home\(\)|HOME\b)\s*[/,]\s*)(?P<seg>"
            + "|".join(pieces)
            + ")"
        )

    def _sub(self, match: re.Match[str]) -> str:
        return f"{match.group('prefix')}/{self.mapping[match.group('name')]}"

    def _sub_code(self, match: re.Match[str]) -> str:
        seg = match.group("seg")
        quote = seg[0]
        name = re.sub(r"""["']\s*/\s*["']""", "/", seg[1:])
        return f"{match.group('prefix')}{quote}{self.mapping[name]}"

    def rewrite_text(self, text: str) -> tuple[str, int]:
        """(new_text, replacements) over both the path and the code pattern."""
        text, n1 = self.pattern.subn(self._sub, text)
        text, n2 = self.code_pattern.subn(self._sub_code, text)
        return text, n1 + n2

    def map_abs(self, path: str) -> str | None:
        """New absolute path for an absolute path under a moved entry, else None."""
        new, n = self.pattern.subn(self._sub, path)
        return new if n else None


def is_text_file(path: Path, max_bytes: int) -> bool:
    """Regular, small enough, and no NUL in the first block."""
    try:
        st = path.stat()
    except OSError:
        return False
    if not path.is_file() or path.is_symlink() or st.st_size > max_bytes:
        return False
    with path.open("rb") as fh:
        return b"\0" not in fh.read(_PROBE_BYTES)


def read_via_sudo(path: Path) -> bytes | None:
    """Contents of a root-only file through ``sudo -n cat``, or None."""
    res = subprocess.run(
        ["sudo", "-n", "cat", str(path)], capture_output=True, check=False
    )
    return res.stdout if res.returncode == 0 else None


def plan_file(
    path: Path, rewriter: Rewriter, max_bytes: int, sudo: bool = False
) -> FileChange | None:
    """The change ``path`` needs, or None if it references nothing moved.

    Raises ``PermissionError`` for a root-only file that sudo cannot read
    either, so the planner can list it instead of silently skipping it.
    """
    try:
        if not is_text_file(path, max_bytes):
            return None
        raw = path.read_bytes()
    except PermissionError:
        if not sudo:
            raise
        got = read_via_sudo(path)
        if got is None:
            raise
        raw = got
        if b"\0" in raw[:_PROBE_BYTES] or len(raw) > max_bytes:
            return None
    old = raw.decode("utf-8", errors="surrogateescape")
    new, count = rewriter.rewrite_text(old)
    if not count:
        return None
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
            n=0,
        )
    )
    return FileChange(path, new, count, diff, sudo)


def plan_link(
    link: Path, rewriter: Rewriter, new_link: Path | None = None, sudo: bool = False
) -> LinkChange | None:
    """Retarget ``link`` if it points into a moved entry.

    ``new_link`` is where the link itself will live after the migration (it
    may sit inside a moved repo); relative targets are recomputed from there.
    """
    if not link.is_symlink():
        return None
    target = os.readlink(link)
    absolute = os.path.isabs(target)
    old_abs = target if absolute else os.path.normpath(link.parent / target)
    new_abs = rewriter.map_abs(old_abs)
    where = new_link or link
    if new_abs is None:
        if new_link is None or absolute:
            return None
        new_abs = old_abs  # link moved, target did not: relative path must follow
    new_target = new_abs if absolute else os.path.relpath(new_abs, where.parent)
    if new_target == target:
        return None
    return LinkChange(where, target, new_target, sudo)


def apply_file(change: FileChange, run_sudo) -> None:
    """Write a planned change; root-owned files go through ``sudo tee``."""
    data = change.new_text.encode("utf-8", errors="surrogateescape")
    if change.sudo:
        run_sudo(["tee", str(change.path)], stdin=data)
        return
    change.path.write_bytes(data)


def apply_link(change: LinkChange, run_sudo) -> None:
    """Replace a symlink's target (``ln -sfn`` semantics)."""
    if change.sudo:
        run_sudo(["ln", "-sfn", change.new_target, str(change.path)])
        return
    tmp = change.path.with_name(change.path.name + ".home-tidy-tmp")
    os.symlink(change.new_target, tmp)
    os.replace(tmp, change.path)
