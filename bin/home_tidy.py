#!/usr/bin/env python3
"""Keep ``~`` readable: lint it, sweep strays to inbox/, migrate it once.

    home_tidy.py check                 # exit 1 on any failure; writes the login nag
    home_tidy.py sweep [--dry-run]     # stray root entries -> ~/inbox (journaled)
    home_tidy.py undo [--last|--all|<ts-prefix>]
    home_tidy.py migrate --plan        # read-only: moves, diffs, sudo block
    home_tidy.py migrate --apply --yes # resumable; journaled; commits via finish_auto

Manifest: home-tidy.toml next to this repo's root (override with --manifest).
State: ~/.local/state/home-tidy/ (report.txt, moves.jsonl, migration.jsonl).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

from _home_tidy_apply import Context, apply_plan, read_journal
from _home_tidy_check import format_report, run_check, write_report
from _home_tidy_manifest import load_manifest
from _home_tidy_migrate import build_plan
from _home_tidy_report import render_plan
from _home_tidy_run import Runner
from _home_tidy_sweep import forced_dry_run, sweep, undo

_HERE = Path(__file__).resolve().parent


def default_manifest() -> Path:
    """``home-tidy.toml`` at the repo root, resolved from this file's location."""
    return _HERE.parent / "home-tidy.toml"


def default_state_dir(home: Path) -> Path:
    """XDG state dir for reports and journals."""
    return home / ".local" / "state" / "home-tidy"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, default=None, help="manifest path (default: repo root)")
    parser.add_argument("--home", type=Path, default=None, help="home directory (default: ~)")
    parser.add_argument("--state-dir", type=Path, default=None, help="state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="lint ~ and write the login nag")
    p_sweep = sub.add_parser("sweep", help="move stray root entries to inbox/")
    p_sweep.add_argument("--dry-run", action="store_true")
    p_undo = sub.add_parser("undo", help="reverse journaled moves")
    p_undo.add_argument("which", nargs="?", default="last", help="last | all | ISO timestamp prefix")
    p_mig = sub.add_parser("migrate", help="the one-shot reorganisation")
    group = p_mig.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true", help="print the plan, change nothing")
    group.add_argument("--apply", action="store_true", help="execute the plan")
    p_mig.add_argument("--yes", action="store_true", help="required with --apply")
    p_mig.add_argument("--dry-run", action="store_true", help="with --apply: log commands, run nothing")
    return parser


def cmd_check(manifest, state_dir: Path) -> int:
    """Lint; the report is printed and persisted for the shell nag."""
    violations = run_check(manifest)
    write_report(state_dir, format_report(violations, nag=True))
    sys.stdout.write(format_report(violations) or "home-tidy: clean\n")
    return 1 if any(not v.warn for v in violations) else 0


def cmd_sweep(manifest, state_dir: Path, dry_run: bool) -> int:
    """Sweep, then refresh the report so the nag reflects the new state."""
    now = _dt.datetime.now().timestamp()
    forced = forced_dry_run(state_dir, manifest.sweep.dry_run_hours_after_install, now)
    dry = dry_run or forced
    if forced and not dry_run:
        print("sweep: still inside the post-install dry-run window")
    moves, skipped = sweep(manifest, state_dir, now, dry)
    for m in moves:
        print(f"{'would move' if dry else 'moved'}: {m.src} -> {m.dst}")
    for s in skipped:
        print(f"skipped: {s}")
    return cmd_check(manifest, state_dir)


def cmd_undo(state_dir: Path, which: str) -> int:
    """Replay journaled moves backwards."""
    undone = undo(state_dir, which)
    for m in undone:
        print(f"restored: {m.dst} -> {m.src}")
    print(f"undo: {len(undone)} move(s) restored")
    return 0


def cmd_migrate(manifest, state_dir: Path, args: argparse.Namespace) -> int:
    """Plan or apply the reorganisation."""
    done = read_journal(state_dir)
    plan = build_plan(manifest, done)
    sys.stdout.write(render_plan(plan, done, state_dir / "plan.diff"))
    if plan.errors:
        return 2
    if args.plan:
        return 0
    if not args.yes:
        print("migrate --apply needs --yes (read the plan first)")
        return 2
    runner = Runner(dry_run=args.dry_run, home=manifest.home)
    if any(a.sudo for a in plan.actions if a.id not in done) and not runner.sudo_available():
        print("sudo is not available without a prompt; run `sudo -v` in a terminal and re-run")
        return 3
    ctx = Context(manifest, runner, state_dir)
    completed = apply_plan(plan, ctx, done)
    for f in ctx.failures:
        print(f"unresolved: {f}")
    if completed and not ctx.failures:
        print("migration complete")
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = _build_parser().parse_args(argv)
    home = (args.home or Path.home()).resolve()
    state_dir = args.state_dir or default_state_dir(home)
    if args.command == "undo":
        return cmd_undo(state_dir, args.which)  # needs no manifest (it may itself have moved)
    manifest = load_manifest(args.manifest or default_manifest(), home)
    if args.command == "check":
        return cmd_check(manifest, state_dir)
    if args.command == "sweep":
        return cmd_sweep(manifest, state_dir, args.dry_run)
    return cmd_migrate(manifest, state_dir, args)


if __name__ == "__main__":
    sys.exit(main())
