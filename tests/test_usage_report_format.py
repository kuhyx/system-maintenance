"""Tests for usage_report's Markdown escaping and duration formatting."""

from __future__ import annotations

import _usage_report_format as fmt
import pytest


# --------------------------------------------------------------------------- #
# _fmt_h
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "0.0s"),
        (8.34, "8.3s"),
        (59.9, "59.9s"),
        (60.0, "1m 00s"),
        (252.0, "4m 12s"),
        (3599.0, "59m 59s"),
        (3600.0, "1h 00m"),
        (5000.0, "1h 23m"),
    ],
)
def test_fmt_h_picks_unit_by_magnitude(seconds: float, expected: str) -> None:
    """Durations switch h/m/s at exactly 3600 and 60 seconds."""
    assert fmt._fmt_h(seconds) == expected


# --------------------------------------------------------------------------- #
# _md_escape
# --------------------------------------------------------------------------- #
def test_md_escape_escapes_pipe_and_flattens_newline() -> None:
    """Pipes are backslash-escaped and newlines become spaces."""
    assert fmt._md_escape("a|b\nc") == r"a\|b c"


def test_md_escape_leaves_plain_names_untouched() -> None:
    """A name with no table-breaking characters passes through unchanged."""
    assert fmt._md_escape("firefox") == "firefox"
