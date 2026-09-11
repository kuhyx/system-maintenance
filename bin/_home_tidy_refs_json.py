"""Scoped rewrite of ``~/.claude.json`` (split out of ``_home_tidy_refs``).

Under ``mcpServers`` every string is fair game (commands, args, env).
Under ``projects`` only the *keys* (project paths) and each project's own
``mcpServers`` are rewritten — the rest is history and must not change.
"""

from __future__ import annotations

import json
from pathlib import Path

from _home_tidy_refs import FileChange, Rewriter


def _rewrite_strings(value: object, rewriter: Rewriter) -> tuple[object, int]:
    if isinstance(value, str):
        return rewriter.rewrite_text(value)
    if isinstance(value, list):
        items, total = [], 0
        for v in value:
            nv, n = _rewrite_strings(v, rewriter)
            items.append(nv)
            total += n
        return items, total
    if isinstance(value, dict):
        out, total = {}, 0
        for k, v in value.items():
            nv, n = _rewrite_strings(v, rewriter)
            out[k] = nv
            total += n
        return out, total
    return value, 0


def plan_claude_json(
    path: Path, keys: tuple[str, ...], rewriter: Rewriter
) -> FileChange | None:
    """Scoped rewrite of ``~/.claude.json``.

    Under ``mcpServers`` every string is fair game (commands, args, env).
    Under ``projects`` only the *keys* (project paths) and each project's own
    ``mcpServers`` are rewritten — the rest is history and must not change.
    """
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    total = 0
    for key in keys:
        if key not in data:
            continue
        if key == "projects":
            projects = {}
            for proj, cfg in data[key].items():
                new_proj, n = rewriter.rewrite_text(proj)
                total += n
                if isinstance(cfg, dict) and "mcpServers" in cfg:
                    cfg = dict(cfg)
                    cfg["mcpServers"], m = _rewrite_strings(cfg["mcpServers"], rewriter)
                    total += m
                projects[new_proj] = cfg
            data[key] = projects
        else:
            data[key], n = _rewrite_strings(data[key], rewriter)
            total += n
    if not total:
        return None
    new_text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return FileChange(
        path, new_text, total, f"{path}: {total} scoped JSON string(s) rewritten\n"
    )
