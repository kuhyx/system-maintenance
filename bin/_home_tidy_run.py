"""Shell-command runner shared by migrate, rebind and verify.

Every external command goes through :class:`Runner` so that ``--plan``
can list what would run, ``--apply`` can log what did, and the tests can
substitute a fake without touching ``subprocess`` themselves. Root steps
use ``sudo -n``: a password prompt cannot be answered from here, so the
caller pre-checks ``sudo -n true`` and tells the user to refresh the
timestamp when it fails.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Result:
    """Outcome of one command."""

    cmd: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """True on exit status zero."""
        return self.returncode == 0


@dataclass
class Runner:
    """Run shell commands, optionally as root, optionally only pretending."""

    dry_run: bool = False
    log: Callable[[str], None] = print
    home: Path | None = None
    history: list[str] = field(default_factory=list)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.home is not None:
            env["HOME"] = str(self.home)
        return env

    def _argv(self, cmd: str, sudo: bool) -> list[str]:
        if sudo and self.home is not None:
            # sudo may reset HOME; spell the user's home out for root steps.
            cmd = cmd.replace("~/", f"{self.home}/").replace("$HOME/", f"{self.home}/")
        argv = ["bash", "-c", cmd]
        return ["sudo", "-n", *argv] if sudo else argv

    def run(self, cmd: str, sudo: bool = False, stdin: bytes | None = None, check: bool = False, cwd: Path | None = None) -> Result:
        """Execute ``cmd`` via ``bash -c`` (``sudo -n bash -c`` for root)."""
        label = ("sudo " if sudo else "") + cmd
        self.history.append(label)
        if self.dry_run:
            self.log(f"  [dry-run] {label}")
            return Result(cmd, 0, "", "")
        self.log(f"  $ {label}")
        proc = subprocess.run(
            self._argv(cmd, sudo), input=stdin, capture_output=True, check=False, env=self._env(), cwd=cwd
        )
        res = Result(
            cmd,
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )
        if check and not res.ok:
            raise RuntimeError(f"command failed ({res.returncode}): {label}\n{res.stderr.strip()}")
        return res

    def sudo_argv(self, argv: list[str], stdin: bytes | None = None) -> Result:
        """Run an argv list as root (used for ``tee``/``ln`` on root files)."""
        return self.run(shlex.join(argv), sudo=True, stdin=stdin, check=True)

    def sudo_available(self) -> bool:
        """Can we run root commands without a prompt right now?"""
        return self.dry_run or self.run("true", sudo=True).ok
