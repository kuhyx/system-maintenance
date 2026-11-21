"""Tests for the shared credit epilog."""

import sys

import pytest

from _credit import credit_epilog


def test_credit_epilog_equals_kuhy_credit_line() -> None:
    from kuhy_credit import credit_line

    assert credit_epilog() == credit_line()


def test_credit_epilog_returns_none_when_kuhy_credit_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "kuhy_credit", None)

    # Need to reload _credit to pick up the patched module
    import importlib
    import _credit
    importlib.reload(_credit)

    assert _credit.credit_epilog() is None


def test_usage_report_help_ends_with_credit_line() -> None:
    import usage_report

    help_text = usage_report._build_parser().format_help().rstrip()
    from kuhy_credit import credit_line

    assert help_text.endswith(credit_line())


def test_home_tidy_help_ends_with_credit_line() -> None:
    import home_tidy

    help_text = home_tidy._build_parser().format_help().rstrip()
    from kuhy_credit import credit_line

    assert help_text.endswith(credit_line())