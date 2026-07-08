"""Unit tests for the per-number lineage helpers — no live Postgres.

The endpoint is mostly SQL assembly reverse-engineering report figures back to
their journal entries (verified live against the dashboard numbers). These lock
the pure pieces: the account-range SQL fragment, the money formatter, and the
as_of parser. Mirrors the pure-function tier of the other smoke tests.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.api.lineage import _as_of_or_today, _q2, _range_or


def test_range_or_single():
    assert _range_or("je.debit_account_number", [(1000, 1099)]) == (
        "(je.debit_account_number BETWEEN 1000 AND 1099)"
    )


def test_range_or_multi():
    # Revenue spans two ranges (4xxx + 7xxx) — must OR them.
    assert _range_or("c", [(4000, 4999), (7000, 7999)]) == (
        "(c BETWEEN 4000 AND 4999 OR c BETWEEN 7000 AND 7999)"
    )


def test_q2_formats_cents():
    assert _q2("100") == "100.00"
    assert _q2(Decimal("12.5")) == "12.50"
    assert _q2(Decimal("-12416.84")) == "-12416.84"
    assert _q2(None) == "0.00"


def test_as_of_parses_iso():
    assert _as_of_or_today("2026-05-15") == date(2026, 5, 15)


def test_as_of_defaults_to_today():
    assert _as_of_or_today(None) == date.today()


def test_as_of_rejects_garbage():
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        _as_of_or_today("not-a-date")
