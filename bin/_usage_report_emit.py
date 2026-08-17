"""Preflight checks and report output for usage_report.

Verifies atop and its log are present before a run, writes the finished report
to stdout, and copies it to the system clipboard with whichever tool is
installed.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


_INSTALL_SCRIPT = Path(__file__).with_name("install_usage_monitoring.sh")


def _preflight(atop_log: Path) -> None:
    if not shutil.which("atop"):
        sys.exit(
            f"error: `atop` is not installed.\nrun: {_INSTALL_SCRIPT}",
        )
    if not atop_log.exists():
        sys.exit(
            f"error: atop log not found: {atop_log}\n"
            f"run: {_INSTALL_SCRIPT} (enables atop.service), "
            "then wait for the first sample.",
        )


_CLIPBOARD_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("wl-copy", ("wl-copy",)),
    ("xclip", ("xclip", "-selection", "clipboard")),
    ("xsel", ("xsel", "--clipboard", "--input")),
)


def _copy_to_clipboard(text: str) -> None:
    """Copy `text` to the system clipboard using the first available tool.

    Prints a one-line status to stderr so the stdout report stays pristine
    for redirection.
    """
    for name, cmd in _CLIPBOARD_CANDIDATES:
        if not shutil.which(name):
            continue
        try:
            subprocess.run(cmd, input=text, text=True, check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            sys.stderr.write(f"clipboard: {name} failed: {exc}\n")
            return
        sys.stderr.write(f"clipboard: copied {len(text)} chars via {name}\n")
        return
    sys.stderr.write(
        "clipboard: no wl-copy/xclip/xsel found; skipping copy\n",
    )


def _emit(args: argparse.Namespace, report: str) -> None:
    """Write the report to stdout and (unless suppressed) the clipboard."""
    sys.stdout.write(report)
    if not args.no_clipboard:
        _copy_to_clipboard(report)
