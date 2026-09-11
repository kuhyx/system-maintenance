"""Execute a migration :class:`Plan` with a resumable journal.

Every action id is appended to ``migration.jsonl`` when it completes, so a
re-run skips it. Commit/push failures (a dirty repo failing its own gate)
are recorded and the run continues; any other failure stops the run at
that action, leaving a resumable state and a clear message. A lock file
keeps the sweep timer out while this runs.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from _home_tidy_manifest import Manifest, load_manifest
from _home_tidy_rebind import execute_post
from _home_tidy_refs import Rewriter, apply_file, apply_link
from _home_tidy_sweep import LOCK_NAME, Move, append_moves
from _home_tidy_verify import ensure_baseline

JOURNAL_NAME = "migration.jsonl"
# One unwritable file or one repo failing its gate is reported at the end
# rather than stranding the run half-applied.
CONTINUE_ON_FAIL = frozenset({"commit", "push", "rewrite", "link", "json"})
# Never journaled as done: commits and pushes are idempotent (nothing to
# commit is a no-op) and a later rewrite pass needs them again; a re-run
# must always re-verify and re-summarise.
ALWAYS_RERUN = frozenset({"commit", "push", "verify", "ci-summary"})
ENV_BEGIN, ENV_END = "# >>> home-tidy >>>", "# <<< home-tidy <<<"


@dataclass
class Context:
    """What executors need: manifest, runner, state dir, log sink."""

    manifest: Manifest
    runner: object
    state_dir: Path
    log: object = print
    failures: list[str] = field(default_factory=list)
    baseline: dict = field(default_factory=dict)
    batch: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def manifest_path(self) -> Path:
        """The manifest file, wherever it is now (its own repo may have moved)."""
        path = self.manifest.path
        if path.exists():
            return path
        mapped = Rewriter(self.manifest.mapping, self.manifest.home).map_abs(str(path))
        return Path(mapped) if mapped else path

    def reload_manifest(self) -> None:
        """Pick up a manifest edited by an executor (bridge bookkeeping)."""
        self.manifest = load_manifest(self.manifest_path, self.manifest.home)


def read_journal(state_dir: Path) -> set[str]:
    """Ids of actions already completed."""
    p = state_dir / JOURNAL_NAME
    if not p.exists():
        return set()
    done = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            if rec.get("status") == "done":
                done.add(rec["id"])
    return done


def _journal(state_dir: Path, action_id: str, status: str, note: str = "") -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": action_id,
        "status": status,
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
    with (state_dir / JOURNAL_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _write_xdg(manifest: Manifest) -> None:
    conf = manifest.home / ".config" / "user-dirs.dirs"
    text = conf.read_text(encoding="utf-8") if conf.exists() else ""
    for key, value in manifest.xdg.items():
        line = f'XDG_{key}_DIR="{value}"'
        pattern = re.compile(rf"^XDG_{key}_DIR=.*$", re.MULTILINE)
        text = (
            pattern.sub(line, text, count=1)
            if pattern.search(text)
            else text.rstrip("\n") + f"\n{line}\n"
        )
        manifest.expand(value).mkdir(parents=True, exist_ok=True)
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(text, encoding="utf-8")
    (conf.parent / "user-dirs.conf").write_text("enabled=False\n", encoding="utf-8")


def _write_env(manifest: Manifest) -> None:
    rc = manifest.home / ".zshenv"
    text = rc.read_text(encoding="utf-8") if rc.exists() else ""
    block = (
        "\n".join(
            [
                ENV_BEGIN,
                *(f'export {k}="{v}"' for k, v in manifest.env.items()),
                ENV_END,
            ]
        )
        + "\n"
    )
    pattern = re.compile(
        re.escape(ENV_BEGIN) + r".*?" + re.escape(ENV_END) + r"\n?", re.DOTALL
    )
    text = (
        pattern.sub(block, text, count=1)
        if pattern.search(text)
        else text.rstrip("\n") + "\n\n" + block
    )
    rc.write_text(text, encoding="utf-8")


def _move(ctx: Context, old: str, new: str, sudo: bool) -> None:
    src, dst = ctx.manifest.home / old, ctx.manifest.home / new
    dst.parent.mkdir(parents=True, exist_ok=True)
    if sudo:
        ctx.runner.sudo_argv(["mv", "-T", str(src), str(dst)])
    else:
        os.rename(src, dst)
    ts = _dt.datetime.now().isoformat(timespec="seconds")
    append_moves(ctx.state_dir, [Move(str(src), str(dst), ts, "migrate", ctx.batch)])


FS_PHASES = frozenset(
    {"backup", "xdg", "env", "move", "project", "rewrite", "json", "link"}
)


def _backup(ctx: Context, paths: list[str]) -> None:
    """Tarball of every config location the rewrite touches (cheap insurance)."""
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ctx.state_dir / f"pre-apply-{ts}.tar.gz"
    existing = [p for p in paths if Path(p).exists()]
    # Root-only files under the sudo roots are skipped with a warning, not
    # a failure: the user-owned config is what the rewrite can damage.
    cmd = (
        "tar czf "
        + shlex.quote(str(out))
        + " --ignore-failed-read --warning=no-file-changed -- "
        + " ".join(shlex.quote(p) for p in existing)
    )
    res = ctx.runner.run(cmd)
    if res.returncode > 1 or not out.exists():
        raise RuntimeError(
            f"backup failed ({res.returncode}): {res.stderr.strip()[:500]}"
        )
    ctx.log(f"  backup written: {out}")


def execute(action, ctx: Context) -> None:
    """Run one action of any phase (filesystem phases are skipped on dry-run)."""
    if ctx.runner.dry_run and action.phase in FS_PHASES:
        ctx.log(f"  [dry-run] {action.phase}: {action.desc}")
        return
    if action.phase == "backup":
        _backup(ctx, action.payload)
    elif action.phase == "xdg":
        _write_xdg(ctx.manifest)
    elif action.phase == "env":
        _write_env(ctx.manifest)
    elif action.phase == "hook-before":
        ctx.runner.run(action.payload, sudo=action.sudo, check=True)
    elif action.phase == "move":
        old, new = action.payload
        _move(ctx, old, new, action.sudo)
    elif action.phase == "project":
        src, dst = action.payload
        if src.exists():
            os.rename(src, dst)
    elif action.phase in ("rewrite", "json"):
        apply_file(action.payload, ctx.runner.sudo_argv)
    elif action.phase == "link":
        apply_link(action.payload, ctx.runner.sudo_argv)
    else:
        execute_post(action, ctx)


def apply_plan(plan, ctx: Context, done: set[str]) -> bool:
    """Run every not-yet-done action. Returns True if the run completed."""
    lock = ctx.state_dir / LOCK_NAME
    ctx.state_dir.mkdir(parents=True, exist_ok=True)
    ctx.baseline = ensure_baseline(ctx)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        for action in plan.actions:
            if action.id in done and action.phase not in ALWAYS_RERUN:
                continue
            ctx.log(f"== {action.phase}: {action.desc}")
            try:
                execute(action, ctx)
            # Broad on purpose: every failure is journaled, then policy decides.
            except Exception as exc:
                if not ctx.runner.dry_run:
                    _journal(ctx.state_dir, action.id, "failed", str(exc))
                if action.phase in CONTINUE_ON_FAIL:
                    ctx.failures.append(f"{action.id}: {exc}")
                    ctx.log(f"  FAILED (continuing): {exc}")
                    continue
                ctx.log(
                    f"  FAILED: {exc}\n  re-run `migrate --apply` to resume from here"
                )
                return False
            if not ctx.runner.dry_run and action.phase not in ALWAYS_RERUN:
                _journal(ctx.state_dir, action.id, "done")
    finally:
        lock.unlink(missing_ok=True)
    return True
