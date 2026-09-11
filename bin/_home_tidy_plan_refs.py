"""Plan every reference rewrite for the migration.

Files are inspected where they are *now* (old location before ``--apply``,
new location on a resumed run) and the planned change is addressed to
where they will be afterwards, so ``--plan`` and ``--apply`` describe the
same edits. Repos also get their changed tracked files recorded so the
commit phase can stage them narrowly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from _home_tidy_locations import (
    Located,
    tracked_files,
    moved_dirs,
    repo_locations,
    sudo_locations,
    user_locations,
)
from _home_tidy_manifest import Manifest
from _home_tidy_refs import FileChange, Rewriter, plan_file, plan_link
from _home_tidy_refs_json import plan_claude_json

# Only live code buckets get commits; an archived backup clone is left as-is.
COMMIT_BUCKETS = ("src", "vendor")


def _relocated(path: Path, base_now: Path, base_new: Path) -> Path:
    """Where ``path`` (under ``base_now``) will be after the move."""
    if base_now == base_new:
        return path
    return base_new / path.relative_to(base_now)


def _plan_located(items: list[Located], rewriter: Rewriter, max_bytes: int, plan, action_cls, base_now: Path | None = None, base_new: Path | None = None, tracked: set[Path] | None = None) -> list[Path]:
    """Append rewrite/link actions; returns the changed tracked files (new paths)."""
    changed: list[Path] = []
    for item in items:
        p = item.path
        new_p = _relocated(p, base_now, base_new) if base_now else p
        if p.is_symlink():
            lc = plan_link(p, rewriter, new_link=new_p if base_now else None, sudo=item.sudo)
            if lc:
                plan.actions.append(action_cls(f"link:{new_p}", "link", f"{new_p} -> {lc.new_target}", sudo=item.sudo, diff=f"{lc.old_target} => {lc.new_target}\n", payload=lc))
            continue
        try:
            fc = plan_file(p, rewriter, max_bytes, sudo=item.sudo)
        except PermissionError:
            plan.notes.append(f"{p}: not readable even via sudo -n; inspect by hand")
            continue
        if fc is None:
            continue
        fc = FileChange(new_p, fc.new_text, fc.count, fc.diff, item.sudo)
        # The id carries a content hash: a later pass with a wider pattern
        # over an already-rewritten file is a new action, not a done one.
        digest = hashlib.sha1(fc.new_text.encode("utf-8", errors="surrogateescape")).hexdigest()[:8]
        plan.actions.append(action_cls(f"rewrite:{new_p}@{digest}", "rewrite", f"{new_p}: {fc.count} ref(s)", sudo=item.sudo, diff=fc.diff, payload=fc))
        if tracked is not None and p in tracked:
            changed.append(new_p)
    return changed


def _keep(items: list[Located], skip: tuple[Path, ...]) -> list[Located]:
    """Drop located files under any manifest ``skip_paths`` entry."""
    if not skip:
        return items
    return [i for i in items if not any(i.path == s or s in i.path.parents for s in skip)]


def plan_rewrites(manifest: Manifest, plan, done: set[str], action_cls) -> None:
    """Add every rewrite, link and JSON action to ``plan``."""
    rewriter = Rewriter(manifest.rewrite_mapping(), manifest.home)
    max_bytes = manifest.refs.max_file_bytes
    skip = tuple(manifest.expand(s) for s in manifest.refs.skip_paths)
    _plan_located(_keep(user_locations(manifest), skip), rewriter, max_bytes, plan, action_cls)
    _plan_located(_keep(sudo_locations(manifest), skip), rewriter, max_bytes, plan, action_cls)
    if manifest.refs.claude_json:
        jc = plan_claude_json(manifest.expand(manifest.refs.claude_json), manifest.refs.claude_json_keys, rewriter)
        if jc:
            plan.actions.append(action_cls("json:claude", "json", jc.diff.strip(), diff=jc.diff, payload=jc))
    repo_changes: dict[str, list[str]] = {}
    for _old_rel, now, new_abs in moved_dirs(manifest, done):
        is_git = (now / ".git").exists()
        globs = manifest.refs.repo_untracked_globs if is_git else manifest.refs.nongit_globs
        items = _keep(repo_locations(now, globs), skip)
        tracked = set(tracked_files(now)) if is_git else set()
        changed = _plan_located(items, rewriter, max_bytes, plan, action_cls, now, new_abs, tracked)
        rel = str(new_abs.relative_to(manifest.home))
        if changed and rel.split("/")[0] in COMMIT_BUCKETS:
            repo_changes[rel] = [str(c.relative_to(new_abs)) for c in changed]
    _merge_journaled(manifest, done, repo_changes)
    plan.repo_changes = repo_changes


def _merge_journaled(manifest: Manifest, done: set[str], repo_changes: dict[str, list[str]]) -> None:
    """Rewrites already applied on an earlier run still need committing.

    A resumed run sees no pending change in those files, so the journal is
    the only record of which tracked files this tool touched.
    """
    home = str(manifest.home) + "/"
    for action_id in sorted(done):
        if not action_id.startswith("rewrite:" + home):
            continue
        rel = action_id[len("rewrite:") + len(home):].rsplit("@", 1)[0]
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] not in COMMIT_BUCKETS:
            continue
        repo, file = "/".join(parts[:2]), "/".join(parts[2:])
        if file.startswith((".git/", ".venv/", "venv/", ".ci-mirror-venv/")):
            continue
        files = repo_changes.setdefault(repo, [])
        if file not in files:
            files.append(file)
