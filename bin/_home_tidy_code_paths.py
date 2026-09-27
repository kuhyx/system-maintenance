"""Rule ``code-home-path``: code that would recreate a root entry in ``~``.

Repos catch a ``~/<name>`` path at commit time with the shared
``check_home_paths.sh`` hook. Code outside any such repo (``~/.claude``
skills and scripts) had no gate, so a bad path there only surfaced as a
fresh ``inbox/`` entry every day after a sweep. This rule runs the same
checker over ``[code_paths] roots`` and names the offending line, so the
nag points at the writer rather than at what it wrote.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from _home_tidy_check import Violation
from _home_tidy_manifest import Manifest

RULE = "code-home-path"
CHECK_TIMEOUT_S = 120
# "<rel>:<line>: builds ~/<name> — <why>", as printed by home_paths/check.py.
_FINDING = re.compile(r"^(?P<file>[^:\s]+):(?P<line>\d+): builds (?P<target>~/\S+)")


def _scan(checker: Path, root: Path) -> list[Violation]:
    try:
        proc = subprocess.run(
            ["bash", str(checker), "--all", str(root)],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [
            Violation(
                RULE, str(root), f"path checker timed out after {CHECK_TIMEOUT_S}s"
            )
        ]
    found = [
        Violation(
            RULE,
            f"{root}/{m['file']}:{m['line']}",
            f"builds {m['target']}, outside the ~ layout",
        )
        for m in map(_FINDING.match, proc.stdout.splitlines())
        if m
    ]
    if proc.returncode != 0 and not found:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or ["no output"]
        return [
            Violation(
                RULE,
                str(root),
                f"path checker failed (exit {proc.returncode}): {detail[0]}",
            )
        ]
    return found


def rule_code_home_paths(manifest: Manifest) -> list[Violation]:
    """One FAIL per offending line; fails closed if the checker cannot run."""
    if not manifest.code_roots:
        return []
    checker = Path(manifest.code_checker)
    if not checker.is_file():
        return [
            Violation(
                RULE, str(checker), "path checker not found; code roots unscanned"
            )
        ]
    out: list[Violation] = []
    for root in map(Path, manifest.code_roots):
        if root.is_dir():
            out.extend(_scan(checker, root))
    return out
