"""Unit tests for the daily cash-balance series builder — no live Postgres.

The SQL that feeds the endpoint (debit − credit on the 10xx cash block, posted
entries bucketed by effective_date) mirrors
``app.tools.reporting._cash_position_from_journal`` exactly, so the only NEW
logic is the forward-cumulative carry-forward and the opening / in-window date
split. These tests lock that math and the reconciliation identity:

    series[-1] == opening + Σ(in-window deltas) == journal cash as of the last day

Mirrors the pure-function tier of tests/smoke/test_ar_invoicing_unit.py (zero
I/O, runs on a laptop with no stack).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.api.cash_history import _build_daily_series


def test_series_length_matches_days():
    s = _build_daily_series(Decimal("0"), {}, date(2026, 1, 1), 90)
    assert len(s) == 90


def test_flat_when_no_activity_carries_opening_forward():
    s = _build_daily_series(Decimal("1000.00"), {}, date(2026, 1, 1), 5)
    assert s == [1000.0, 1000.0, 1000.0, 1000.0, 1000.0]


def test_cumulative_carry_forward_across_gaps():
    start = date(2026, 1, 1)
    deltas = {
        date(2026, 1, 1): Decimal("100.00"),   # day 0: 1000 + 100 = 1100
        date(2026, 1, 3): Decimal("-250.00"),  # day 1 flat 1100; day 2: 850
    }
    # day 3 has no activity -> carries 850 forward
    s = _build_daily_series(Decimal("1000.00"), deltas, start, 4)
    assert s == [1100.0, 1100.0, 850.0, 850.0]


def test_last_point_reconciles_to_opening_plus_all_deltas():
    start = date(2026, 3, 1)
    deltas = {
        start: Decimal("500.25"),
        start + timedelta(days=10): Decimal("-120.50"),
        start + timedelta(days=40): Decimal("75.00"),
    }
    opening = Decimal("2000.00")
    s = _build_daily_series(opening, deltas, start, 90)
    expected_last = float(
        (opening + sum(deltas.values(), Decimal("0"))).quantize(Decimal("0.01"))
    )
    # The last point == cash position as of the final day (reconciliation).
    assert s[-1] == expected_last
    # Deltas land on the right calendar offsets.
    assert s[0] == 2500.25
    assert s[9] == 2500.25      # nothing until day 10
    assert s[10] == 2379.75     # 2500.25 - 120.50
    assert s[39] == 2379.75
    assert s[40] == 2454.75     # + 75.00


def test_rounding_is_half_up_to_cents():
    s = _build_daily_series(
        Decimal("0"), {date(2026, 1, 1): Decimal("0.005")}, date(2026, 1, 1), 1
    )
    assert s == [0.01]


def test_negative_balance_is_preserved():
    # Overdrawn cash should report a real negative ending balance, not clamp.
    s = _build_daily_series(
        Decimal("100.00"), {date(2026, 1, 2): Decimal("-300.00")}, date(2026, 1, 1), 3
    )
    assert s == [100.0, -200.0, -200.0]
