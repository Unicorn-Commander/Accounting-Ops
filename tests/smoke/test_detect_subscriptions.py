"""Unit tests for the PURE subscription detector — no live Postgres.

The detector (``app.subscriptions.detect.detect_subscriptions``) is DB-free, so
the whole heuristic is locked here with synthetic journal-entry dicts. The MCP
tool wrapper (``app.tools.subscriptions``) is just the SELECT that feeds these
same row shapes in, so these tests cover the model-agnostic "brain".

Emphasis is on PRECISION: subscriptions must be detected, but one-off purchases
and owner-equity movements must NOT be flagged (few false positives).

Mirrors the pure-function tier of tests/smoke/test_cash_history_unit.py (zero
I/O, runs on a laptop with no stack).
"""
from __future__ import annotations

from datetime import date, timedelta

from app.subscriptions.detect import detect_subscriptions


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _monthly(
    payee: str,
    amount: str,
    count: int,
    start: date,
    *,
    source: str = "bofa-statement",
    description: str | None = None,
    category: str = "Software Subscriptions",
    step_days: int = 30,
    counterparty_id: str | None = None,
) -> list[dict]:
    """`count` charges roughly `step_days` apart starting at `start`."""
    rows = []
    for i in range(count):
        rows.append(
            {
                "payee": payee,
                "description": description if description is not None else payee,
                "amount": amount,
                "effective_date": (start + timedelta(days=step_days * i)).isoformat(),
                "posted_at": (start + timedelta(days=step_days * i)).isoformat(),
                "source": source,
                "category": category,
                "counterparty_id": counterparty_id,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Clean monthly subscription  (e.g. 12 monthly $38 Pictory charges)
# --------------------------------------------------------------------------- #

def test_clean_monthly_subscription_detected():
    as_of = date(2026, 1, 15)
    entries = _monthly("PICTORY", "38.00", 12, date(2025, 1, 10))

    subs = detect_subscriptions(entries, as_of=as_of)

    assert len(subs) == 1
    sub = subs[0]
    assert sub["merchant"] == "PICTORY"
    assert sub["cadence"] == "monthly"
    assert sub["typical_amount"] == "38.00"
    assert sub["charge_count"] == 12
    assert sub["first_seen"] == "2025-01-10"
    assert sub["last_seen"] == "2025-12-06"  # 2025-01-10 + 11*30 days
    assert sub["funding_source"] == "bofa-statement"
    assert sub["is_active"] is True
    assert sub["total_paid"] == "456.00"  # 12 * 38


def test_monthly_next_expected_is_last_seen_plus_one_period():
    as_of = date(2026, 1, 15)
    entries = _monthly("PICTORY", "38.00", 12, date(2025, 1, 10))

    sub = detect_subscriptions(entries, as_of=as_of)[0]

    # next_expected = last_seen + one monthly period (30 days).
    last_seen = date.fromisoformat(sub["last_seen"])
    assert sub["next_expected"] == (last_seen + timedelta(days=30)).isoformat()


def test_real_calendar_month_cadence_still_monthly():
    # Real bank dates fall on the same day-of-month (28..31-day gaps), not a
    # synthetic 30-day step. The median-gap classifier must still say monthly.
    as_of = date(2026, 1, 5)
    months = [
        date(2025, m, 1) for m in range(1, 13)
    ]
    entries = [
        {
            "payee": "NETFLIX.COM",
            "amount": "15.49",
            "effective_date": d.isoformat(),
            "source": "bofa-statement",
            "category": "Subscriptions",
        }
        for d in months
    ]

    subs = detect_subscriptions(entries, as_of=as_of)
    assert len(subs) == 1
    assert subs[0]["cadence"] == "monthly"
    assert subs[0]["merchant"] == "NETFLIX COM"


# --------------------------------------------------------------------------- #
# Annual subscription
# --------------------------------------------------------------------------- #

def test_annual_subscription_detected():
    as_of = date(2026, 6, 1)
    entries = [
        {
            "payee": "GITHUB",
            "amount": "100.00",
            "effective_date": d.isoformat(),
            "source": "simplefin",
            "category": "Software Subscriptions",
        }
        for d in (date(2023, 3, 1), date(2024, 3, 1), date(2025, 3, 1))
    ]

    subs = detect_subscriptions(entries, as_of=as_of)

    assert len(subs) == 1
    sub = subs[0]
    assert sub["cadence"] == "annual"
    assert sub["typical_amount"] == "100.00"
    assert sub["charge_count"] == 3
    assert sub["funding_source"] == "simplefin"
    # last_seen 2025-03-01 + 365 days.
    assert sub["next_expected"] == (date(2025, 3, 1) + timedelta(days=365)).isoformat()
    # as_of 2026-06-01 is ~15 months after last charge -> still inside the
    # ~1.5x-cadence (~18-month) active window for an annual sub.
    assert sub["is_active"] is True


def test_annual_subscription_goes_stale_past_active_window():
    # Same annual GitHub sub, but viewed 2+ years after the last charge: now
    # outside the ~1.5x-annual window, so it reads as inactive/cancelled.
    as_of = date(2027, 9, 1)
    entries = [
        {
            "payee": "GITHUB",
            "amount": "100.00",
            "effective_date": d.isoformat(),
            "source": "simplefin",
            "category": "Software Subscriptions",
        }
        for d in (date(2023, 3, 1), date(2024, 3, 1), date(2025, 3, 1))
    ]
    sub = detect_subscriptions(entries, as_of=as_of)[0]
    assert sub["cadence"] == "annual"
    assert sub["is_active"] is False


# --------------------------------------------------------------------------- #
# Stale / cancelled subscription (charges stopped ~6 months ago)
# --------------------------------------------------------------------------- #

def test_stale_cancelled_monthly_sub_is_inactive():
    as_of = date(2026, 6, 15)
    # Six monthly charges that STOPPED ~6 months before as_of.
    entries = _monthly("ADOBE", "52.99", 6, date(2025, 7, 1))
    # last charge = 2025-07-01 + 5*30 = 2025-11-27 -> ~6.5 months before as_of.

    subs = detect_subscriptions(entries, as_of=as_of)

    assert len(subs) == 1
    sub = subs[0]
    assert sub["cadence"] == "monthly"
    assert sub["is_active"] is False           # stopped > 1.5 months ago
    assert sub["months_since_last"] >= 6.0


# --------------------------------------------------------------------------- #
# One-off purchases — must NOT be flagged
# --------------------------------------------------------------------------- #

def test_one_off_purchases_not_flagged():
    as_of = date(2026, 1, 15)
    entries = [
        {
            "payee": "BEST BUY",
            "amount": "899.00",
            "effective_date": "2025-03-04",
            "source": "bofa-statement",
            "category": "Equipment",
        },
        {
            "payee": "DELTA AIR LINES",
            "amount": "412.30",
            "effective_date": "2025-06-21",
            "source": "bofa-statement",
            "category": "Travel",
        },
        {
            "payee": "HOME DEPOT",
            "amount": "73.18",
            "effective_date": "2025-09-02",
            "source": "bofa-statement",
            "category": "Supplies",
        },
    ]

    assert detect_subscriptions(entries, as_of=as_of) == []


def test_two_charges_below_min_not_flagged():
    # Two identical monthly charges is not yet a subscription (need >= 3).
    as_of = date(2026, 1, 15)
    entries = _monthly("SPOTIFY", "11.99", 2, date(2025, 11, 1))
    assert detect_subscriptions(entries, as_of=as_of) == []


def test_irregular_repeat_spend_not_flagged():
    # Same merchant 4x but at wildly irregular gaps + varied amounts: this is
    # incidental repeat spend (a coffee shop), not a subscription.
    as_of = date(2026, 1, 15)
    entries = [
        {"payee": "STARBUCKS", "amount": "6.45", "effective_date": "2025-01-03",
         "source": "bofa-statement", "category": "Meals"},
        {"payee": "STARBUCKS", "amount": "12.10", "effective_date": "2025-01-30",
         "source": "bofa-statement", "category": "Meals"},
        {"payee": "STARBUCKS", "amount": "4.25", "effective_date": "2025-05-18",
         "source": "bofa-statement", "category": "Meals"},
        {"payee": "STARBUCKS", "amount": "21.00", "effective_date": "2025-11-02",
         "source": "bofa-statement", "category": "Meals"},
    ]
    assert detect_subscriptions(entries, as_of=as_of) == []


# --------------------------------------------------------------------------- #
# Owner distribution / transfer / loan series — must NOT be flagged
# --------------------------------------------------------------------------- #

def test_owner_distribution_series_not_flagged():
    # 12 monthly owner draws of the same amount — a textbook recurring pattern
    # that must be EXCLUDED because it's an equity movement, not a subscription.
    as_of = date(2026, 1, 15)
    entries = _monthly(
        "AARON STRANSKY",
        "5000.00",
        12,
        date(2025, 1, 1),
        category="Owner Distribution",
        source="bofa-statement",
    )
    assert detect_subscriptions(entries, as_of=as_of) == []


def test_transfer_and_loan_categories_excluded():
    as_of = date(2026, 1, 15)
    transfers = _monthly(
        "TRANSFER TO SAVINGS", "2000.00", 6, date(2025, 1, 1),
        category="Transfer", source="simplefin",
    )
    loans = _monthly(
        "SBA LOAN PMT", "1500.00", 6, date(2025, 1, 5),
        category="Loan Repayment", source="bofa-statement",
    )
    assert detect_subscriptions(transfers + loans, as_of=as_of) == []


# --------------------------------------------------------------------------- #
# Merchant normalization (statement noise) + counterparty keying
# --------------------------------------------------------------------------- #

def test_checkcard_noise_collapses_same_style_charges():
    # The SAME card-funded PayPal sub arrives as a CHECKCARD line each month
    # with a different leading terminal/date token + trailing txn id. The POS
    # prefix + long-digit strips must collapse all of them into one group.
    as_of = date(2026, 1, 15)
    raws = [
        ("CHECKCARD 0904 PAYPAL *NETFLIX 4029357733", "2025-09-04"),
        ("CHECKCARD 1004 PAYPAL *NETFLIX 4029357733", "2025-10-04"),
        ("CHECKCARD 1104 PAYPAL *NETFLIX 8005551234", "2025-11-04"),
        ("CHECKCARD 1204 PAYPAL *NETFLIX 4029357733", "2025-12-04"),
    ]
    entries = [
        {
            "payee": None,                 # force the description-normalization path
            "description": desc,
            "amount": "15.49",
            "effective_date": d,
            "source": "simplefin",
            "category": "Subscriptions",
        }
        for desc, d in raws
    ]

    subs = detect_subscriptions(entries, as_of=as_of)
    assert len(subs) == 1
    assert subs[0]["charge_count"] == 4
    assert subs[0]["cadence"] == "monthly"
    assert subs[0]["merchant"] == "PAYPAL NETFLIX"


def test_processor_des_id_tail_normalizes_to_processor_token():
    # The canonical ACH-style "PAYPAL DES:... ID:..." form: the DES:/ID: tail
    # is cut so the merchant key is the bare processor "PAYPAL" (per spec).
    as_of = date(2026, 1, 15)
    raws = [
        "PAYPAL DES:INST XFER ID:NETFLIX INDN:AARON",
        "PAYPAL DES:INST XFER ID:NETFLIX INDN:AARON",
        "POS DEBIT PAYPAL DES:NETFLIX ID:123456",
    ]
    entries = [
        {
            "payee": None,
            "description": desc,
            "amount": "15.49",
            "effective_date": (date(2025, 9, 4) + timedelta(days=30 * i)).isoformat(),
            "source": "simplefin",
            "category": "Subscriptions",
        }
        for i, desc in enumerate(raws)
    ]
    subs = detect_subscriptions(entries, as_of=as_of)
    assert len(subs) == 1
    assert subs[0]["merchant"] == "PAYPAL"


def test_counterparty_id_groups_despite_payee_drift():
    # Payee text drifts month to month, but a stable counterparty_id ties them
    # together (the strongest key).
    as_of = date(2026, 1, 15)
    entries = []
    base = date(2025, 8, 1)
    variants = ["AWS *EMEA", "AMAZON WEB SERVICES", "AWS US EAST", "AWS *0428"]
    for i, v in enumerate(variants):
        entries.append(
            {
                "payee": v,
                "amount": "42.00",
                "effective_date": (base + timedelta(days=30 * i)).isoformat(),
                "source": "bofa-statement",
                "category": "Cloud Hosting",
                "counterparty_id": "cp-aws-001",
            }
        )

    subs = detect_subscriptions(entries, as_of=as_of)
    assert len(subs) == 1
    assert subs[0]["charge_count"] == 4
    assert subs[0]["cadence"] == "monthly"


# --------------------------------------------------------------------------- #
# Amount tolerance + sorting + mixed portfolio
# --------------------------------------------------------------------------- #

def test_small_price_changes_within_tolerance_stay_one_group():
    # A price bump from 38.00 -> 40.00 (~5%) stays inside the 15% band.
    as_of = date(2026, 1, 15)
    amounts = ["38.00", "38.00", "38.00", "40.00", "40.00", "40.00"]
    entries = []
    start = date(2025, 7, 1)
    for i, amt in enumerate(amounts):
        entries.append(
            {
                "payee": "PICTORY",
                "amount": amt,
                "effective_date": (start + timedelta(days=30 * i)).isoformat(),
                "source": "bofa-statement",
                "category": "Software Subscriptions",
            }
        )
    subs = detect_subscriptions(entries, as_of=as_of)
    assert len(subs) == 1
    assert subs[0]["charge_count"] == 6


def test_results_sorted_by_typical_amount_desc():
    as_of = date(2026, 1, 15)
    cheap = _monthly("SPOTIFY", "11.99", 6, date(2025, 7, 1))
    mid = _monthly("PICTORY", "38.00", 6, date(2025, 7, 3))
    pricey = _monthly("SALESFORCE", "150.00", 6, date(2025, 7, 5))

    subs = detect_subscriptions(cheap + mid + pricey, as_of=as_of)

    assert [s["merchant"] for s in subs] == ["SALESFORCE", "PICTORY", "SPOTIFY"]
    assert [s["typical_amount"] for s in subs] == ["150.00", "38.00", "11.99"]


def test_negative_amounts_compared_on_magnitude():
    # Outflows may be stored as negative; the detector keys on magnitude.
    as_of = date(2026, 1, 15)
    entries = _monthly("PICTORY", "-38.00", 5, date(2025, 8, 1))
    subs = detect_subscriptions(entries, as_of=as_of)
    assert len(subs) == 1
    assert subs[0]["typical_amount"] == "38.00"
    assert subs[0]["total_paid"] == "190.00"


def test_empty_input_returns_empty_list():
    assert detect_subscriptions([], as_of=date(2026, 1, 1)) == []
