"""Post-migration verification and the bridge-symlink decision.

The suite is a diff against a baseline captured before the first action
(failed units, which restartable units were active) plus built-in checks
that need no shell (every MCP server command in ``~/.claude.json`` is
executable) plus the manifest's own commands. It runs twice: with the
bridge symlinks present, then with them moved aside. Only a clean
bridgeless run drops the bridges; otherwise they stay until their expiry
and the failing checks are reported by name.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from _home_tidy_manifest import Manifest, write_bridges

BASELINE_NAME = "baseline.json"


def _units(runner, sudo: bool, state: str) -> list[str]:
    scope = "" if sudo else "--user "
    res = runner.run(f"systemctl {scope}list-units --state={state} --no-legend --plain", sudo=sudo)
    return sorted(line.split()[0] for line in res.stdout.splitlines() if line.strip())


def failed_units(runner, sudo: bool) -> list[str]:
    """Names of units currently in the failed state."""
    return _units(runner, sudo, "failed")


def ensure_baseline(ctx) -> dict:
    """Capture (once) what was failed and what was active before any change."""
    p = ctx.state_dir / BASELINE_NAME
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    rb = ctx.manifest.rebind
    active_user = set(_units(ctx.runner, False, "active"))
    active_sudo = set(_units(ctx.runner, True, "active")) if rb.restart_sudo_units else set()
    base = {
        "failed_user": failed_units(ctx.runner, False),
        "failed_sudo": failed_units(ctx.runner, True),
        "active_user": sorted(u for u in rb.restart_user_units if u in active_user),
        "active_sudo": sorted(u for u in rb.restart_sudo_units if u in active_sudo),
    }
    if not ctx.runner.dry_run:
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(base, indent=2) + "\n", encoding="utf-8")
    return base


def mcp_commands(claude_json: Path) -> list[str]:
    """Absolute ``command`` paths of every MCP server in ``~/.claude.json``."""
    if not claude_json.is_file():
        return []
    data = json.loads(claude_json.read_text(encoding="utf-8"))
    cmds = [v.get("command", "") for v in data.get("mcpServers", {}).values() if isinstance(v, dict)]
    for proj in data.get("projects", {}).values():
        if isinstance(proj, dict):
            cmds += [v.get("command", "") for v in proj.get("mcpServers", {}).values() if isinstance(v, dict)]
    return sorted({c for c in cmds if c.startswith("/")})


def run_suite(manifest: Manifest, runner, baseline: dict) -> list[str]:
    """Every check; returns the failures as human-readable lines."""
    failed: list[str] = []
    for scope, sudo in (("failed_user", False), ("failed_sudo", True)):
        new = sorted(set(failed_units(runner, sudo)) - set(baseline.get(scope, [])))
        failed += [f"newly failed unit: {u}" for u in new]
    for scope, sudo in (("active_user", False), ("active_sudo", True)):
        flag = "" if sudo else "--user "
        for unit in baseline.get(scope, []):
            if not runner.run(f"systemctl {flag}is-active --quiet {shlex.quote(unit)}", sudo=sudo).ok:
                failed.append(f"unit no longer active: {unit}")
    if manifest.refs.claude_json:
        for cmd in mcp_commands(manifest.expand(manifest.refs.claude_json)):
            if not os.access(cmd, os.X_OK):
                failed.append(f"MCP command not executable: {cmd}")
    for cmd in manifest.verify_user:
        if not runner.run(cmd).ok:
            failed.append(cmd)
    for cmd in manifest.verify_sudo:
        if not runner.run(cmd, sudo=True).ok:
            failed.append("sudo " + cmd)
    return failed


def bridges_aside(manifest: Manifest, aside: bool) -> None:
    """Park every bridge symlink as ``.<name>.bridge`` (or bring it back)."""
    for name in manifest.bridges_paths:
        link = manifest.home / name
        parked = manifest.home / f".{name}.bridge"
        src, dst = (link, parked) if aside else (parked, link)
        if src.is_symlink():
            os.rename(src, dst)


def verify(ctx, bridgeless: bool) -> None:
    """Run the suite; on a clean bridgeless run drop the bridges for good."""
    manifest, runner, log = ctx.manifest, ctx.runner, ctx.log
    if runner.dry_run:
        log("  [dry-run] verify suite")
        return
    if bridgeless:
        bridges_aside(manifest, aside=True)
    try:
        failed = run_suite(manifest, runner, ctx.baseline)
    finally:
        if bridgeless:
            bridges_aside(manifest, aside=False)
    label = "verify (bridgeless)" if bridgeless else "verify"
    if failed:
        ctx.failures.extend(f"{label}: {f}" for f in failed)
        log(f"  {len(failed)} check(s) failed" + (" — bridges kept until expiry" if bridgeless else ""))
        return
    log("  all checks passed")
    if bridgeless and manifest.bridges_paths:
        names = list(manifest.bridges_paths)
        write_bridges(ctx.manifest_path, "", [])  # before unlinking: the path may go through a bridge
        ctx.reload_manifest()
        for name in names:
            link = manifest.home / name
            if link.is_symlink():
                link.unlink()
        log("  bridgeless verify passed — all bridge symlinks dropped")
