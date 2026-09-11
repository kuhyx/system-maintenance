"""Planner preflight, apply failure policy, dry-run, resume, sudo paths."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from home_tidy_fixture import MANIFEST, build_home

from _home_tidy_apply import Context, apply_plan, execute, read_journal
from _home_tidy_manifest import load_manifest
from _home_tidy_migrate import Action, build_plan, sanitise
from _home_tidy_run import Result, Runner


class FakeRunner(Runner):
    """Records commands; answers from a table instead of a shell."""

    def __init__(self, answers: dict[str, Result] | None = None) -> None:
        super().__init__(dry_run=False, log=lambda _s: None)
        self.answers = answers or {}
        self.tar_ok = True
        self.sudo_calls: list[tuple[list[str], bytes | None]] = []

    def run(self, cmd, sudo=False, stdin=None, check=False, cwd=None):
        self.history.append(("sudo " if sudo else "") + cmd)
        if cmd.startswith("tar czf ") and self.tar_ok:
            Path(cmd.split()[2]).touch()  # the backup step checks the archive exists
        res = self.answers.get(cmd, Result(cmd, 0, "", ""))
        if check and not res.ok:
            raise RuntimeError(f"command failed: {cmd}")
        return res

    def sudo_argv(self, argv, stdin=None):
        self.sudo_calls.append((argv, stdin))
        return Result(" ".join(argv), 0, "", "")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    build_home(tmp_path)
    return tmp_path


def _m(home: Path):
    return load_manifest(home / "home-tidy.toml", home)


def test_preflight_errors(home: Path) -> None:
    p = home / "home-tidy.toml"
    t = p.read_text()
    p.write_text(t.replace('"junk.txt" = "inbox/junk.txt"', '"junk.txt" = "elsewhere/junk.txt"\n"src" = "data/src"\n"ghost" = "data/ghost"').replace('entry = "svc"', 'entry = "nope"'))
    errors = build_plan(_m(home)).errors
    assert any("elsewhere" in e for e in errors) and any("also a mapping key" in e for e in errors)
    assert any("ghost: source missing" in e for e in errors) and any("hook for unknown entry" in e for e in errors)
    p.write_text(t)
    (home / "src" / "todo").mkdir(parents=True)
    assert any("already exists" in e for e in build_plan(_m(home)).errors)
    (home / "src" / "todo").rmdir()
    assert build_plan(_m(home), done={"move:todo"}).errors == []


def test_preflight_mountpoint(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("_home_tidy_migrate.is_mountpoint_within", lambda p: p.name == "models")
    assert any("mount point" in e for e in build_plan(_m(home)).errors)


def test_plan_shape_and_sudo_owner(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getuid", lambda: 12345)  # every entry looks foreign-owned
    plan = build_plan(_m(home))
    assert not plan.errors and len(plan.ids()) == len(plan.actions)
    moves = [a for a in plan.actions if a.phase == "move"]
    assert all(a.sudo for a in moves) and moves[0].payload[0] == "wrap/inner"
    assert [a.payload[0] for a in moves][-2:] == ["todo", "utils"]
    assert plan.actions[0].phase == "backup" and "user-dirs.dirs" in plan.actions[0].payload[-1]
    assert sanitise("a/b/c") == "a-b-c"
    assert any(a.phase == "project" for a in plan.actions)


def test_no_projects_dir_and_no_xdg(home: Path) -> None:
    p = home / "home-tidy.toml"
    p.write_text(p.read_text().replace('claude_projects_dir = "~/.claude/projects"', 'claude_projects_dir = ""').replace('[xdg]\nDESKTOP = "$HOME/inbox"\nDOCUMENTS = "$HOME/Documents"\n', "").replace('[env]\nGOPATH = "$HOME/data/go"\n', ""))
    plan = build_plan(_m(home))
    phases = {a.phase for a in plan.actions}
    assert "project" not in phases and "xdg" not in phases and "env" not in phases and not plan.notes


def test_duplicate_ids_detected(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import _home_tidy_migrate as mig

    orig = mig.plan_post_move

    def twice(manifest, plan, cls):
        orig(manifest, plan, cls)
        orig(manifest, plan, cls)

    monkeypatch.setattr(mig, "plan_post_move", twice)
    assert any("duplicate action ids" in e for e in build_plan(_m(home)).errors)


def test_apply_dry_run_touches_nothing(home: Path) -> None:
    m = _m(home)
    plan = build_plan(m)
    logs: list[str] = []
    ctx = Context(m, Runner(dry_run=True, log=logs.append, home=home), home / ".state", log=logs.append)
    assert apply_plan(plan, ctx, set()) is True
    assert (home / "todo").is_dir() and not (home / "src").exists()
    assert not (home / ".state" / "migration.jsonl").exists()
    assert any("[dry-run] move" in l for l in logs) and any("[dry-run] verify" in l for l in logs)


def test_apply_abort_and_resume(home: Path) -> None:
    m = _m(home)
    plan = build_plan(m)
    runner = FakeRunner({"echo before-svc": Result("echo before-svc", 1, "", "boom")})
    ctx = Context(m, runner, home / ".state", log=lambda _s: None)
    assert apply_plan(plan, ctx, set()) is False  # hook-before is not a continue phase
    done = read_journal(home / ".state")
    assert "move:models" in done and "move:svc" not in done and not (home / ".state" / "migrate.lock").exists()
    assert (home / "data" / "models").is_dir()
    # resumed: preflight tolerates the moved entries, plan continues from the hook
    plan2 = build_plan(_m(home), done)
    assert not plan2.errors
    ctx2 = Context(_m(home), FakeRunner(), home / ".state", log=lambda _s: None)
    assert apply_plan(plan2, ctx2, done) is True
    assert (home / "services" / "svc").is_dir() and "verify:bridgeless" not in read_journal(home / ".state")


def test_execute_sudo_move_and_unknown_phase(home: Path) -> None:
    m = _m(home)
    runner = FakeRunner()
    ctx = Context(m, runner, home / ".state", log=lambda _s: None)
    execute(Action("move:models", "move", "", sudo=True, payload=("models", "data/models")), ctx)
    assert runner.sudo_calls[0][0][:2] == ["mv", "-T"]
    execute(Action("project:x", "project", "", payload=(home / "nope", home / "never")), ctx)  # missing src: no-op
    with pytest.raises(RuntimeError, match="unknown post-move phase"):
        execute(Action("weird", "weird", ""), ctx)


def test_backup_failure(home: Path) -> None:
    m = _m(home)
    runner = FakeRunner()
    runner.tar_ok = False
    ctx = Context(m, runner, home / ".state", log=lambda _s: None)
    with pytest.raises(RuntimeError, match="backup failed"):
        execute(Action("backup", "backup", "", payload=[str(home / ".zshrc")]), ctx)  # fake tar writes nothing


def test_manifest_path_follows_own_repo(home: Path) -> None:
    m = _m(home)
    ctx = Context(m, FakeRunner(), home / ".state")
    assert ctx.manifest_path == home / "home-tidy.toml"
    (home / "utils").rename(home / "src") if False else None
    moved = home / "src" / "utils"
    moved.parent.mkdir(exist_ok=True)
    os.rename(home / "utils", moved)
    manifest_in_repo = load_manifest(home / "home-tidy.toml", home)
    object.__setattr__(manifest_in_repo, "path", home / "utils" / "home-tidy.toml")
    (moved / "home-tidy.toml").write_text(MANIFEST)
    ctx = Context(manifest_in_repo, FakeRunner(), home / ".state")
    assert ctx.manifest_path == moved / "home-tidy.toml"
    ctx.reload_manifest()
    assert ctx.manifest.path == moved / "home-tidy.toml"
    object.__setattr__(ctx.manifest, "path", home / "nowhere" / "x.toml")
    assert ctx.manifest_path == home / "nowhere" / "x.toml"


def test_read_journal_ignores_failed(tmp_path: Path) -> None:
    (tmp_path / "migration.jsonl").write_text(json.dumps({"id": "a", "status": "done"}) + "\n\n" + json.dumps({"id": "b", "status": "failed"}) + "\n")
    assert read_journal(tmp_path) == {"a"}
