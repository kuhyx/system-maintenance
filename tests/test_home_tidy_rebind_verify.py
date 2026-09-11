"""Post-move executors (commits, restarts, CI summary, bridges) and verify."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from home_tidy_fixture import MANIFEST, build_home
from test_home_tidy_migrate import FakeRunner

from _home_tidy_apply import Context
from _home_tidy_manifest import load_manifest, write_bridges
from _home_tidy_migrate import Action, build_plan
from _home_tidy_rebind import execute_post, plan_post_move, pure_rewrites
from _home_tidy_refs import Rewriter
from _home_tidy_run import Result
from _home_tidy_verify import bridges_aside, ensure_baseline, mcp_commands, run_suite, verify


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    build_home(tmp_path)
    return tmp_path


def _ctx(home: Path, runner=None, **toml_edits: str) -> Context:
    p = home / "home-tidy.toml"
    t = p.read_text()
    for old, new in toml_edits.items():
        t = t.replace(old, new)
    p.write_text(t)
    return Context(load_manifest(p, home), runner or FakeRunner(), home / ".state", log=lambda _s: None)


def test_plan_post_move_ordering(home: Path) -> None:
    ctx = _ctx(home, **{'restart_user_units = []': 'restart_user_units = ["a.service"]', 'restart_sudo_units = []': 'restart_sudo_units = ["b.service"]'})
    plan = build_plan(ctx.manifest)
    phases = [a.phase for a in plan.actions]
    assert phases.index("cmd") < phases.index("restart") < phases.index("commit") < phases.index("push") < phases.index("ci-summary") < phases.index("bridge")
    restarts = [a for a in plan.actions if a.phase == "restart"]
    assert [a.sudo for a in restarts] == [False, True]
    assert [a.payload[0] for a in plan.actions if a.phase == "commit"][:2] == ["src/inner", "src/todo"]
    # commit_order_first puts utils first when it has changes
    plan.repo_changes = {"src/todo": ["a"], "src/utils": ["b"]}
    plan.actions = []
    plan_post_move(ctx.manifest, plan, Action)
    assert [a.payload[0] for a in plan.actions if a.phase == "commit"] == ["src/utils", "src/todo"]


def test_restart_only_when_active(home: Path) -> None:
    runner = FakeRunner({"systemctl --user is-active --quiet a.service": Result("", 3, "", "")})
    ctx = _ctx(home, runner)
    execute_post(Action("r", "restart", "", payload=("a.service", False)), ctx)
    assert not any("restart" in h for h in runner.history)
    execute_post(Action("r", "restart", "", sudo=True, payload=("b.service", True)), ctx)
    assert "sudo systemctl restart b.service" in runner.history


def test_commit_paths(home: Path) -> None:
    repo = home / "todo"
    rw = Rewriter({"utils": "src/utils", "todo": "src/todo", "gone": "src/utils/gone"}, home)
    readme = repo / "README.md"
    readme.write_text(rw.rewrite_text(readme.read_text())[0])  # exactly our rewrite
    (repo / "run.sh").write_text("#!/bin/bash\ncd $HOME/src/todo\necho extra\n")  # ours + foreign
    real = FakeRunner()
    real.run = lambda cmd, sudo=False, stdin=None, check=False, cwd=None: Result(cmd, *_sh(cmd))  # type: ignore[method-assign]
    safe, skipped = pure_rewrites(repo, ["README.md", "run.sh", ".env", "untouched.txt"], real, rw)
    assert safe == ["README.md"] and skipped == ["run.sh"]
    ctx = _ctx(home, real, **{'finish_script = "~/fake_finish.sh"': 'finish_script = ""\ncommit_trailers = ["X: y"]'})
    execute_post(Action("c", "commit", "", payload=("todo", ["README.md", "run.sh", "nothing.txt"])), ctx)
    assert any("run.sh: not committed" in f for f in ctx.failures)
    log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%B"], capture_output=True, text=True, check=True).stdout
    assert "X: y" in log
    execute_post(Action("c", "commit", "", payload=("todo", ["README.md"])), ctx)  # nothing left: no-op
    ctx2 = _ctx(home, real, **{'finish_script = ""\ncommit_trailers = ["X: y"]': 'finish_script = "~/fake_finish.sh"'})
    (repo / "extra.md").write_text("~/src/utils/x\n")
    subprocess.run(["git", "-C", str(repo), "add", "extra.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "extra"], check=True)
    (repo / "extra.md").write_text("~/src/utils/x\n")
    execute_post(Action("c", "commit", "", payload=("todo", ["extra.md"])), ctx2)  # finish_script route
    assert subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"], capture_output=True, text=True, check=True).stdout.strip() == "rewrite"


def _sh(cmd: str) -> tuple[int, str, str]:
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=False)
    return p.returncode, p.stdout, p.stderr


def test_push_ci_bridges(home: Path) -> None:
    runner = FakeRunner()
    ctx = _ctx(home, runner)
    execute_post(Action("p", "push", "", payload="todo"), ctx)
    assert runner.history[-1].endswith("push")
    execute_post(Action("ci", "ci-summary", "", payload=["todo"]), ctx)
    assert "gh run list" in runner.history[-1]
    execute_post(Action("b", "bridge", "", payload=("gone", "src/gone")), ctx)
    assert os.readlink(home / "gone") == str(home / "src" / "gone")
    execute_post(Action("b", "bridge", "", payload=("gone", "src/gone")), ctx)  # idempotent
    execute_post(Action("br", "bridges-record", "", payload=["gone"]), ctx)
    assert ctx.manifest.bridges_paths == ("gone",) and ctx.manifest.bridges_expires
    dry = _ctx(home, FakeRunner())
    dry.runner.dry_run = True
    execute_post(Action("b", "bridge", "", payload=("dry", "src/dry")), dry)
    execute_post(Action("br", "bridges-record", "", payload=["dry"]), dry)
    assert not (home / "dry").exists()


def test_baseline_and_suite(home: Path, tmp_path: Path) -> None:
    answers = {
        "systemctl --user list-units --state=active --no-legend --plain": Result("", 0, "a.service loaded active running x\n", ""),
        "systemctl --user list-units --state=failed --no-legend --plain": Result("", 0, "old.service loaded failed failed x\n", ""),
        "systemctl list-units --state=active --no-legend --plain": Result("", 0, "b.service loaded active running x\n", ""),
        "systemctl list-units --state=failed --no-legend --plain": Result("", 0, "", ""),
    }
    runner = FakeRunner(answers)
    ctx = _ctx(home, runner, **{'restart_user_units = []': 'restart_user_units = ["a.service", "z.service"]', 'restart_sudo_units = []': 'restart_sudo_units = ["b.service"]'})
    base = ensure_baseline(ctx)
    assert base == {"failed_user": ["old.service"], "failed_sudo": [], "active_user": ["a.service"], "active_sudo": ["b.service"]}
    assert ensure_baseline(ctx) == base  # cached on disk
    runner.answers["systemctl --user list-units --state=failed --no-legend --plain"] = Result("", 0, "old.service x\nnew.service x\n", "")
    runner.answers["systemctl --user is-active --quiet a.service"] = Result("", 3, "", "")
    runner.answers["test -f ~/src/todo/README.md"] = Result("", 1, "", "")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"m": {"command": "/nonexistent/x"}, "rel": {"command": "uv"}}, "projects": {"p": {"mcpServers": {"q": {"command": "/bin/sh"}}}, "s": "str"}}))
    failed = run_suite(ctx.manifest, runner, base)
    assert "newly failed unit: new.service" in failed and "unit no longer active: a.service" in failed
    assert "MCP command not executable: /nonexistent/x" in failed and "test -f ~/src/todo/README.md" in failed
    assert mcp_commands(tmp_path / "missing.json") == []
    ctx2 = _ctx(home, FakeRunner({"exit 1": Result("", 1, "", "")}), **{'[verify]\nuser = ["test -f ~/src/todo/README.md", "test -L ~/.local/bin/tool"]\nsudo = []': '[verify]\nuser = []\nsudo = ["exit 1"]'})
    (home / ".claude.json").unlink()
    assert run_suite(ctx2.manifest, ctx2.runner, {}) == ["sudo exit 1"]


def test_verify_decides_bridges(home: Path) -> None:
    os.symlink(home / "src", home / "b1")
    write_bridges(home / "home-tidy.toml", "2030-01-01", ["b1", "b2"])
    ctx = _ctx(home, FakeRunner(), **{'user = ["test -f ~/src/todo/README.md", "test -L ~/.local/bin/tool"]': 'user = ["failing"]'})
    ctx.runner.answers["failing"] = Result("", 1, "", "")
    ctx.baseline = {}
    verify(ctx, bridgeless=False)
    assert ctx.failures == ["verify: failing"] and (home / "b1").is_symlink()
    verify(ctx, bridgeless=True)
    assert ctx.failures[-1] == "verify (bridgeless): failing" and (home / "b1").is_symlink()
    ctx.runner.answers.clear()
    ctx.failures.clear()
    verify(ctx, bridgeless=True)
    assert not ctx.failures and not (home / "b1").exists() and ctx.manifest.bridges_paths == ()
    verify(ctx, bridgeless=True)  # no bridges left: nothing to drop
    ctx.runner.dry_run = True
    verify(ctx, bridgeless=True)
    bridges_aside(ctx.manifest, aside=True)  # no-op without bridges
