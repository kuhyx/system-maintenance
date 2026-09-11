"""Post-move phases: rebind commands, unit restarts, commits, bridges, verify.

Planning appends actions; :func:`execute_post` runs one of them. Commits
go through ``finish_auto.sh -P`` (narrow staging, the repo's own gate, no
push) and pushes happen afterwards in one batch, so 48 CI watches are not
serialised. Bridges are symlinks left at every old directory path; the
bridgeless verify run decides whether they are dropped at once or kept on
the 14-day fuse recorded in the manifest.
"""

from __future__ import annotations

import datetime as _dt
import os
import shlex
from pathlib import Path

from _home_tidy_manifest import Manifest, write_bridges
from _home_tidy_refs import Rewriter
from _home_tidy_verify import verify

NO_BRIDGE_BUCKETS = ("inbox", "archive")
BRIDGE_DAYS = 14
COMMIT_SUBJECT = "Re-point paths for the ~ reorganisation (home-tidy)"


def _commit_order(manifest: Manifest, repos: list[str]) -> list[str]:
    first = [r for r in manifest.rebind.commit_order_first if r in repos]
    return first + sorted(r for r in repos if r not in first)


def plan_post_move(manifest: Manifest, plan, action_cls) -> None:
    """Append rebind, restart, hook-after, commit, push, bridge and verify actions."""
    rb = manifest.rebind
    for i, cmd in enumerate(rb.user):
        plan.actions.append(action_cls(f"rebind:user:{i}", "cmd", cmd, payload=(cmd, False)))
    for i, cmd in enumerate(rb.sudo):
        plan.actions.append(action_cls(f"rebind:sudo:{i}", "cmd", cmd, sudo=True, payload=(cmd, True)))
    for hook in manifest.hooks:
        for i, cmd in enumerate(hook.after):
            plan.actions.append(action_cls(f"hook-after:{hook.entry}:{i}", "cmd", cmd, sudo=hook.sudo, payload=(cmd, hook.sudo)))
    for unit in rb.restart_user_units:
        plan.actions.append(action_cls(f"restart:user:{unit}", "restart", f"restart {unit} (if active)", payload=(unit, False)))
    for unit in rb.restart_sudo_units:
        plan.actions.append(action_cls(f"restart:sudo:{unit}", "restart", f"restart {unit} (if active)", sudo=True, payload=(unit, True)))
    for rel in _commit_order(manifest, list(plan.repo_changes)):
        files = plan.repo_changes[rel]
        plan.actions.append(action_cls(f"commit:{rel}", "commit", f"commit {len(files)} file(s) in {rel}", payload=(rel, files)))
    for rel in _commit_order(manifest, list(plan.repo_changes)):
        plan.actions.append(action_cls(f"push:{rel}", "push", f"push {rel}", payload=rel))
    if plan.repo_changes:
        plan.actions.append(action_cls("ci:summary", "ci-summary", "latest CI run per pushed repo", payload=_commit_order(manifest, list(plan.repo_changes))))
    bridges = []
    for old, new in manifest.mapping.items():
        src = manifest.home / old
        if "/" in old or new.split("/")[0] in NO_BRIDGE_BUCKETS:
            continue
        if not (src.is_dir() and not src.is_symlink()) and not (manifest.home / new).is_dir():
            continue
        bridges.append(old)
        plan.actions.append(action_cls(f"bridge:{old}", "bridge", f"{old} -> {new} (bridge symlink)", payload=(old, new)))
    plan.actions.append(action_cls("bridges:record", "bridges-record", f"record {len(bridges)} bridge(s) in the manifest", payload=bridges))
    plan.actions.append(action_cls("verify:with-bridges", "verify", "verify suite (bridges present)", payload=False))
    plan.actions.append(action_cls("verify:bridgeless", "verify", "verify suite with bridges moved aside; drop them if it passes", payload=True))


