"""Unit tests for the Stripe payout ↔ BoA deposit matcher.

Pure Python — no DB. Mirror of test_ebay_reconcile_unit.py.
"""
from __future__ import annotations

from decimal import Decimal

from app.connectors.stripe.reconcile import (
    MATCH_AMOUNT_TOLERANCE_CENTS,
    MATCH_DATE_TOLERANCE_DAYS,
    match_payouts,
)


def _payout(jid: str, payout_id: str, amount: str, date: str) -> dict:
    return {
        "journal_id": jid,
        "payout_id": payout_id,
        "amount": Decimal(amount),
        "effective_date": date,
    }


def _bofa(jid: str, txn_id: str, amount: str, date: str) -> dict:
    return {
        "journal_id": jid,
        "bofa_txn_id": txn_id,
        "amount": Decimal(amount),
        "effective_date": date,
        "description": "STRIPE PAYOUT",
        "payee": "STRIPE",
    }


def test_exact_amount_same_day_matches():
    payouts = [_payout("p1", "po_TESTPAYOUT1", "5000.00", "2026-05-01T00:00:00")]
    bofa = [_bofa("b1", "B1", "5000.00", "2026-05-01T00:00:00")]
    matches, unmatched = match_payouts(payouts, bofa)
    assert len(matches) == 1
    assert len(unmatched) == 0
    assert matches[0].stripe_payout_id == "po_TESTPAYOUT1"
    assert matches[0].bofa_journal_id == "b1"
    assert matches[0].amount_cents == 500000


def test_within_dollar_tolerance_matches():
    payouts = [_payout("p1", "po_1", "100.00", "2026-05-01T00:00:00")]
    bofa = [_bofa("b1", "B1", "100.99", "2026-05-01T00:00:00")]
    matches, _ = match_payouts(payouts, bofa)
    assert len(matches) == 1


def test_over_dollar_tolerance_does_not_match():
    payouts = [_payout("p1", "po_1", "100.00", "2026-05-01T00:00:00")]
    bofa = [_bofa("b1", "B1", "102.50", "2026-05-01T00:00:00")]
    matches, unmatched = match_payouts(payouts, bofa)
    assert len(matches) == 0
    assert len(unmatched) == 1


def test_within_2_days_matches():
    payouts = [_payout("p1", "po_1", "100.00", "2026-05-01T00:00:00")]
    bofa = [_bofa("b1", "B1", "100.00", "2026-05-03T00:00:00")]  # 2 days later
    matches, _ = match_payouts(payouts, bofa)
    assert len(matches) == 1


def test_outside_2_days_does_not_match():
    payouts = [_payout("p1", "po_1", "100.00", "2026-05-01T00:00:00")]
    bofa = [_bofa("b1", "B1", "100.00", "2026-05-05T00:00:00")]  # 4 days
    matches, _ = match_payouts(payouts, bofa)
    assert len(matches) == 0


def test_one_bofa_consumed_per_payout():
    """Two Stripe payouts can't both claim the same BoA deposit."""
    payouts = [
        _payout("p1", "po_1", "100.00", "2026-05-01T00:00:00"),
        _payout("p2", "po_2", "100.00", "2026-05-01T00:00:00"),
    ]
    bofa = [_bofa("b1", "B1", "100.00", "2026-05-01T00:00:00")]
    matches, unmatched = match_payouts(payouts, bofa)
    assert len(matches) == 1
    assert len(unmatched) == 1


def test_greedy_picks_closest_date():
    payouts = [_payout("p1", "po_1", "100.00", "2026-05-01T00:00:00")]
    bofa = [
        _bofa("b_far", "B-FAR", "100.00", "2026-05-03T00:00:00"),  # 2 days
        _bofa("b_near", "B-NEAR", "100.00", "2026-05-02T00:00:00"),  # 1 day
    ]
    matches, _ = match_payouts(payouts, bofa)
    assert len(matches) == 1
    assert matches[0].bofa_journal_id == "b_near"


def test_no_payouts_returns_empty():
    assert match_payouts([], []) == ([], [])


def test_constants_visible_for_docs():
    assert MATCH_AMOUNT_TOLERANCE_CENTS == 100
    assert MATCH_DATE_TOLERANCE_DAYS == 2
