"""Reference rewriter: regex spellings, binary/size guards, links, JSON scope."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from _home_tidy_refs import (
    FileChange,
    LinkChange,
    Rewriter,
    apply_file,
    apply_link,
    is_text_file,
    plan_file,
    plan_link,
    read_via_sudo,
)
from _home_tidy_refs_json import plan_claude_json

HOME = Path("/home/u")


@pytest.fixture
def rw() -> Rewriter:
    return Rewriter({"todo": "src/todo", "kuhylog/kuhylog": "src/kuhylog", "kuhylog": "data/kuhylog", "VirtualBox VMs": "games/VirtualBox VMs"}, HOME)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cd ~/src/todo && x", "cd ~/src/todo && x"),
        ("WorkingDirectory=%h/src/todo", "WorkingDirectory=%h/src/todo"),
        ("/home/u/todo-desktop", "/home/u/todo-desktop"),
        ("Exec=/home/u/kuhylog/kuhylog/build", "Exec=/home/u/src/kuhylog/build"),
        ('"$HOME/data/kuhylog/boards.json"', '"$HOME/data/kuhylog/boards.json"'),
        ("${HOME}/games/VirtualBox VMs/x", "${HOME}/games/VirtualBox VMs/x"),
        ("~/src/todo", "~/src/todo"),
        ('Path.home() / "src/todo" / "x"', 'Path.home() / "src/todo" / "x"'),
        ('_HOME / "src/kuhylog"', '_HOME / "src/kuhylog"'),
        ("os.path.join(HOME, 'data/kuhylog')", "os.path.join(HOME, 'data/kuhylog')"),
        ('HOME / "todo-app"', 'HOME / "todo-app"'),
    ],
)
def test_rewrite_text(rw: Rewriter, text: str, expected: str) -> None:
    assert rw.rewrite_text(text)[0] == expected


def test_map_abs(rw: Rewriter) -> None:
    assert rw.map_abs("/home/u/todo/a") == "/home/u/src/todo/a"
    assert rw.map_abs("/home/u/other") is None


def test_is_text_file_guards(tmp_path: Path) -> None:
    t = tmp_path / "t.txt"
    t.write_text("hello")
    assert is_text_file(t, 100)
    assert not is_text_file(t, 2)  # too big
    b = tmp_path / "b.bin"
    b.write_bytes(b"ab\0cd")
    assert not is_text_file(b, 100)
    assert not is_text_file(tmp_path / "missing", 100)
    os.symlink(t, tmp_path / "ln")
    assert not is_text_file(tmp_path / "ln", 100)
    assert not is_text_file(tmp_path, 100)


def test_plan_file(tmp_path: Path, rw: Rewriter) -> None:
    f = tmp_path / "f"
    f.write_text("x ~/src/todo y\n")
    fc = plan_file(f, rw, 1000)
    assert fc is not None and fc.count == 1 and "+x ~/src/todo y" in fc.diff
    f.write_text("nothing here\n")
    assert plan_file(f, rw, 1000) is None
    assert plan_file(tmp_path / "b.bin", rw, 1000) is None


def test_plan_file_permission(tmp_path: Path, rw: Rewriter, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "root-only"
    f.write_text("secret ~/todo\n")
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        plan_file(f, rw, 1000)
    monkeypatch.setattr("_home_tidy_refs.read_via_sudo", lambda p: None)
    with pytest.raises(PermissionError):
        plan_file(f, rw, 1000, sudo=True)  # sudo -n cat also fails
    monkeypatch.setattr("_home_tidy_refs.read_via_sudo", lambda p: b"via sudo ~/todo\n")
    fc = plan_file(f, rw, 1000, sudo=True)
    assert fc is not None and fc.sudo and "src/todo" in fc.new_text
    monkeypatch.setattr("_home_tidy_refs.read_via_sudo", lambda p: b"\0bin")
    assert plan_file(f, rw, 1000, sudo=True) is None


def test_read_via_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    class R:
        returncode = 0
        stdout = b"data"

    monkeypatch.setattr("_home_tidy_refs.subprocess.run", lambda *a, **k: R())
    assert read_via_sudo(Path("/x")) == b"data"
    R.returncode = 1
    assert read_via_sudo(Path("/x")) is None


def test_plan_link(tmp_path: Path) -> None:
    home = tmp_path
    rw2 = Rewriter({"todo": "src/todo"}, home)
    (home / "todo").mkdir()
    (home / "todo" / "t").write_text("")
    abs_link = home / "abs"
    os.symlink(home / "todo" / "t", abs_link)
    lc = plan_link(abs_link, rw2)
    assert lc is not None and lc.new_target == str(home / "src" / "todo" / "t")
    rel_link = home / "todo" / "rel"
    os.symlink("t", rel_link)  # relative link inside the moved repo, target moves with it
    assert plan_link(rel_link, rw2, new_link=home / "src" / "todo" / "rel") is None  # still "t"
    out_rel = home / "todo" / "up"
    os.symlink("../other/x", out_rel)  # relative link to something that does not move
    lc = plan_link(out_rel, rw2, new_link=home / "src" / "todo" / "up")
    assert lc is not None and lc.new_target == "../../other/x"
    assert plan_link(home / "todo" / "t", rw2) is None  # not a link
    unrelated = home / "elsewhere"
    os.symlink(home / "nope", unrelated)
    assert plan_link(unrelated, rw2) is None


def test_apply_file_and_link(tmp_path: Path) -> None:
    calls: list[tuple[list[str], bytes | None]] = []

    def run_sudo(argv: list[str], stdin: bytes | None = None) -> None:
        calls.append((argv, stdin))

    f = tmp_path / "f"
    f.write_text("old")
    apply_file(FileChange(f, "new", 1, ""), run_sudo)
    assert f.read_text() == "new"
    apply_file(FileChange(Path("/etc/x"), "root", 1, "", sudo=True), run_sudo)
    assert calls[-1] == (["tee", "/etc/x"], b"root")
    link = tmp_path / "ln"
    os.symlink("a", link)
    apply_link(LinkChange(link, "a", "b"), run_sudo)
    assert os.readlink(link) == "b"
    apply_link(LinkChange(Path("/usr/local/bin/x"), "a", "b", sudo=True), run_sudo)
    assert calls[-1][0] == ["ln", "-sfn", "b", "/usr/local/bin/x"]


def test_plan_claude_json(tmp_path: Path) -> None:
    home = tmp_path
    rw2 = Rewriter({"todo": "src/todo"}, home)
    p = home / ".claude.json"
    assert plan_claude_json(p, ("mcpServers",), rw2) is None
    p.write_text(json.dumps({"numStartups": 3}))
    assert plan_claude_json(p, ("mcpServers", "projects"), rw2) is None
    p.write_text(json.dumps({
        "mcpServers": {"a": {"command": f"{home}/todo/bin", "env": {"X": f"{home}/todo"}, "n": 1}},
        "projects": {f"{home}/todo": {"history": [f"{home}/todo"], "mcpServers": {"b": {"args": [f"{home}/todo"]}}}, f"{home}/z": "plain"},
    }))
    fc = plan_claude_json(p, ("mcpServers", "projects", "missing"), rw2)
    assert fc is not None and fc.count == 4
    data = json.loads(fc.new_text)
    assert data["mcpServers"]["a"]["env"]["X"] == f"{home}/src/todo" and data["mcpServers"]["a"]["n"] == 1
    proj = data["projects"][f"{home}/src/todo"]
    assert proj["history"] == [f"{home}/todo"] and proj["mcpServers"]["b"]["args"] == [f"{home}/src/todo"]
    assert data["projects"][f"{home}/z"] == "plain"
