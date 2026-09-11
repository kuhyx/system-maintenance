"""Build the one-shot migration as an ordered list of :class:`Action`.

Planning is read-only and works from the current state of the disk, so the
same function serves ``--plan`` (print it) and ``--apply`` (execute it) and
a re-run after a failure (already-journaled actions are skipped). Order is
least-coupled first: XDG fix, env, loose files, plain dirs, sdk, services,
vendor, src (home-tidy's own repo last), then rewrites, rebinds, commits,
bridges and verification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from _home_tidy_manifest import Manifest
from _home_tidy_plan_refs import plan_rewrites
from _home_tidy_rebind import plan_post_move
from _home_tidy_scan import is_mountpoint_within

BUCKET_ORDER = ("inbox", "archive", "data", "media", "games", "sdk", "services", "vendor", "src")
OWN_REPO = "src/system-maintenance"


@dataclass
class Action:
    """One step of the migration; ``id`` is the journal key."""

    id: str
    phase: str
    desc: str
    sudo: bool = False
    diff: str = ""
    payload: object = None


@dataclass
class Plan:
    """Ordered actions plus what the planner wants a human to read."""

    actions: list[Action] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    repo_changes: dict[str, list[str]] = field(default_factory=dict)

    def ids(self) -> set[str]:
        """All action ids (for uniqueness checks)."""
        return {a.id for a in self.actions}


def sanitise(rel: str) -> str:
    """Claude Code's project-dir spelling of a path component (``/`` -> ``-``)."""
    return rel.replace("/", "-")


def _entry_order(manifest: Manifest) -> list[tuple[str, str]]:
    nested = [(o, n) for o, n in manifest.ordered_mapping() if "/" in o]
    flat = [(o, n) for o, n in manifest.mapping.items() if "/" not in o]

    def key(pair: tuple[str, str]) -> tuple[int, int, str]:
        old, new = pair
        bucket = new.split("/")[0]
        rank = BUCKET_ORDER.index(bucket) if bucket in BUCKET_ORDER else len(BUCKET_ORDER)
        return (1 if new == OWN_REPO else 0, rank, old.lower())

    return nested + sorted(flat, key=key)


def _preflight(manifest: Manifest, done: set[str], plan: Plan) -> None:
    buckets = {n.split("/")[0] for n in manifest.mapping.values()}
    for b in buckets - manifest.allow:
        plan.errors.append(f"destination bucket {b!r} is not in [root].allow")
    for b in buckets & set(manifest.mapping):
        plan.errors.append(f"bucket {b!r} is also a mapping key")
    for hook in manifest.hooks:
        if hook.entry not in manifest.mapping:
            plan.errors.append(f"hook for unknown entry {hook.entry!r}")
    for old, new in manifest.mapping.items():
        src, dst = manifest.home / old, manifest.home / new
        if f"move:{old}" in done:
            continue  # resumed run: the old path may now be a bridge symlink
        if not (src.exists() or src.is_symlink()):
            plan.errors.append(f"{old}: source missing")
            continue
        if dst.exists() or dst.is_symlink():
            plan.errors.append(f"{old}: destination {new} already exists")
        if is_mountpoint_within(src):
            plan.errors.append(f"{old}: contains a mount point; unmount first")


def _backup_action(manifest: Manifest, plan: Plan) -> None:
    refs = manifest.refs
    paths = [str(manifest.expand(r)) for r in (*refs.user_roots, *refs.user_files)]
    paths += [str(manifest.expand(refs.claude_json))] if refs.claude_json else []
    paths += [str(manifest.home / ".config" / "user-dirs.dirs")]
    paths += [p for p in refs.sudo_roots if os.access(p, os.R_OK)]
    paths = [p for p in paths if "*" not in p]
    plan.actions.append(Action("backup", "backup", "tar the config locations the rewrite touches into the state dir", payload=paths))


def _xdg_env_actions(manifest: Manifest, plan: Plan) -> None:
    if manifest.xdg:
        lines = "\n".join(f'XDG_{k}_DIR="{v}"' for k, v in manifest.xdg.items())
        plan.actions.append(Action("xdg", "xdg", "rewrite ~/.config/user-dirs.dirs; user-dirs.conf enabled=False", diff=lines + "\n"))
    if manifest.env:
        lines = "\n".join(f'export {k}="{v}"' for k, v in manifest.env.items())
        plan.actions.append(Action("env", "env", "append home-tidy export block to ~/.zshenv", diff=lines + "\n"))


def _project_dirs(manifest: Manifest) -> list[tuple[Path, Path]]:
    """Claude per-project memory dirs to rename, longest mapping key wins."""
    root = manifest.expand(manifest.refs.claude_projects_dir) if manifest.refs.claude_projects_dir else None
    if root is None or not root.is_dir():
        return []
    stem = sanitise(str(manifest.home))
    keys = sorted(manifest.mapping, key=len, reverse=True)
    out = []
    for name in sorted(os.listdir(root)):
        for k in keys:
            head = f"{stem}-{sanitise(k)}"
            if name == head or name.startswith(head + "-"):
                out.append((root / name, root / (f"{stem}-{sanitise(manifest.mapping[k])}" + name[len(head):])))
                break
    return out


def _move_actions(manifest: Manifest, plan: Plan, done: set[str]) -> None:
    uid = os.getuid()
    for old, new in _entry_order(manifest):
        hook = manifest.hook_for(old)
        src = manifest.home / old
        if hook:
            for i, cmd in enumerate(hook.before):
                plan.actions.append(Action(f"hook-before:{old}:{i}", "hook-before", cmd, sudo=hook.sudo, payload=cmd))
        needs_sudo = False
        if src.exists() or src.is_symlink():
            needs_sudo = src.lstat().st_uid != uid
        plan.actions.append(Action(f"move:{old}", "move", f"{old} -> {new}", sudo=needs_sudo, payload=(old, new)))
    if "xdg" in manifest.raw:
        plan.notes.append("XDG user dirs re-pointed; xdg-user-dirs-update disabled so login cannot revert them")


def build_plan(manifest: Manifest, done: set[str] | None = None) -> Plan:
    """The full migration for the manifest, given already-completed action ids."""
    done = done or set()
    plan = Plan()
    _preflight(manifest, done, plan)
    if plan.errors:
        return plan
    _backup_action(manifest, plan)
    _xdg_env_actions(manifest, plan)
    _move_actions(manifest, plan, done)
    plan_rewrites(manifest, plan, done, Action)
    # After the rewrites: their planned paths point at the memory files'
    # current location, which this rename would otherwise invalidate.
    for src, dst in _project_dirs(manifest):
        plan.actions.append(Action(f"project:{src.name}", "project", f"{src.name} -> {dst.name}", payload=(src, dst)))
    plan_post_move(manifest, plan, Action)
    ids = [a.id for a in plan.actions]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        plan.errors.append(f"duplicate action ids: {dupes[:5]}")
    return plan
