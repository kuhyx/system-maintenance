"""Load and expose the home-tidy manifest (``home-tidy.toml``).

The manifest is the single source of truth for what ``~`` may contain, where
every current entry goes, and which files/commands re-point what references
it. This module only parses; nothing here touches the filesystem beyond
reading the manifest itself.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SweepPolicy:
    """Guardrails for the automatic root sweep."""

    grace_minutes: int
    max_entry_bytes: int
    max_entry_count: int
    inbox_nag_days: int
    dry_run_hours_after_install: int


@dataclass(frozen=True)
class RefsConfig:
    """Where the reference rewrite looks for text to rewrite."""

    user_roots: tuple[str, ...]
    user_files: tuple[str, ...]
    user_globs: tuple[str, ...]
    sudo_roots: tuple[str, ...]
    claude_json: str
    claude_json_keys: tuple[str, ...]
    claude_projects_dir: str
    repo_untracked_globs: tuple[str, ...]
    nongit_globs: tuple[str, ...]
    prune_dirs: frozenset[str]
    prune_suffixes: tuple[str, ...]
    # Paths never rewritten, however they are reached. This tool's own test
    # corpus spells pre-move paths on purpose; rewriting them turns every
    # assertion into a tautology (it did, on 2026-09-11, in commit bdf2a5e).
    skip_paths: tuple[str, ...]
    max_file_bytes: int


@dataclass(frozen=True)
class Hook:
    """Commands bracketing the move of one entry (docker down/up, umount...)."""

    entry: str
    before: tuple[str, ...]
    after: tuple[str, ...]
    sudo: bool = False


@dataclass(frozen=True)
class RebindConfig:
    """Commands run once after every move and rewrite."""

    user: tuple[str, ...]
    sudo: tuple[str, ...]
    restart_user_units: tuple[str, ...]
    restart_sudo_units: tuple[str, ...]
    commit_order_first: tuple[str, ...]
    finish_script: str
    commit_trailers: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    """Everything ``home-tidy.toml`` declares, typed."""

    path: Path
    home: Path
    allow: frozenset[str]
    git_only: tuple[str, ...]
    broad_buckets: frozenset[str]
    warn_bucket_size: int
    sweep: SweepPolicy
    bridges_expires: str
    bridges_paths: tuple[str, ...]
    xdg: dict[str, str]
    env: dict[str, str]
    refs: RefsConfig
    mapping: dict[str, str]
    aliases: dict[str, str]
    hooks: tuple[Hook, ...]
    rebind: RebindConfig
    verify_user: tuple[str, ...]
    verify_sudo: tuple[str, ...]
    raw: dict = field(repr=False, default_factory=dict)

    def expand(self, value: str) -> Path:
        """Resolve ``~``/``$HOME`` prefixes against this manifest's home."""
        return Path(expand_home(value, self.home))

    def hook_for(self, entry: str) -> Hook | None:
        """The hook declared for ``entry``, if any."""
        for hook in self.hooks:
            if hook.entry == entry:
                return hook
        return None

    def rewrite_mapping(self) -> dict[str, str]:
        """Moves plus aliases: everything the reference rewrite should know."""
        return {**self.aliases, **self.mapping}

    def ordered_mapping(self) -> list[tuple[str, str]]:
        """Mapping pairs, deepest source first so nested keys hoist before parents."""
        return sorted(self.mapping.items(), key=lambda kv: (-kv[0].count("/"), kv[0]))


def expand_home(value: str, home: Path) -> str:
    """Replace a leading ``~``, ``$HOME`` or ``${HOME}`` with ``home``."""
    for prefix in ("~", "${HOME}", "$HOME"):
        if value == prefix:
            return str(home)
        if value.startswith(prefix + "/"):
            return str(home) + value[len(prefix) :]
    return value


def _tuple(values: list | None) -> tuple[str, ...]:
    return tuple(values or ())


