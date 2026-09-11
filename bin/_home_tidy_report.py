"""Render a migration plan for a human to read before ``--apply``.

The terminal gets the summary (move table, per-phase counts, the sudo
block, failures); the full unified diff of every rewrite goes to a file
because it runs to thousands of lines.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path


def _moves_table(plan) -> list[str]:
    rows = [a.payload for a in plan.actions if a.phase == "move"]
    if not rows:
        return []
    width = max(len(old) for old, _ in rows)
    lines = [f"Moves ({len(rows)}):"]
    lines += [f"  {old:<{width}}  ->  {new}" for old, new in rows]
    return lines


def _sudo_block(plan) -> list[str]:
    sudo = [a for a in plan.actions if a.sudo]
    if not sudo:
        return []
    lines = [f"Root steps ({len(sudo)}), run through `sudo -n`:"]
    lines += [f"  [{a.phase}] {a.desc}" for a in sudo]
    return lines


def _rewrite_summary(plan) -> list[str]:
    changes = [a for a in plan.actions if a.phase in ("rewrite", "link", "json")]
    if not changes:
        return ["Reference rewrites: none"]
    refs = sum(a.payload.count for a in changes if a.phase in ("rewrite", "json"))
    lines = [f"Reference rewrites: {len(changes)} file(s)/link(s), {refs} reference(s)"]
    by_top: Counter[str] = Counter()
    for a in changes:
        parts = Path(a.id.split(":", 1)[1]).parts
        by_top["/".join(parts[1:4])] += 1
    lines += [f"  {n:4d}  {top}" for top, n in by_top.most_common(25)]
    if len(by_top) > 25:
        lines.append(f"  ... {len(by_top) - 25} more locations")
    return lines


def _commits(plan) -> list[str]:
    if not plan.repo_changes:
        return []
    lines = [f"Repos to commit + push ({len(plan.repo_changes)}):"]
    lines += [f"  {rel}: {len(files)} file(s)" for rel, files in sorted(plan.repo_changes.items())]
    return lines


def render_plan(plan, done: set[str], diff_path: Path | None) -> str:
    """The human summary; writes the full diff to ``diff_path`` when given."""
    if plan.errors:
        return "Plan aborted:\n" + "\n".join(f"  - {e}" for e in plan.errors) + "\n"
    phases = Counter(a.phase for a in plan.actions)
    pending = sum(1 for a in plan.actions if a.id not in done)
    lines = [f"home-tidy migration plan: {len(plan.actions)} actions, {pending} pending, {len(done)} already done"]
    lines.append("Phases: " + ", ".join(f"{p}={n}" for p, n in phases.items()))
    lines += [""] + _moves_table(plan)
    lines += [""] + _rewrite_summary(plan)
    lines += [""] + _commits(plan)
    lines += [""] + _sudo_block(plan)
    if plan.notes:
        lines += ["", "Notes:"] + [f"  - {n}" for n in plan.notes]
    if diff_path is not None:
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text("".join(a.diff for a in plan.actions if a.diff), encoding="utf-8")
        lines += ["", f"Full diff of every change: {diff_path}"]
    return "\n".join(lines) + "\n"
