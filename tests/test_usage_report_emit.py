"""Tests for usage_report's output side and its preflight checks: clipboard
tool selection, stdout emission, the atop/log existence guards, and the
since-last-report run including its no-data path.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import _usage_report_emit as emit
import _usage_report_run as run
from _usage_report_types import _Window
import pytest
import usage_report


def _args(**overrides: object) -> object:
    """Build a parsed-args namespace with the parser's own defaults."""
    args = usage_report._build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _aggregates(*, days_with_data: int = 1, seconds: int = 600) -> object:
    """Build an _Aggregates with a window that looks like real coverage."""
    return run._Aggregates(
        cpu={},
        gpu={},
        window=_Window(distinct_samples=2, interval_s=600, seconds=seconds),
        gpu_samples=0,
        days_with_data=days_with_data,
    )


# --------------------------------------------------------------------------- #
# _preflight
# --------------------------------------------------------------------------- #
def test_preflight_exits_without_atop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing atop binary aborts with an install hint."""
    monkeypatch.setattr(emit.shutil, "which", lambda _n: None)

    with pytest.raises(SystemExit) as excinfo:
        emit._preflight(Path("/nonexistent/whatever"))

    assert "not installed" in str(excinfo.value)


def test_preflight_exits_without_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing log file aborts and names the path."""
    monkeypatch.setattr(emit.shutil, "which", lambda _n: "/usr/bin/atop")
    missing = tmp_path / "absent"

    with pytest.raises(SystemExit) as excinfo:
        emit._preflight(missing)

    assert str(missing) in str(excinfo.value)


def test_preflight_passes_when_both_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With atop installed and the log present, preflight is silent."""
    monkeypatch.setattr(emit.shutil, "which", lambda _n: "/usr/bin/atop")
    log = tmp_path / "atop.log"
    log.write_text("x", encoding="utf-8")

    assert emit._preflight(log) is None


# --------------------------------------------------------------------------- #
# _copy_to_clipboard
# --------------------------------------------------------------------------- #
def test_copy_to_clipboard_uses_first_available_tool(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The first candidate present on PATH is the one invoked."""
    monkeypatch.setattr(
        emit.shutil,
        "which",
        lambda name: "/usr/bin/xclip" if name == "xclip" else None,
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        emit.subprocess,
        "run",
        lambda cmd, **_k: calls.append(tuple(cmd)),
    )

    emit._copy_to_clipboard("hello")

    assert calls == [("xclip", "-selection", "clipboard")]
    assert "copied 5 chars via xclip" in capsys.readouterr().err


def test_copy_to_clipboard_reports_a_failing_tool(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tool that errors is reported on stderr and not retried elsewhere."""
    monkeypatch.setattr(emit.shutil, "which", lambda _n: "/usr/bin/wl-copy")

    def boom(*_a: object, **_k: object) -> None:
        raise subprocess.CalledProcessError(1, "wl-copy")

    monkeypatch.setattr(emit.subprocess, "run", boom)

    emit._copy_to_clipboard("hello")

    assert "wl-copy failed" in capsys.readouterr().err


def test_copy_to_clipboard_without_any_tool(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no clipboard tool installed the copy is skipped, not fatal."""
    monkeypatch.setattr(emit.shutil, "which", lambda _n: None)

    emit._copy_to_clipboard("hello")

    assert "skipping copy" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# _emit
# --------------------------------------------------------------------------- #
def test_emit_writes_stdout_and_copies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """By default the report goes to stdout and the clipboard."""
    copied: list[str] = []
    monkeypatch.setattr(emit, "_copy_to_clipboard", copied.append)

    emit._emit(_args(no_clipboard=False), "REPORT")

    assert capsys.readouterr().out == "REPORT"
    assert copied == ["REPORT"]


def test_emit_skips_clipboard_when_suppressed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--no-clipboard leaves stdout untouched but skips the copy."""
    copied: list[str] = []
    monkeypatch.setattr(emit, "_copy_to_clipboard", copied.append)

    emit._emit(_args(no_clipboard=True), "REPORT")

    assert capsys.readouterr().out == "REPORT"
    assert copied == []
