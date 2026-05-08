"""Regression tests for pmon process-name normalization in usage_report."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "system-maintenance"
    / "bin"
    / "usage_report.py"
)
SPEC = importlib.util.spec_from_file_location("usage_report", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    msg = "could not load usage_report module"
    raise RuntimeError(msg)
usage_report = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = usage_report
SPEC.loader.exec_module(usage_report)


def test_normalize_pmon_command_prefers_first_executable_token() -> None:
    """The parser should keep executable-like token, not trailing args."""
    tokens = ["code-insiders", "--type=", "gpu-process", "Not"]

    assert usage_report._normalize_pmon_command(tokens) == "code-insiders"


def test_normalize_pmon_command_skips_leading_option_tokens() -> None:
    """If the first token is an option, use the next non-option token."""
    tokens = ["--type=", "code-insiders", "--flag"]

    assert usage_report._normalize_pmon_command(tokens) == "code-insiders"


def test_ingest_pmon_row_uses_command_field_start_not_last_token() -> None:
    """Rows with command args should aggregate under process name, not args."""
    row = [
        "20260507",
        "12:00:00",
        "0",
        "123",
        "C",
        "10",
        "5",
        "0",
        "0",
        "0",
        "0",
        "code-insiders",
        "--type=",
        "gpu-process",
    ]
    agg: dict[str, object] = {}

    consumed = usage_report._ingest_pmon_row(row, agg)

    assert consumed == 1
    assert "code-insiders" in agg


def test_ingest_pmon_row_falls_back_to_proc_comm_on_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When command field is empty, parser can recover with /proc/<pid>/comm."""
    row = [
        "20260507",
        "12:00:00",
        "0",
        "999",
        "C",
        "30",
        "10",
        "0",
        "0",
        "0",
        "0",
    ]
    agg: dict[str, object] = {}

    monkeypatch.setattr(usage_report, "_pid_comm_name", lambda _pid: "python")
    consumed = usage_report._ingest_pmon_row(row, agg)

    assert consumed == 1
    assert "python" in agg
