"""The lint: deterministic rules over ``~`` with an exit code.

Each rule yields :class:`Violation` objects; ``warn`` violations never fail
the run. The report is written verbatim to the login nag file, so wording
is terse and every line names the path to act on.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from pathlib import Path

from _home_tidy_manifest import Manifest
from _home_tidy_scan import list_root, visible_children

_HOME_DIR_LINE = re.compile(r'^XDG_([A-Z]+)_DIR="\$HOME/"\s*$', re.MULTILINE)


@dataclass(frozen=True)
class Violation:
    """One finding; ``warn`` findings are informational."""

    rule: str
    path: str
    detail: str
    warn: bool = False

    def line(self) -> str:
        """Render as a single report line."""
        tag = "warn" if self.warn else "FAIL"
        return f"[{tag}] {self.rule}: {self.path} — {self.detail}"


def _rule_root_allow(manifest: Manifest) -> list[Violation]:
    bridges = set(manifest.bridges_paths)
    out = []
    for entry in list_root(manifest.home):
        if entry.name in manifest.allow:
            continue
        if entry.kind == "link" and entry.name in bridges:
            continue  # bridge symlinks are judged by _rule_bridges
        out.append(Violation("root-allow", entry.name, "not in the root allowlist; sweep moves it to inbox/"))
    return out


def _rule_bridges(manifest: Manifest, today: _dt.date) -> list[Violation]:
    if not manifest.bridges_paths:
        return []
    expires = _dt.date.fromisoformat(manifest.bridges_expires)
    out = []
    for name in manifest.bridges_paths:
        path = manifest.home / name
        if not path.is_symlink():
            continue
        if today >= expires:
            out.append(Violation("bridge-expired", name, f"bridge symlink expired {expires}; sweep removes it"))
        else:
            out.append(Violation("bridge", name, f"bridge symlink, expires {expires}", warn=True))
    return out


def _rule_git_only(manifest: Manifest) -> list[Violation]:
    out = []
    for bucket in manifest.git_only:
        for child in visible_children(manifest.home / bucket):
            path = manifest.home / bucket / child
            if path.is_dir() and not (path / ".git").exists():
                out.append(Violation("git-only", f"{bucket}/{child}", "not a git repository"))
    return out


def _rule_xdg(manifest: Manifest) -> list[Violation]:
    conf = manifest.home / ".config" / "user-dirs.dirs"
    if not conf.exists():
        return []
    return [
        Violation("xdg-home", f"XDG_{m.group(1)}_DIR", "points at $HOME/ — root clutter source")
        for m in _HOME_DIR_LINE.finditer(conf.read_text(encoding="utf-8"))
    ]


def _rule_inbox(manifest: Manifest, now: float) -> list[Violation]:
    inbox = manifest.home / "inbox"
    limit = manifest.sweep.inbox_nag_days * 86400
    out = []
    for child in visible_children(inbox):
        if child == "keep":
            continue
        age = now - (inbox / child).lstat().st_mtime
        if age > limit:
            out.append(Violation("inbox-stale", f"inbox/{child}", f"{int(age // 86400)} days old", warn=True))
    return out


def _rule_bucket_size(manifest: Manifest) -> list[Violation]:
    out = []
    for bucket in sorted(manifest.allow):
        n = len(visible_children(manifest.home / bucket))
        if n > manifest.warn_bucket_size:
            out.append(Violation("bucket-size", bucket, f"{n} entries > {manifest.warn_bucket_size}", warn=True))
    return out


def run_check(manifest: Manifest, now: float | None = None) -> list[Violation]:
    """Every rule, in report order."""
    ts = _dt.datetime.now().timestamp() if now is None else now
    today = _dt.date.fromtimestamp(ts)
    return (
        _rule_root_allow(manifest)
        + _rule_bridges(manifest, today)
        + _rule_git_only(manifest)
        + _rule_xdg(manifest)
        + _rule_inbox(manifest, ts)
        + _rule_bucket_size(manifest)
    )


NAG_WARN_RULES = frozenset({"inbox-stale"})


def format_report(violations: list[Violation], nag: bool = False) -> str:
    """Multi-line report; empty when there is nothing to say.

    ``nag=True`` is the login-time rendering: every failure, the actionable
    warnings (stale inbox items) in full, and everything else collapsed to
    one summary line — a nag that prints 100 lines every login gets ignored.
    """
    if not violations:
        return ""
    fails = [v for v in violations if not v.warn]
    warns = [v for v in violations if v.warn]
    head = f"home-tidy: {len(fails)} failure(s), {len(warns)} warning(s)"
    if not nag:
        return "\n".join([head, *(v.line() for v in violations)]) + "\n"
    stale = [v for v in warns if v.rule in NAG_WARN_RULES]
    other = [v for v in warns if v.rule not in NAG_WARN_RULES]
    lines = [head, *(v.line() for v in fails)]
    if stale:
        oldest = max(stale, key=lambda v: int(v.detail.split()[0]))
        lines.append(f"[warn] inbox: {len(stale)} item(s) older than the nag age (oldest {oldest.path}, {oldest.detail}) — `ls -lt ~/inbox`")
    if other:
        by_rule = sorted({v.rule for v in other})
        lines.append(f"[warn] {len(other)} more ({', '.join(by_rule)}) — run `home_tidy.py check`")
    return "\n".join(lines) + "\n"


def write_report(state_dir: Path, text: str) -> Path:
    """Persist the report for the zero-fork login nag."""
    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / "report.txt"
    out.write_text(text, encoding="utf-8")
    return out
