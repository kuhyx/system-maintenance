"""Markdown escaping and duration formatting shared by the report sections."""

from __future__ import annotations

_SEC_PER_HOUR = 3600
_SEC_PER_MIN = 60


def _md_escape(name: str) -> str:
    """Escape characters that would break a Markdown table cell."""
    return name.replace("|", r"\|").replace("\n", " ")


def _fmt_h(seconds: float) -> str:
    """Human-friendly duration: `"1h 23m"` / `"4m 12s"` / `"8.3s"`."""
    if seconds >= _SEC_PER_HOUR:
        h = int(seconds // _SEC_PER_HOUR)
        m = int((seconds % _SEC_PER_HOUR) // _SEC_PER_MIN)
        return f"{h}h {m:02d}m"
    if seconds >= _SEC_PER_MIN:
        m = int(seconds // _SEC_PER_MIN)
        s = int(seconds % _SEC_PER_MIN)
        return f"{m}m {s:02d}s"
    return f"{seconds:.1f}s"