def load_manifest(path: Path, home: Path) -> Manifest:
    """Parse ``path`` into a :class:`Manifest` anchored at ``home``."""
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    root = raw["root"]
    sweep = raw["sweep"]
    refs = raw["refs"]
    rebind = raw.get("rebind", {})
    verify = raw.get("verify", {})
    bridges = raw.get("bridges", {})
    hooks = tuple(
        Hook(
            entry=h["entry"],
            before=_tuple(h.get("before")),
            after=_tuple(h.get("after")),
            sudo=bool(h.get("sudo", False)),
        )
        for h in raw.get("hook", [])
    )
    return Manifest(
        path=path,
        home=home,
        allow=frozenset(root["allow"]),
        git_only=_tuple(root.get("git_only")),
        broad_buckets=frozenset(root.get("broad_buckets", ())),
        warn_bucket_size=int(root.get("warn_bucket_size", 21)),
        sweep=SweepPolicy(
            grace_minutes=int(sweep["grace_minutes"]),
            max_entry_bytes=int(sweep["max_entry_bytes"]),
            max_entry_count=int(sweep["max_entry_count"]),
            inbox_nag_days=int(sweep["inbox_nag_days"]),
            dry_run_hours_after_install=int(sweep.get("dry_run_hours_after_install", 0)),
        ),
        bridges_expires=str(bridges.get("expires", "")),
        bridges_paths=_tuple(bridges.get("paths")),
        xdg=dict(raw.get("xdg", {})),
        env=dict(raw.get("env", {})),
        refs=RefsConfig(
            user_roots=_tuple(refs.get("user_roots")),
            user_files=_tuple(refs.get("user_files")),
            user_globs=_tuple(refs.get("user_globs")),
            sudo_roots=_tuple(refs.get("sudo_roots")),
            claude_json=str(refs.get("claude_json", "")),
            claude_json_keys=_tuple(refs.get("claude_json_keys")),
            claude_projects_dir=str(refs.get("claude_projects_dir", "")),
            repo_untracked_globs=_tuple(refs.get("repo_untracked_globs")),
            nongit_globs=_tuple(refs.get("nongit_globs")),
            prune_dirs=frozenset(refs.get("prune_dirs", ())),
            prune_suffixes=_tuple(refs.get("prune_suffixes")),
            skip_paths=_tuple(refs.get("skip_paths")),
            max_file_bytes=int(refs.get("max_file_bytes", 5_000_000)),
        ),
        mapping=dict(raw.get("map", {})),
        aliases=dict(raw.get("aliases", {})),
        hooks=hooks,
        rebind=RebindConfig(
            user=_tuple(rebind.get("user")),
            sudo=_tuple(rebind.get("sudo")),
            restart_user_units=_tuple(rebind.get("restart_user_units")),
            restart_sudo_units=_tuple(rebind.get("restart_sudo_units")),
            commit_order_first=_tuple(rebind.get("commit_order_first")),
            finish_script=str(rebind.get("finish_script", "")),
            commit_trailers=_tuple(rebind.get("commit_trailers")),
        ),
        verify_user=_tuple(verify.get("user")),
        verify_sudo=_tuple(verify.get("sudo")),
        raw=raw,
    )


_BRIDGE_EXPIRES = re.compile(r'^expires\s*=\s*"[^"]*"\s*$', re.MULTILINE)
_BRIDGE_PATHS = re.compile(r"^paths\s*=\s*\[[^\]]*\]\s*$", re.MULTILINE | re.DOTALL)


def write_bridges(path: Path, expires: str, paths: list[str]) -> None:
    """Record the bridge symlinks in the manifest's ``[bridges]`` section.

    ``tomllib`` cannot write, so the two lines are replaced textually; the
    section is small and written by this tool alone, which keeps that safe.
    """
    text = path.read_text(encoding="utf-8")
    rendered = "paths = [" + ", ".join(f'"{p}"' for p in paths) + "]"
    text = _BRIDGE_EXPIRES.sub(f'expires = "{expires}"', text, count=1)
    text = _BRIDGE_PATHS.sub(rendered, text, count=1)
    path.write_text(text, encoding="utf-8")
