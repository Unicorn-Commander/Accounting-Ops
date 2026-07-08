"""Unit tests for the eBay → journal mapper.

Pure-Python paths: we don't hit Postgres. The session is stubbed so we can
exercise the decision tree (account resolution, idempotency, first-run guard,
materiality routing) without a live DB.

What we cover:
  - SALE / REFUND / FEE / PAYOUT account routing
  - Unknown txn type returns blocked_unknown_type
  - Missing COA returns blocked_unknown_account
  - Zero amount returns skipped_zero_amount
  - Idempotency hit returns blocked_idempotent
  - First-run guard queues first 30 rows
  - normalize_payout_as_txn shape
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from app.connectors.ebay import mapper as ebay_mapper


# --- Test helpers -----------------------------------------------------------


class _StubResult:
    """SQLAlchemy Result stand-in supporting .first() / .mappings().first()."""

    def __init__(self, rows: list[Any]):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def mappings(self):
        return self

    def all(self):
        return self._rows


@dataclass
class _Row:
    """Minimal Row-like object with attribute access (mimics SA Row)."""

    _data: dict[str, Any]

    def __post_init__(self):
        for k, v in self._data.items():
            setattr(self, k, v)

    def __getitem__(self, k):
        return self._data[k]

    def keys(self):
        return self._data.keys()


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StubSession:
    """A minimal AsyncSession stub.

    Provides .execute() that picks a stub response per (lookup_key, sql_marker).
    """

    def __init__(self, *, accounts: dict[str, str], existing_posts: int = 0):
        # accounts: {"1010": canonical_form, "1220": ...}
        self.accounts = accounts
        self.existing_posts = existing_posts
        self.posted_entries: list[dict] = []
        self.posting_log: list[dict] = []
        self.approvals: list[dict] = []
        self.idempotent_hit = False
        self.linked_external_id_response = None  # if set, _resolve_cash_for_payout returns this

    def begin_nested(self):
        return _Savepoint()

    async def execute(self, query, params=None):
        sql = str(query)
        params = params or {}

        # COA lookup by account_number
        if "FROM chart_of_account" in sql and "account_number" in sql and "linked_external_account_id" not in sql:
            num = params.get("num")
            canonical = self.accounts.get(num)
            if not canonical:
                return _StubResult([])
            return _StubResult([_Row({
                "canonical_form": canonical,
                "account_type": "asset" if num.startswith("1") else (
                    "revenue" if num.startswith("4") else "expense"
                ),
            })])

        # COA lookup by linked external
        if "linked_external_account_id" in sql:
            if self.linked_external_id_response:
                return _StubResult([_Row(self.linked_external_id_response)])
            return _StubResult([])

        # Idempotency check
        if "FROM journal_entry" in sql and "source_external_id = :ext" in sql:
            if self.idempotent_hit:
                return _StubResult([_Row({"id": "00000000-0000-0000-0000-000000000001"})])
            return _StubResult([])

        # closed_period check
        if "FROM closed_period" in sql:
            return _StubResult([])

        # existing eBay post count
        if "FROM journal_entry" in sql and "AND source = :src" in sql and "count(*)" in sql:
            return _StubResult([_Row({"c": self.existing_posts})])

        # Hash chain last hash
        if "FROM journal_entry" in sql and "this_hash IS NOT NULL" in sql:
            return _StubResult([])  # GENESIS_HASH default

        # _insert_journal_entry: capture
        if "INSERT INTO journal_entry" in sql:
            self.posted_entries.append(dict(params))
            return _StubResult([])

        # _log_post_attempt
        if "INSERT INTO posting_log" in sql:
            self.posting_log.append(dict(params))
            return _StubResult([])

        # approval_queue insert
        if "INSERT INTO approval_queue" in sql:
            self.approvals.append(dict(params))
            return _StubResult([])

        # Fallback
        return _StubResult([])


def _txn(
    txn_id: str,
    txn_type: str,
    amount: str = "10.00",
    *,
    order_id: str | None = "ORD-99887766",
    payout_instrument: dict | None = None,
    payout_date: str | None = None,
    transaction_date: str = "2026-05-01T12:00:00.000Z",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "transactionId": txn_id,
        "transactionType": txn_type,
        "amount": {"value": amount, "currency": "USD"},
        "transactionDate": transaction_date,
    }
    if order_id:
        body["orderId"] = order_id
    if payout_instrument:
        body["payoutInstrument"] = payout_instrument
    if payout_date:
        body["payoutDate"] = payout_date
    return body


# ---- canonical account map for the test entity -----
_ACCOUNTS = {
    "1010": "1010·Cash · Primary Checking",
    "1220": "1220·eBay Payouts Receivable",
    "4150": "4150·eBay Resale Revenue",
    "5520": "5520·Payment Processor Fees",
}


# --- Tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sale_routes_to_revenue_after_first_run_guard():
    # Past the first-30 review window so we should auto-post a tier-L row.
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    txn = _txn("TXN-SALE-1", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "posted", result.reason
    # Verify the posted entry has the right debit/credit
    assert session.posted_entries, "no journal_entry insert recorded"
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["1220"]
    assert entry["credit"] == _ACCOUNTS["4150"]
    assert entry["source"] == "ebay_finances"
    assert entry["source_external_id"] == "TXN-SALE-1"
    # Payee must NOT carry PII; should be opaque order ref
    assert entry["payee"].startswith("eBay SALE")
    assert "ORD-99887766"[-8:] in entry["payee"]


@pytest.mark.asyncio
async def test_refund_reverses_sale_routing():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    txn = _txn("TXN-REF-1", "REFUND", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["4150"]
    assert entry["credit"] == _ACCOUNTS["1220"]


@pytest.mark.asyncio
async def test_fee_routes_to_processor_fees():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    txn = _txn("TXN-FEE-1", "FEE", "1.25")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["5520"]
    assert entry["credit"] == _ACCOUNTS["1220"]


@pytest.mark.asyncio
async def test_payout_routes_to_cash_default_account():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    # PAYOUT shape via normalize_payout_as_txn
    payout = {
        "payoutId": "PAY-12345678",
        "amount": {"value": "50.00", "currency": "USD"},
        "payoutDate": "2026-05-02T08:00:00.000Z",
        "payoutStatus": "SUCCEEDED",
    }
    txn = ebay_mapper.normalize_payout_as_txn(payout)
    assert txn["transactionType"] == "PAYOUT"
    assert txn["transactionId"] == "PAY-12345678"
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "posted", result.reason
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["1010"]  # cash
    assert entry["credit"] == _ACCOUNTS["1220"]


@pytest.mark.asyncio
async def test_unknown_type_blocked():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    txn = _txn("TXN-X-1", "TRANSFER", "5.00")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "blocked_unknown_type"
    assert not session.posted_entries


@pytest.mark.asyncio
async def test_missing_coa_blocked():
    """Even with a known type, no COA = no post."""
    session = _StubSession(accounts={}, existing_posts=31)
    txn = _txn("TXN-NOCOA", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "blocked_unknown_account"
    assert not session.posted_entries


@pytest.mark.asyncio
async def test_zero_amount_skipped():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    txn = _txn("TXN-ZERO", "SALE", "0.00")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "skipped_zero_amount"


@pytest.mark.asyncio
async def test_idempotent_hit_returns_blocked_idempotent():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    session.idempotent_hit = True
    txn = _txn("TXN-DUP-1", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "blocked_idempotent"
    assert result.journal_entry_id is not None


@pytest.mark.asyncio
async def test_first_run_guard_queues_first_30():
    """With 0-29 existing ebay posts, every result is queued_first_run."""
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=0)
    txn = _txn("TXN-FIRST-RUN", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "queued_first_run"
    assert result.approval_id is not None
    assert len(session.approvals) == 1
    # Verify the queue carries the structured action
    import json as _json

    payload = _json.loads(session.approvals[0]["payload"])
    assert payload["action"] == "ebay_post_transaction"
    assert payload["ebay_type"] == "SALE"


@pytest.mark.asyncio
async def test_first_run_exactly_at_threshold_posts():
    """existing_posts == FIRST_RUN_REVIEW_LIMIT means we're past the guard."""
    session = _StubSession(
        accounts=_ACCOUNTS,
        existing_posts=ebay_mapper.FIRST_RUN_REVIEW_LIMIT,
    )
    txn = _txn("TXN-LIMIT", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"


@pytest.mark.asyncio
async def test_pii_not_in_payee_or_description():
    """Buyer info must never land in the journal entry's payee/description."""
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    txn = _txn("TXN-PII", "SALE", "10.00")
    txn["buyer"] = {"username": "very_secret_buyer_42", "buyerId": "BUYER123"}
    result = await ebay_mapper.map_and_post_transaction(
        session, workspace_id="wid", entity_id="eid", txn=txn, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"
    entry = session.posted_entries[0]
    blob = (entry["payee"] or "") + "|" + (entry["description"] or "") + "|" + (entry["memo"] or "")
    assert "very_secret_buyer_42" not in blob
    assert "BUYER123" not in blob


def test_normalize_payout_as_txn_shape():
    payout = {
        "payoutId": "PAY-ABCDEFGH",
        "amount": {"value": "100.00", "currency": "USD"},
        "payoutDate": "2026-05-15T00:00:00.000Z",
        "payoutStatus": "SUCCEEDED",
        "payoutInstrument": {"accountLastFourDigits": "1234"},
    }
    out = ebay_mapper.normalize_payout_as_txn(payout)
    assert out["transactionId"] == "PAY-ABCDEFGH"
    assert out["transactionType"] == "PAYOUT"
    assert out["amount"] == payout["amount"]
    assert out["_raw_payout"] is True
    assert out["payoutInstrument"]["accountLastFourDigits"] == "1234"


def test_decimal_to_cents_handles_formats():
    assert ebay_mapper._decimal_to_cents("10.00") == 1000
    assert ebay_mapper._decimal_to_cents("0") == 0
    assert ebay_mapper._decimal_to_cents("0.01") == 1
    assert ebay_mapper._decimal_to_cents(None) == 0
    assert ebay_mapper._decimal_to_cents("not-a-number") == 0
    assert ebay_mapper._decimal_to_cents(Decimal("100.5")) == 10050


def test_opaque_payee_strips_buyer_info():
    txn = {
        "transactionType": "SALE",
        "orderId": "ORDER-ABCD1234",
        "buyer": {"username": "leaky_username"},
    }
    payee = ebay_mapper._opaque_payee(txn)
    assert "leaky_username" not in payee
    assert "ABCD1234" in payee
    assert payee.startswith("eBay SALE")