def pure_rewrites(repo: Path, files: list[str], runner, rewriter) -> tuple[list[str], list[str]]:
    """Split ``files`` into (safe to commit, skipped).

    Safe = tracked, and every removed line of ``git diff`` turns into the
    matching added line under the rewriter — i.e. the whole diff is ours.
    A file the user had already edited is left for them to commit.
    """
    safe, skipped = [], []
    for f in files:
        tracked = runner.run(f"git -C {shlex.quote(str(repo))} ls-files --error-unmatch -- {shlex.quote(f)}").ok
        if not tracked:
            continue
        # Against HEAD: a file left staged by an earlier failed commit still counts.
        diff = runner.run(f"git -C {shlex.quote(str(repo))} diff HEAD -- {shlex.quote(f)}").stdout
        if not diff.strip():
            continue  # already committed
        minus = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        plus = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
        if len(minus) == len(plus) and all(rewriter.rewrite_text(m)[0] == p for m, p in zip(minus, plus)):
            safe.append(f)
        else:
            skipped.append(f)
    return safe, skipped


def _is_active(runner, unit: str, sudo: bool) -> bool:
    scope = "" if sudo else "--user "
    return runner.run(f"systemctl {scope}is-active --quiet {shlex.quote(unit)}", sudo=sudo).ok


def execute_post(action, ctx) -> None:
    """Run one post-move action; raises on a hard failure."""
    manifest, runner, log = ctx.manifest, ctx.runner, ctx.log
    if action.phase == "cmd":
        cmd, sudo = action.payload
        runner.run(cmd, sudo=sudo, check=True)
    elif action.phase == "restart":
        unit, sudo = action.payload
        if _is_active(runner, unit, sudo):
            scope = "" if sudo else "--user "
            runner.run(f"systemctl {scope}restart {shlex.quote(unit)}", sudo=sudo, check=True)
        else:
            log(f"  {unit}: not active, not restarted")
    elif action.phase == "commit":
        rel, candidates = action.payload
        repo = manifest.home / rel
        files, skipped = pure_rewrites(repo, candidates, runner, Rewriter(manifest.rewrite_mapping(), manifest.home))
        for f in skipped:
            log(f"  {rel}/{f}: has other uncommitted changes — commit by hand")
            ctx.failures.append(f"{rel}/{f}: not committed (foreign changes in the diff)")
        if not files:
            log(f"  {rel}: nothing left to commit")
            return
        if manifest.rebind.finish_script:
            script = manifest.expand(manifest.rebind.finish_script)
            argv = [str(script), "-P", "-m", COMMIT_SUBJECT, str(repo), *files]
        else:
            git = ["git", "-C", str(repo)]
            runner.run(shlex.join([*git, "add", "--", *files]), check=True)
            argv = [*git, "commit", "-m", COMMIT_SUBJECT]
            for trailer in manifest.rebind.commit_trailers:
                argv += ["--trailer", trailer]
            argv += ["--", *files]
        runner.run(shlex.join(argv), check=True)
    elif action.phase == "push":
        runner.run(f"git -C {shlex.quote(str(manifest.home / action.payload))} push", check=True)
    elif action.phase == "ci-summary":
        query = '.[0] | "\\(.status) \\(.conclusion) \\(.url)"'
        for rel in action.payload:
            res = runner.run(f"gh run list --limit 1 --json status,conclusion,url --jq {shlex.quote(query)}", cwd=manifest.home / rel)
            log(f"  {rel}: {res.stdout.strip() or 'no CI run found'}")
    elif action.phase == "bridge":
        old, new = action.payload
        link = manifest.home / old
        if not runner.dry_run and not link.exists() and not link.is_symlink():
            os.symlink(manifest.home / new, link)
    elif action.phase == "bridges-record":
        expires = (_dt.date.today() + _dt.timedelta(days=BRIDGE_DAYS)).isoformat()
        if not runner.dry_run:
            write_bridges(ctx.manifest_path, expires, list(action.payload))
            ctx.reload_manifest()
        log(f"  bridges expire {expires}")
    elif action.phase == "verify":
        verify(ctx, bridgeless=bool(action.payload))
    else:
        raise RuntimeError(f"unknown post-move phase {action.phase}")
