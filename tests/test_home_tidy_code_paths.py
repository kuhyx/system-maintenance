"""Rule ``code-home-path``: the shared path checker run over code roots."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import _home_tidy_code_paths
import home_tidy
import pytest
from _home_tidy_code_paths import rule_code_home_paths
from _home_tidy_manifest import load_manifest
from home_tidy_fixture import MANIFEST

# Echoes $FAKE_OUT and exits $FAKE_RC, standing in for check_home_paths.sh.
FAKE_CHECKER = '#!/bin/bash\n[[ -n "${FAKE_OUT:-}" ]] && printf "%s\\n" "$FAKE_OUT"\nexit "${FAKE_RC:-0}"\n'
FINDING = "skills/x/make_cards.py:51: builds ~/learn — ~ holds only src. Did you mean ~/src/learn?  [py-pathlib]"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "code").mkdir()
    (tmp_path / "check.sh").write_text(FAKE_CHECKER)
    (tmp_path / "home-tidy.toml").write_text(
        MANIFEST
        + '\n[code_paths]\nroots = ["~/code", "~/absent"]\nchecker = "~/check.sh"\n'
    )
    return tmp_path


def _manifest(home: Path):
    return load_manifest(home / "home-tidy.toml", home)


def test_manifest_expands_code_paths(home: Path) -> None:
    m = _manifest(home)
    assert m.code_roots == (str(home / "code"), str(home / "absent"))
    assert m.code_checker == str(home / "check.sh")


def test_no_roots_means_no_scan(tmp_path: Path) -> None:
    (tmp_path / "home-tidy.toml").write_text(MANIFEST)
    m = load_manifest(tmp_path / "home-tidy.toml", tmp_path)
    assert m.code_roots == () and rule_code_home_paths(m) == []


def test_missing_checker_fails_closed(home: Path) -> None:
    m = dataclasses.replace(_manifest(home), code_checker=str(home / "nope.sh"))
    [v] = rule_code_home_paths(m)
    assert (v.rule, v.warn) == ("code-home-path", False)
    assert "path checker not found" in v.detail


def test_clean_tree(home: Path) -> None:
    assert rule_code_home_paths(_manifest(home)) == []


def test_findings_name_the_line(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "FAKE_OUT",
        f"\n1 home-relative path(s) point outside the ~ layout.\n{FINDING}\n    OUT_DIR = ...",
    )
    monkeypatch.setenv("FAKE_RC", "1")
    [v] = rule_code_home_paths(_manifest(home))
    assert v.path == f"{home / 'code'}/skills/x/make_cards.py:51"
    assert v.detail == "builds ~/learn, outside the ~ layout"


@pytest.mark.parametrize(
    ("out", "detail"),
    [
        ("boom: bad args", "path checker failed (exit 2): boom: bad args"),
        ("", "path checker failed (exit 2): no output"),
    ],
)
def test_checker_crash_is_a_failure(
    home: Path, monkeypatch: pytest.MonkeyPatch, out: str, detail: str
) -> None:
    monkeypatch.setenv("FAKE_OUT", out)
    monkeypatch.setenv("FAKE_RC", "2")
    [v] = rule_code_home_paths(_manifest(home))
    assert (v.path, v.detail) == (str(home / "code"), detail)


def test_checker_timeout_is_a_failure(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _hang(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired("check.sh", 120)

    monkeypatch.setattr(_home_tidy_code_paths.subprocess, "run", _hang)
    [v] = rule_code_home_paths(_manifest(home))
    assert v.detail == "path checker timed out after 120s"


def test_check_command_includes_rule(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for b in (
        "src",
        "vendor",
        "services",
        "data",
        "archive",
        "inbox",
        "Downloads",
        "Documents",
    ):
        (home / b).mkdir()
    for stray in ("code", "check.sh", "home-tidy.toml"):
        (home / stray).rename(home / "data" / stray)
    toml = home / "data" / "home-tidy.toml"
    toml.write_text(
        toml.read_text()
        .replace("~/code", "~/data/code")
        .replace("~/check.sh", "~/data/check.sh")
    )
    monkeypatch.setenv("FAKE_OUT", FINDING)
    monkeypatch.setenv("FAKE_RC", "1")
    rc = home_tidy.main(
        [
            "--home",
            str(home),
            "--manifest",
            str(toml),
            "--state-dir",
            str(home / ".state"),
            "check",
        ]
    )
    assert rc == 1
    assert "[FAIL] code-home-path: " in capsys.readouterr().out
