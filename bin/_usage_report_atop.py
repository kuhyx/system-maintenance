"""Subprocess plumbing for usage_report.

Runs atop, streams its parseable output, and builds/caches the native
`atop_agg` C helper.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
import shutil
import subprocess
from typing import TYPE_CHECKING

from _usage_report_types import ProcAgg, _Window

if TYPE_CHECKING:
    from collections.abc import Iterator

    from _usage_report_types import _Progress

_ATOP_AGG_CACHE_BIN = Path.home() / ".cache" / "usage_report" / "atop_agg"
_ATOP_AGG_BIN_MODE = 0o755
# Repo layout: linux_configuration/scripts/system-maintenance/bin/usage_report.py
# -> parents[4] is the repo root which hosts the C/ source tree.
_ATOP_AGG_SRC_DIR = Path(__file__).resolve().parents[4] / "C" / "atop_agg"
_ATOP_AGG_BUILD_TIMEOUT_S = 60
_NATIVE_TSV_NAME_LEN = 7
_NATIVE_TSV_WIN_LEN = 5


def _run(cmd: list[str]) -> str:
    """Run *cmd* and return stdout (empty string on failure)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout


def _atop_read_cmd(
    log: Path,
    labels: str,
    begin: str | None,
    end: str | None,
) -> list[str]:
    """Build an `atop -r` command, optionally bounded by begin/end times.

    *begin*/*end* are atop `-b`/`-e` arguments (`[YYYYMMDD]hhmm[ss]`) used to
    restrict replay to a sub-window of the day's log, so a "since last report"
    run does not double-count the part of the first day already reported.
    """
    cmd = ["atop", "-r", str(log)]
    if begin is not None:
        cmd += ["-b", begin]
    if end is not None:
        cmd += ["-e", end]
    cmd += ["-P", labels]
    return cmd


def _iter_atop_lines(
    log: Path,
    labels: str,
    begin: str | None = None,
    end: str | None = None,
) -> Iterator[str]:
    """Stream `atop -r LOG -P LABELS` stdout line-by-line.

    Uses `Popen` so the report can show progress while atop is still
    decoding its binary log, rather than buffering the whole output.
    """
    cmd = _atop_read_cmd(log, labels, begin, end)
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as proc:
        stdout = proc.stdout
        if stdout is None:
            return
        for raw in stdout:
            yield raw.rstrip("\n")


def _atop_agg_binary() -> Path | None:
    """Return a cached `atop_agg` binary path, auto-building if missing/stale.

    Falls back to ``None`` when the C source tree or a system C compiler
    is unavailable, in which case callers use the pure-Python parser.
    """
    src_c = _ATOP_AGG_SRC_DIR / "atop_agg.c"
    if not src_c.exists():
        # Source tree is gone (relocated/extracted): never trust an orphaned
        # cached binary whose provenance we can no longer verify against
        # source — a stale build can silently carry parsing bugs. Fall back to
        # the pure-Python parser instead.
        return None
    if (
        _ATOP_AGG_CACHE_BIN.exists()
        and src_c.stat().st_mtime <= _ATOP_AGG_CACHE_BIN.stat().st_mtime
    ):
        return _ATOP_AGG_CACHE_BIN
    if shutil.which("cc") is None:
        return None
    _ATOP_AGG_CACHE_BIN.parent.mkdir(parents=True, exist_ok=True)
    make_cmd = ["make", "-s", "-C", str(_ATOP_AGG_SRC_DIR), "atop_agg"]
    try:
        subprocess.run(
            make_cmd,
            check=True,
            capture_output=True,
            timeout=_ATOP_AGG_BUILD_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    built = _ATOP_AGG_SRC_DIR / "atop_agg"
    if not built.exists():
        return None
    shutil.copy2(built, _ATOP_AGG_CACHE_BIN)
    _ATOP_AGG_CACHE_BIN.chmod(_ATOP_AGG_BIN_MODE)
    return _ATOP_AGG_CACHE_BIN


def _apply_native_name(parts: list[str], agg_map: dict[str, ProcAgg]) -> None:
    r"""Fold one `N\t<name>\t<cpu>\t<peak>\t<sum_avg>\t<ram_n>\t<pids>` row."""
    _, name, cpu_s, peak_s, sum_avg_s, rss_n_s, pids_s = parts
    entry = agg_map.setdefault(name, ProcAgg(name=name))
    entry.cpu_ticks = int(cpu_s)
    entry.peak_rss_kb = int(peak_s)
    entry.rss_kb_sum = int(sum_avg_s)
    entry.rss_samples = int(rss_n_s)
    # The C helper pre-aggregates by name; pid_set is unused in the native
    # path but `len(pid_set)` drives the "PIDs" column in the report.
    entry.pid_set = set(range(int(pids_s)))


def _window_from_native(parts: list[str]) -> _Window:
    r"""Build a `_Window` from a `W\t<start>\t<end>\t<n>\t<interval>` row."""
    _, start_s, end_s, n_s, interval_s = parts
    n_epochs = int(n_s)
    if not n_epochs:
        return _Window()
    start_epoch = int(start_s)
    end_epoch = int(end_s)
    start_dt = _dt.datetime.fromtimestamp(start_epoch).astimezone()
    end_dt = _dt.datetime.fromtimestamp(end_epoch).astimezone()
    return _Window(
        start=start_dt.isoformat(timespec="seconds"),
        end=end_dt.isoformat(timespec="seconds"),
        distinct_samples=n_epochs,
        interval_s=int(interval_s),
        seconds=end_epoch - start_epoch,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
    )


def _aggregate_atop_native(
    log: Path,
    progress: _Progress,
    binary: Path,
    begin: str | None = None,
    end: str | None = None,
) -> tuple[dict[str, ProcAgg], _Window]:
    """Aggregate via `atop | atop_agg`; return `(by_name, window)`."""
    progress.start_stage("atop: parse PRC+PRM (native)")
    agg_map: dict[str, ProcAgg] = {}
    window = _Window()
    atop_cmd = _atop_read_cmd(log, "PRC,PRM", begin, end)
    agg_cmd = [str(binary)]
    with (
        subprocess.Popen(
            atop_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as atop,
        subprocess.Popen(
            agg_cmd,
            stdin=atop.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ) as agg,
    ):
        if atop.stdout is not None:
            atop.stdout.close()
        stdout = agg.stdout
        if stdout is None:
            return agg_map, window
        for raw in stdout:
            parts = raw.rstrip("\n").split("\t")
            tag = parts[0]
            if tag == "N" and len(parts) == _NATIVE_TSV_NAME_LEN:
                _apply_native_name(parts, agg_map)
            elif tag == "W" and len(parts) == _NATIVE_TSV_WIN_LEN:
                window = _window_from_native(parts)
    progress.update(1.0)
    return agg_map, window
