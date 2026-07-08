"""Smoke tests for the BofA-coverage vendor-rule additions.

All inputs in this file are SYNTHETIC and constructed to exercise the
rule shapes documented in `app/categorization/vendor_rules.py` — they
are NOT taken verbatim from any real bank statement. The point is to
pin the pattern → (category, direction) bindings against regressions,
not to assert anything about a specific customer's books.

Bindings under test:

* PayPal direction split
    - `paypal ... des:transfer ...` (inflow)        → Revenue · Marketplace (auto)
    - `paypal ... des:inst xfer ...` (outflow)     → Office · Supplies (auto)
    - `checkcard ... paypal *<merchant> ...` (out)  → Office · Supplies (auto)
* Recurring SaaS / utilities (all outflow)
    - Xero ACH                                      → SaaS · Productivity
    - Apple subscription billing                    → SaaS · Productivity
    - Adobe subscription billing                    → SaaS · Productivity
    - Pictory subscription billing                  → SaaS · Productivity
    - Soundraw subscription billing                 → SaaS · Productivity
    - Google billing                                → SaaS · Productivity
    - IPVM industry research subscription           → SaaS · Productivity
    - Claude.ai / ChatGPT subscription              → SaaS · AI
    - Verizon wireless / VZWRLSS variants           → Infrastructure · Network
* Banking · Fees
    - Monthly Fee Business Adv Fundamentals         → Banking · Fees
    - OVERDRAFT ITEM FEE (already covered by the
      existing "overdraft" rule — pinned here too)  → Banking · Fees
* Negative coverage (must NOT auto-categorize)
    - "Zelle Transfer ..." stays Uncategorized
    - PayPal-rule does not steal a non-PayPal outflow
    - A PayPal outflow does NOT get auto-mapped to Revenue · Marketplace
"""

from __future__ import annotations

import pytest

from app.categorization import categorize_transaction


def _suggest(blob: str, amount: float):
    """Run the engine on a single synthetic blob and return its suggestion."""
    return categorize_transaction(
        payee=None,
        description=blob,
        memo=None,
        amount=amount,
        overrides=None,
    )


# ----------------------------------------------------------------------
# 1. PayPal direction split
# ----------------------------------------------------------------------

def test_paypal_inbound_transfer_is_marketplace_revenue():
    # Synthetic shape: an ACH payout descriptor where PayPal is sending
    # the bank account funds (typical for a marketplace seller).
    s = _suggest("PAYPAL DES:TRANSFER ID:XXXXXXXX INDN:SYNTHETIC TEST PPD", amount=+50000)
    assert s.category == "Revenue · Marketplace", s
    assert s.kind == "revenue"
    assert s.status == "auto"
    assert s.sign_matched is True
    assert s.source == "vendor_rule:paypal"


def test_paypal_outbound_inst_xfer_is_office_supplies_expense():
    # Synthetic shape: an ACH descriptor where PayPal is pulling funds
    # FROM the bank account (an INST XFER / payout-style debit).
    s = _suggest("PAYPAL DES:INST XFER ID:XXXX INDN:SYNTHETIC TEST WEB", amount=-12345)
    assert s.category == "Office · Supplies", s
    assert s.kind == "expense"
    assert s.status == "auto"
    assert s.sign_matched is True
    assert s.source == "vendor_rule:paypal"


def test_paypal_outbound_card_swipe_is_office_supplies_expense():
    # Synthetic shape: a CHECKCARD line where PayPal is the payment
    # processor for an unknown sub-merchant. Sign is negative
    # (card swipe = always an outflow for the cardholder).
    s = _suggest(
        "CHECKCARD 0101 PAYPAL *SYNMERCH 000-000-0000 CA SYNTHETIC RECURRING",
        amount=-1999,
    )
    assert s.category == "Office · Supplies", s
    assert s.kind == "expense"
    assert s.status == "auto"
    assert s.sign_matched is True


def test_paypal_outbound_retry_pymt_is_office_supplies_expense():
    # Synthetic shape: PAYPAL DES:RETRY PYMT is the descriptor PayPal
    # uses when retrying a previously failed pull from the bank
    # account — must be an outflow (Office · Supplies expense).
    s = _suggest(
        "PAYPAL DES:RETRY PYMT ID:SYN INDN:SYNTHETIC TEST WEB",
        amount=-2500,
    )
    assert s.category == "Office · Supplies", s
    assert s.kind == "expense"
    assert s.status == "auto"


def test_paypal_outflow_is_never_auto_routed_to_marketplace_revenue():
    # Regression guard against the original mis-coding: a PayPal pull
    # from the account (negative amount) must not auto-post as revenue.
    s = _suggest("PAYPAL DES:INST XFER ID:SYN INDN:SYNTHETIC", amount=-9999)
    assert s.category != "Revenue · Marketplace"


