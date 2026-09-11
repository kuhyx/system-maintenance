"""CLI edges, the plan renderer, locations, and the zshrc block helper."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from home_tidy_fixture import build_home
from test_home_tidy_migrate import FakeRunner

import _replace_block
import home_tidy
from _home_tidy_locations import repo_locations, user_locations
from _home_tidy_manifest import load_manifest
from _home_tidy_migrate import Plan, build_plan
from _home_tidy_report import render_plan
from _home_tidy_run import Result


@pytest.fixture
def home(tmp_path: Path) -> Path:
    build_home(tmp_path)
    return tmp_path


def _args(home: Path, *rest: str) -> list[str]:
    return ["--home", str(home), "--manifest", str(home / "home-tidy.toml"), "--state-dir", str(home / ".state"), *rest]


def test_defaults_and_check_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert home_tidy.default_manifest().name == "home-tidy.toml"
    assert home_tidy.default_state_dir(Path("/h")) == Path("/h/.local/state/home-tidy")
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "home-tidy.toml").write_text(__import__("home_tidy_fixture").MANIFEST)
    (tmp_path / "home-tidy.toml").rename(tmp_path / ".manifest.toml")
    assert home_tidy.main(["--home", str(tmp_path), "--manifest", str(tmp_path / ".manifest.toml"), "--state-dir", str(tmp_path / ".s"), "check"]) == 0
    assert capsys.readouterr().out == "home-tidy: clean\n"
    assert (tmp_path / ".s" / "report.txt").read_text() == ""


def test_sweep_forced_dry_run(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = home / ".state"
    state.mkdir()
    (state / "installed-at").write_text(str(__import__("time").time()))
    old = 1_000
    os.utime(home / "fake_finish.sh", (old, old))
    assert home_tidy.main(_args(home, "sweep")) == 1
    out = capsys.readouterr().out
    assert "post-install dry-run window" in out and "would move" in out and (home / "fake_finish.sh").exists()


def test_migrate_plan_errors_and_sudo_gate(home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (home / "src" / "todo").mkdir(parents=True)
    assert home_tidy.main(_args(home, "migrate", "--plan")) == 2
    assert "Plan aborted" in capsys.readouterr().out
    (home / "src" / "todo").rmdir()
    p = home / "home-tidy.toml"
    (home / "sudoroot").mkdir()
    (home / "sudoroot" / "unit").write_text("ExecStart=~/todo/run.sh\n")
    p.write_text(p.read_text().replace('sudo_roots = []', f'sudo_roots = ["{home}/sudoroot"]'))
    monkeypatch.setattr(home_tidy.Runner, "sudo_available", lambda self: False)
    assert home_tidy.main(_args(home, "migrate", "--apply", "--yes")) == 3
    assert "sudo -v" in capsys.readouterr().out


def test_migrate_apply_complete(home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(home_tidy, "Runner", lambda dry_run, home: FakeRunner())  # pushes "succeed"
    assert home_tidy.main(_args(home, "migrate", "--apply", "--yes")) == 0
    assert "migration complete" in capsys.readouterr().out
    assert home_tidy.main(_args(home, "migrate", "--apply", "--yes", "--dry-run")) == 0


def test_render_plan(home: Path, tmp_path: Path) -> None:
    m = load_manifest(home / "home-tidy.toml", home)
    plan = build_plan(m)
    text = render_plan(plan, {plan.actions[0].id}, None)
    assert "1 already done" in text and "Root steps" not in text and "Full diff" not in text
    plan.notes.append("note me")
    plan.actions = [a for a in plan.actions if a.phase not in ("move", "rewrite", "link", "json")]
    plan.repo_changes = {}
    text = render_plan(plan, set(), tmp_path / "d.diff")
    assert "Reference rewrites: none" in text and "note me" in text and "Moves" not in text
    assert (tmp_path / "d.diff").exists()
    many = Plan()
    from _home_tidy_migrate import Action
    from _home_tidy_refs import FileChange

    for i in range(30):
        fc = FileChange(Path(f"/a/b/{i}/f"), "", 1, "")
        many.actions.append(Action(f"rewrite:/a/b/{i}/f", "rewrite", "", sudo=(i == 0), diff="d", payload=fc))
    text = render_plan(many, set(), None)
    assert "more locations" in text and "Root steps (1)" in text
    assert render_plan(Plan(errors=["x"]), set(), None) == "Plan aborted:\n  - x\n"


def test_locations_edges(home: Path) -> None:
    m = load_manifest(home / "home-tidy.toml", home)
    (home / ".local" / "bin" / "sub").mkdir()
    os.symlink("..", home / ".local" / "bin" / "sub" / "loop")
    (home / ".local" / "bin" / "sub" / "skip.jsonl").write_text("")
    (home / ".local" / "bin" / "sub" / "node_modules").mkdir()
    paths = {str(l.path.relative_to(home)) for l in user_locations(m)}
    assert ".local/bin/sub/loop" in paths and ".local/bin/sub/skip.jsonl" not in paths
    assert not any("node_modules" in p for p in paths)
    assert repo_locations(home / "models", ("*.yml",)) == []
    (home / "models" / "x.yml").write_text("")
    assert [l.path.name for l in repo_locations(home / "models", ("*.yml", "*.yml"))] == ["x.yml"]
    (home / "todo" / ".git" / "broken").mkdir()
    import _home_tidy_locations as loc

    real = loc.subprocess.run
    loc.subprocess.run = lambda *a, **k: Result("", 128, "", "")  # type: ignore[assignment]
    try:
        assert loc.tracked_files(home / "todo") == []
    finally:
        loc.subprocess.run = real


def test_replace_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "rc"
    f.write_text("a\n\n# >>> x >>>\nold\n# <<< x <<<\nb\n")
    assert _replace_block.main(["x", str(f), "# >>> x >>>", "# <<< x <<<", "# >>> x >>>\nnew\n# <<< x <<<"]) == 0
    assert f.read_text() == "a\n\n# >>> x >>>\nnew\n# <<< x <<<\nb\n"
    assert _replace_block.main(["x", str(f), "# >>> x >>>", "# <<< x <<<", ""]) == 0
    assert f.read_text() == "a\n\nb\n"
    assert _replace_block.main(["x"]) == 2
    assert "Replace" in capsys.readouterr().err
