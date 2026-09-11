"""The last branches: root-only notes, journaled commits, rebind cmds, mains."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import pytest
from home_tidy_fixture import build_home
from test_home_tidy_migrate import FakeRunner

from _home_tidy_locations import Located, _walk, locate
from _home_tidy_manifest import load_manifest
from _home_tidy_migrate import Plan, build_plan
from _home_tidy_plan_refs import _keep, _merge_journaled
from _home_tidy_run import Runner
from _home_tidy_verify import run_suite


@pytest.fixture
def home(tmp_path: Path) -> Path:
    build_home(tmp_path)
    return tmp_path


def test_walk_file_root_and_missing(tmp_path: Path) -> None:
    f = tmp_path / "f"
    f.write_text("")
    assert [l.path for l in _walk(f, False, frozenset(), ())] == [f]
    assert _walk(tmp_path / "missing", False, frozenset(), ()) == []


def test_user_files_missing_and_locate(home: Path) -> None:
    m = load_manifest(home / "home-tidy.toml", home)
    assert locate(m, "todo", set()) == home / "todo"
    os.rename(home / "todo", home / "gone-elsewhere")
    assert locate(m, "todo", {"move:todo"}) is None  # moved flag but nothing at the new path either


def test_unreadable_file_becomes_note(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("_home_tidy_plan_refs.plan_file", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    plan = build_plan(load_manifest(home / "home-tidy.toml", home))
    assert any("not readable even via sudo" in n for n in plan.notes)


def test_no_claude_json_key(home: Path) -> None:
    p = home / "home-tidy.toml"
    p.write_text(p.read_text().replace('claude_json = "~/.claude.json"', 'claude_json = ""'))
    plan = build_plan(load_manifest(p, home))
    assert not any(a.phase == "json" for a in plan.actions)
    m = load_manifest(p, home)
    assert run_suite(m, FakeRunner(), {}) == []


def test_keep_drops_only_skipped_paths(tmp_path: Path) -> None:
    """``skip_paths`` protects a subtree; with none set nothing is filtered."""
    skipped = tmp_path / "tests" / "fixture.py"
    kept = tmp_path / "bin" / "tool.py"
    items = [Located(skipped), Located(kept), Located(tmp_path / "tests")]
    assert _keep(items, ()) == items
    assert [i.path for i in _keep(items, (tmp_path / "tests",))] == [kept]


def test_merge_journaled(home: Path) -> None:
    m = load_manifest(home / "home-tidy.toml", home)
    h = str(home)
    done = {
        f"rewrite:{h}/src/todo/a.py@1", f"rewrite:{h}/src/todo/a.py@2", f"rewrite:{h}/src/todo/.venv/bin/pip@3",
        f"rewrite:{h}/data/models/x@4", f"rewrite:{h}/src/short@5", "rewrite:/elsewhere/x@6", "move:todo",
    }
    changes = {"src/todo": ["b.py"]}
    _merge_journaled(m, done, changes)
    assert changes == {"src/todo": ["b.py", "a.py"]}


def test_rebind_sudo_cmds_planned(home: Path) -> None:
    p = home / "home-tidy.toml"
    p.write_text(p.read_text().replace('sudo = []\nrestart_user_units', 'sudo = ["systemctl daemon-reload"]\nrestart_user_units'))
    plan = build_plan(load_manifest(p, home))
    assert any(a.id == "rebind:sudo:0" and a.sudo for a in plan.actions)


def test_bridge_skips_missing_dirs(home: Path) -> None:
    m = load_manifest(home / "home-tidy.toml", home)
    os.rename(home / "models", home / "models.bak")
    plan = Plan()
    from _home_tidy_migrate import Action
    from _home_tidy_rebind import plan_post_move

    plan_post_move(m, plan, Action)
    assert not any(a.id == "bridge:models" for a in plan.actions) and not any(a.phase == "ci-summary" for a in plan.actions)


def test_runner_sudo_argv(tmp_path: Path) -> None:
    r = Runner(dry_run=True, log=lambda _s: None, home=tmp_path)
    assert r.sudo_argv(["tee", "/x"], stdin=b"d").ok and r.history == ["sudo tee /x"]


def test_mains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = Path(__file__).resolve().parents[1] / "bin"
    f = tmp_path / "rc"
    f.write_text("# >>> m >>>\nx\n# <<< m <<<\n")
    monkeypatch.setattr(sys, "argv", ["_replace_block.py", str(f), "# >>> m >>>", "# <<< m <<<", ""])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(bin_dir / "_replace_block.py"), run_name="__main__")
    assert exc.value.code == 0 and f.read_text() == "\n"
    monkeypatch.setattr(sys, "argv", ["home_tidy.py", "--home", str(tmp_path), "--state-dir", str(tmp_path / "s"), "undo", "all"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(bin_dir / "home_tidy.py"), run_name="__main__")
    assert exc.value.code == 0


def test_dry_run_failure_is_not_journaled(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from _home_tidy_apply import Context, apply_plan

    m = load_manifest(home / "home-tidy.toml", home)
    plan = build_plan(m)
    runner = FakeRunner({"echo before-svc": __import__("_home_tidy_run").Result("", 1, "", "")})
    runner.dry_run = True
    ctx = Context(m, runner, home / ".state", log=lambda _s: None)
    assert apply_plan(plan, ctx, set()) is False
    assert not (home / ".state" / "migration.jsonl").exists()


def test_user_files_absent_and_sudo_verify_ok(home: Path) -> None:
    p = home / "home-tidy.toml"
    p.write_text(p.read_text().replace('user_files = ["~/.zshrc"]', 'user_files = ["~/.nope"]').replace("sudo = []\n", 'sudo = ["true"]\n'))
    m = load_manifest(p, home)
    from _home_tidy_locations import user_locations

    assert not any(l.path.name == ".nope" for l in user_locations(m))
    (home / ".claude.json").unlink()
    assert run_suite(m, FakeRunner(), {}) == []