# ----------------------------------------------------------------------
# 2. Recurring SaaS subscriptions
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "blob,expected_category",
    [
        # Synthetic shape: Xero's ACH descriptor on an outbound charge.
        ("Xero Inc DES:Xero US IN ID:ST-XXXXX INDN:SYNTHETIC CO CCD",
         "SaaS · Productivity"),
        # Synthetic shape: Apple's recurring billing descriptor.
        ("CHECKCARD 0101 APPLE.COM/BILL 000-000-0000 CA SYNTHETIC RECURRING",
         "SaaS · Productivity"),
        # Synthetic shape: Adobe Creative Cloud / Acrobat descriptor.
        ("CHECKCARD 0101 ADOBE *PRODUCTS 000-000-0000 CA SYNTHETIC RECURRING",
         "SaaS · Productivity"),
        # Synthetic shape: Adobe descriptor with no asterisk variant.
        ("PURCHASE 0101 ADOBE INC. 000-000-0000 CA",
         "SaaS · Productivity"),
        # Synthetic shape: Pictory.ai subscription.
        ("PICTORY* PICTORY.AI PICTORY.AI WA SYNTHETIC",
         "SaaS · Productivity"),
        # Synthetic shape: Soundraw AI music subscription.
        ("SOUNDRAW INC. (EC SYNTHETIC-JP 00 SYNTHETIC",
         "SaaS · Productivity"),
        # Synthetic shape: IPVM video-surveillance research subscription.
        ("CHECKCARD 0101 IPVM IP VIDEO MARKET I IPVM.COM PA SYNTHETIC RECURRING",
         "SaaS · Productivity"),
        # Synthetic shape: Google Workspace / Google One / etc.
        ("CHECKCARD 0101 GOOGLE *Google Mountain ViewCA SYNTHETIC RECURRING",
         "SaaS · Productivity"),
    ],
)
def test_recurring_saas_subscriptions_auto_categorize(blob, expected_category):
    s = _suggest(blob, amount=-1599)
    assert s.category == expected_category, s
    assert s.kind == "expense"
    assert s.status == "auto"
    assert s.sign_matched is True


@pytest.mark.parametrize(
    "blob",
    [
        # Synthetic shape: Claude.ai subscription billing.
        "CHECKCARD 0101 CLAUDE.AI SUBSCRI CA SYNTHETIC RECURRING",
        # Synthetic shape: ChatGPT subscription billing.
        "CHECKCARD 0101 CHATGPT SUBSCRIPT CA SYNTHETIC RECURRING",
    ],
)
def test_ai_subscriptions_route_to_saas_ai(blob):
    s = _suggest(blob, amount=-2000)
    assert s.category == "SaaS · AI", s
    assert s.kind == "expense"
    assert s.status == "auto"


# ----------------------------------------------------------------------
# 3. Verizon / wireless telecom → Infrastructure · Network
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "blob",
    [
        # Synthetic shape: Verizon Wireless billing-pay descriptor.
        "CHECKCARD 0101 VZWRLSS*APOCC VISE 000-000-0000 FL SYNTHETIC RECURRING",
        # Synthetic shape: Verizon bill-pay variant.
        "CHECKCARD 0101 VZWRLSS*BILL PAY VE 000-000-0000 CA SYNTHETIC RECURRING",
        # Synthetic shape: spelled-out VERIZONWRLSS variant.
        "CHECKCARD 0101 VERIZONWRLSS*RTCCR VE 000-000-0000 FL SYNTHETIC RECURRING",
        # Synthetic shape: VERIZON WRLS IVR variant.
        "CHECKCARD 0101 VERIZON WRLS IVR VE 000-000-0000 CA SYNTHETIC",
    ],
)
def test_verizon_telecom_routes_to_infrastructure_network(blob):
    s = _suggest(blob, amount=-9999)
    assert s.category == "Infrastructure · Network", s
    assert s.kind == "expense"
    assert s.status == "auto"


# ----------------------------------------------------------------------
# 4. Banking fees
# ----------------------------------------------------------------------

def test_monthly_business_account_fee_is_banking_fees():
    # Synthetic shape: BofA business-checking monthly maintenance fee.
    s = _suggest("Monthly Fee Business Adv Fundamentals", amount=-1600)
    assert s.category == "Banking · Fees", s
    assert s.kind == "expense"
    assert s.status == "auto"


def test_overdraft_item_fee_is_banking_fees():
    # Synthetic shape: BofA overdraft item fee descriptor. Already
    # covered by the long-standing "overdraft" rule — pinned here so
    # any future rule reshuffle doesn't accidentally regress it.
    s = _suggest("OVERDRAFT ITEM FEE FOR ACTIVITY OF 99-99", amount=-1000)
    assert s.category == "Banking · Fees", s
    assert s.status == "auto"


# ----------------------------------------------------------------------
# 5. Negative coverage — DO NOT auto-categorize
# ----------------------------------------------------------------------

def test_zelle_transfer_remains_uncategorized():
    # Spec: Zelle transfers are owner-distribution / contractor /
    # personal-transfer territory and must NOT auto-categorize. They
    # should flow to /approvals for human judgment.
    s = _suggest("Zelle Transfer Conf# XXXXXXXXX; SYNTHETIC TEST", amount=-100000)
    assert s.category == "Uncategorized"
    assert s.status == "unknown"


def test_bare_checkcard_prefix_without_known_vendor_remains_uncategorized():
    # Generic prefixes ("CHECKCARD", "PURCHASE", "MOBILE PURCHASE")
    # without a recognized merchant signature must NOT auto-categorize.
    s = _suggest("CHECKCARD 0101 UNKNOWN MERCHANT 000-000-0000 SC", amount=-2500)
    assert s.category == "Uncategorized"
    assert s.status == "unknown"


def test_paypal_outflow_does_not_steal_a_non_paypal_outflow():
    # The PayPal outflow rule must only fire when "paypal" actually
    # appears in the blob. A descriptor with no PayPal substring stays
    # Uncategorized regardless of sign.
    s = _suggest("MOBILE PURCHASE 0101 SYN MERCHANT CHARLESTON SC", amount=-1000)
    assert s.category == "Uncategorized"
