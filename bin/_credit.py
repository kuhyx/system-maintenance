"""kuhy's shared credit line for --help, when kuhy_credit is installed."""

from __future__ import annotations


def credit_epilog() -> str | None:
    """Return the credit line, or None under a python3 without kuhy_credit."""
    try:
        from kuhy_credit import credit_line
    except ImportError:
        return None
    return credit_line()