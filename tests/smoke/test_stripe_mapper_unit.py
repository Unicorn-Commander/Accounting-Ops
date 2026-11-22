"""Unit tests for the Stripe → journal mapper.

Pure-Python paths: we don't hit Postgres. The session is stubbed so we can
exercise the decision tree (account resolution, idempotency, first-run guard,
materiality routing, sidecar fee entry) without a live DB.

What we cover:
  - charge / refund / stripe_fee / payout account routing
  - charge with fee !=0 emits sidecar fee entry
  - Unknown txn type returns blocked_unknown_type
  - Missing COA returns blocked_unknown_account
  - Zero amount (non-payout) returns skipped_zero_amount
  - Idempotency hit returns blocked_idempotent
  - First-run guard queues first 30 rows
  - application_fee / adjustment / transfer → flagged_for_cpa
  - PII guard (no customer email, no metadata in payee/description)
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.connectors.stripe import mapper as stripe_mapper


# --- Test helpers -----------------------------------------------------------


class _StubResult:
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
    """Minimal AsyncSession stub.

    Provides .execute() that picks a stub response per (lookup_key, sql_marker).
    Tracks posted_entries, approvals, posting_log so tests can assert.
    """

    def __init__(
        self,
        *,
        accounts: dict[str, str],
        existing_posts: int = 0,
        linked_external_id_response: dict | None = None,
    ):
        self.accounts = accounts
        self.existing_posts = existing_posts
        self.posted_entries: list[dict] = []
        self.posting_log: list[dict] = []
        self.approvals: list[dict] = []
        self.idempotent_hit = False
        self.fee_idempotent_hit = False
        self.linked_external_id_response = linked_external_id_response

    def begin_nested(self):
        return _Savepoint()

    async def execute(self, query, params=None):
        sql = str(query)
        params = params or {}

        # COA lookup by account_number (skipping linked_external check)
        if "FROM chart_of_account" in sql and "account_number" in sql and "linked_external_account_id" not in sql:
            num = params.get("num")
            canonical = self.accounts.get(num)
            if not canonical:
                return _StubResult([])
            return _StubResult([_Row({"canonical_form": canonical})])

        # COA lookup by linked external
        if "linked_external_account_id" in sql:
            if self.linked_external_id_response:
                return _StubResult([_Row(self.linked_external_id_response)])
            return _StubResult([])

        # Idempotency check: source_external_id = :ext
        if "FROM journal_entry" in sql and "source_external_id = :ext" in sql:
            ext = params.get("ext", "")
            # Distinguish parent vs fee sidecar
            if ext.endswith("#fee"):
                if self.fee_idempotent_hit:
                    return _StubResult([_Row({"id": "00000000-0000-0000-0000-000000000099"})])
                return _StubResult([])
            if self.idempotent_hit:
                return _StubResult([_Row({"id": "00000000-0000-0000-0000-000000000001"})])
            return _StubResult([])

        # closed_period check
        if "FROM closed_period" in sql:
            return _StubResult([])

        # Existing Stripe approval lookup. This models the payload-based
        # idempotency check used when a transaction is reread from overlap.
        if "FROM approval_queue" in sql and "stripe_balance_txn_id" in sql:
            stripe_id = params.get("stripe_id")
            expected_status = "rejected" if "status = 'rejected'" in sql else "pending"
            for approval in self.approvals:
                payload = json.loads(approval["payload"])
                if (
                    approval.get("status", "pending") == expected_status
                    and payload.get("stripe_balance_txn_id") == stripe_id
                ):
                    return _StubResult([_Row({"id": approval["id"]})])
            return _StubResult([])

        # existing Stripe post count
        if "FROM journal_entry" in sql and "AND source = :src" in sql and "count(*)" in sql:
            return _StubResult([_Row({"c": self.existing_posts})])

        # Hash chain last hash
        if "FROM journal_entry" in sql and "this_hash IS NOT NULL" in sql:
            return _StubResult([])

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
            approval = dict(params)
            approval["status"] = "pending"
            self.approvals.append(approval)
            return _StubResult([])

        # blocked alert refreshes the existing pending approval in place
        if "UPDATE approval_queue" in sql:
            for approval in self.approvals:
                if approval["id"] == params.get("id"):
                    approval["reasoning"] = params["reasoning"]
                    payload = json.loads(approval["payload"])
                    payload.update(json.loads(params["payload"]))
                    approval["payload"] = json.dumps(payload)
                    break
            return _StubResult([])

        # Fallback
        return _StubResult([])


class _NonIdempotencyIntegritySession(_StubSession):
    async def execute(self, query, params=None):
        if "INSERT INTO journal_entry" in str(query):
            original = RuntimeError("duplicate hash-chain value")
            original.diag = type(
                "Diag", (), {"constraint_name": "uq_je_entity_this_hash"}
            )()
            raise IntegrityError("insert journal_entry", {}, original)
        return await super().execute(query, params)


def _bt(
    bt_id: str,
    bt_type: str,
    amount: int = 1000,
    *,
    fee: int = 0,
    source_id: str | None = None,
    created: int = 1735689600,
    available_on: int | None = None,
) -> dict[str, Any]:
    """Build a synthetic Stripe balance_transaction."""
    body: dict[str, Any] = {
        "id": bt_id,
        "object": "balance_transaction",
        "type": bt_type,
        "amount": amount,
        "currency": "usd",
        "fee": fee,
        "net": amount - fee,
        "status": "available",
        "created": created,
        "available_on": available_on or created,
        "reporting_category": bt_type,
    }
    if source_id:
        body["source"] = {"id": source_id, "object": "charge"}
    return body


# Canonical account map for tests
_ACCOUNTS = {
    "1010": "1010·Cash · Primary Checking",
    "1210": "1210·Stripe Receivable",
    "4200": "4200·AI Infrastructure Revenue (UC-Cloud)",
    "5520": "5520·Payment Processor Fees",
}


# --- Tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_charge_routes_to_revenue_after_first_run_guard():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_charge_1", "charge", amount=999, fee=0, source_id="ch_abc12345")
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "posted", result.reason
    # Verify journal entry insert
    assert session.posted_entries, "no journal_entry insert recorded"
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["1210"]
    assert entry["credit"] == _ACCOUNTS["4200"]
    assert entry["source"] == "stripe_api"
    assert entry["source_external_id"] == "txn_charge_1"
    # Payee should be opaque (no PII)
    assert entry["payee"].startswith("Stripe charge")
    assert "ch_abc1" not in entry["payee"]  # source id not leaked in payee


@pytest.mark.asyncio
async def test_charge_with_fee_emits_sidecar_fee_entry():
    """charge with fee != 0 → posts parent + sidecar fee entry.

    Use a tier-L amount (<$75) so we exercise the auto-post path and verify
    both the parent journal entry AND the sidecar fee entry land.
    """
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_charge_w_fee", "charge", amount=5000, fee=175)  # $50 charge, $1.75 fee
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "posted", result.reason
    assert result.fee_journal_entry_id is not None
    # Two journal entries posted: parent + fee sidecar
    assert len(session.posted_entries) == 2
    parent, fee_entry = session.posted_entries
    assert parent["debit"] == _ACCOUNTS["1210"]
    assert parent["credit"] == _ACCOUNTS["4200"]
    assert parent["amount"] == 50  # $50
    assert fee_entry["debit"] == _ACCOUNTS["5520"]
    assert fee_entry["credit"] == _ACCOUNTS["1210"]
    assert fee_entry["source_external_id"] == "txn_charge_w_fee#fee"
    assert fee_entry["amount"] == pytest.approx(1.75)


@pytest.mark.asyncio
async def test_refund_reverses_charge_routing():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_refund_1", "refund", amount=-500, fee=0)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["4200"]
    assert entry["credit"] == _ACCOUNTS["1210"]


@pytest.mark.asyncio
async def test_stripe_fee_routes_to_processor_fees():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_fee_1", "stripe_fee", amount=-250)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["5520"]
    assert entry["credit"] == _ACCOUNTS["1210"]


@pytest.mark.asyncio
async def test_payout_routes_to_cash_default_account():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_payout_1", "payout", amount=-5000, source_id="po_abc12345")
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "posted", result.reason
    entry = session.posted_entries[0]
    assert entry["debit"] == _ACCOUNTS["1010"]
    assert entry["credit"] == _ACCOUNTS["1210"]


@pytest.mark.asyncio
async def test_unknown_type_blocked():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_x_1", "weird_new_type_2099", amount=100)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "blocked_unknown_type"
    assert not session.posted_entries


@pytest.mark.asyncio
async def test_application_fee_flagged_for_cpa():
    """application_fee / adjustment / transfer → CPA queue, never auto-posted."""
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_appfee_1", "application_fee", amount=200)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "flagged_for_cpa"
    assert result.approval_id is not None
    assert len(session.approvals) == 1
    assert not session.posted_entries  # never posted


@pytest.mark.asyncio
async def test_adjustment_flagged_for_cpa():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_adj_1", "adjustment", amount=-50)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "flagged_for_cpa"


@pytest.mark.asyncio
async def test_missing_coa_blocked():
    session = _StubSession(accounts={}, existing_posts=31)
    bt = _bt("txn_nocoa", "charge", amount=999)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "blocked_unknown_account"
    assert not session.posted_entries


@pytest.mark.asyncio
async def test_zero_amount_non_payout_skipped():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_zero", "charge", amount=0)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "skipped_zero_amount"


@pytest.mark.asyncio
async def test_idempotent_hit_returns_blocked_idempotent():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    session.idempotent_hit = True
    bt = _bt("txn_dup_1", "charge", amount=999)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "blocked_idempotent"
    assert result.journal_entry_id is not None


@pytest.mark.asyncio
async def test_non_idempotency_integrity_error_is_not_a_duplicate():
    session = _NonIdempotencyIntegritySession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_hash_race", "charge", amount=999)

    with pytest.raises(IntegrityError):
        await stripe_mapper.map_and_post_balance_transaction(
            session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
        )


@pytest.mark.asyncio
async def test_first_run_guard_queues_first_30():
    """With 0-29 existing Stripe posts, every result is queued_first_run."""
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=0)
    bt = _bt("txn_first_run", "charge", amount=999)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "queued_first_run"
    assert result.approval_id is not None
    assert len(session.approvals) == 1
    import json as _json

    payload = _json.loads(session.approvals[0]["payload"])
    assert payload["action"] == "stripe_post_balance_transaction"
    assert payload["stripe_type"] == "charge"


async def _replay(amount: int, **kw):
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=0)
    bt = _bt(f"txn_mat_{amount}", "charge", amount=amount)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane", **kw,
    )
    return session, result


@pytest.mark.asyncio
async def test_worker_path_still_queues_m_tier_even_past_first_run():
    """No human attestation: a $200 charge queues for confirm, never posts."""
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_m_worker", "charge", amount=20000)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "queued_for_confirm"
    assert not session.posted_entries


@pytest.mark.asyncio
async def test_decide_replay_without_attestation_cannot_post_m_tier():
    """first_run_confirmed alone does not clear the materiality gate."""
    session, result = await _replay(
        20000, first_run_confirmed=True, approving_human_uc_uid="jane",
    )
    assert result.outcome == "queued_for_confirm"
    assert not session.posted_entries


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [5000, 20000])
async def test_decide_replay_with_human_attestation_posts_l_and_m_once(amount):
    session, result = await _replay(
        amount, first_run_confirmed=True, approving_human_uc_uid="jane",
        human_attested=True,
    )
    assert result.outcome == "posted", result.reason
    assert len(session.posted_entries) == 1
    assert session.posted_entries[0]["debit"] == _ACCOUNTS["1210"]
    assert session.posted_entries[0]["credit"] == _ACCOUNTS["4200"]


@pytest.mark.asyncio
async def test_decide_replay_h_tier_stays_blocked_even_when_attested():
    """H-tier behaviour is unchanged: the decide path still gets blocked."""
    session, result = await _replay(
        150000, first_run_confirmed=True, approving_human_uc_uid="jane",
        human_attested=True,
    )
    assert result.outcome == "blocked_materiality"
    assert not session.posted_entries


@pytest.mark.asyncio
async def test_first_run_exactly_at_threshold_posts():
    """existing_posts == FIRST_RUN_REVIEW_LIMIT means we're past the guard."""
    session = _StubSession(
        accounts=_ACCOUNTS,
        existing_posts=stripe_mapper.FIRST_RUN_REVIEW_LIMIT,
    )
    bt = _bt("txn_at_limit", "charge", amount=999)
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"


@pytest.mark.asyncio
async def test_first_run_reread_does_not_enqueue_duplicate_approval():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=0)
    bt = _bt("txn_first_run_reread", "charge", amount=999)

    first = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    second = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )

    assert first.outcome == "queued_first_run"
    assert second.outcome == "queued_first_run"
    assert second.approval_id == first.approval_id
    assert len(session.approvals) == 1


@pytest.mark.asyncio
async def test_blocked_alert_updates_existing_pending_approval():
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=0)
    bt = _bt("txn_blocked_existing", "charge", amount=999)

    first = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    blocked = await stripe_mapper.enqueue_blocked_balance_transaction(
        session,
        workspace_id="wid",
        entity_id="eid",
        txn=bt,
        actor_uc_uid="jane",
        error="blocked_unknown_account: missing revenue account",
    )

    assert blocked == first.approval_id
    assert len(session.approvals) == 1
    assert session.approvals[0]["reasoning"] == (
        "(blocked) Stripe txn_blocked_existing 999: "
        "blocked_unknown_account: missing revenue account"
    )
    payload = json.loads(session.approvals[0]["payload"])
    assert payload["blocked"] is True
    assert payload["failure_detail"] == (
        "blocked_unknown_account: missing revenue account"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("decided_status", ["approved", "rejected"])
async def test_decided_approval_does_not_suppress_new_pending_approval(
    decided_status: str,
):
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=0)
    bt = _bt("txn_decided_then_reread", "charge", amount=999)

    first = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    session.approvals[0]["status"] = decided_status
    second = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )

    assert first.approval_id != second.approval_id
    assert len(session.approvals) == 2


@pytest.mark.asyncio
async def test_pii_not_in_payee_or_description():
    """Customer email / metadata must never land in journal entry."""
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    bt = _bt("txn_pii", "charge", amount=1000)
    # Stripe sometimes inlines billing details with PII
    bt["customer"] = "cus_VERY_SECRET_CUSTOMER"
    bt["billing_details"] = {"email": "secret@example.com", "name": "John Doe"}
    bt["metadata"] = {"customer_email": "leaked@example.com", "card_last4": "4242"}
    result = await stripe_mapper.map_and_post_balance_transaction(
        session, workspace_id="wid", entity_id="eid", txn=bt, actor_uc_uid="jane",
    )
    assert result.outcome == "posted"
    entry = session.posted_entries[0]
    blob = (entry["payee"] or "") + "|" + (entry["description"] or "") + "|" + (entry["memo"] or "")
    assert "secret@example.com" not in blob
    assert "John Doe" not in blob
    assert "4242" not in blob
    assert "VERY_SECRET" not in blob
    assert "leaked@example.com" not in blob


def test_amount_to_cents_handles_formats():
    assert stripe_mapper._amount_to_cents(1000) == 1000
    assert stripe_mapper._amount_to_cents("1000") == 1000
    assert stripe_mapper._amount_to_cents(0) == 0
    assert stripe_mapper._amount_to_cents(None) == 0
    assert stripe_mapper._amount_to_cents("not-a-number") == 0
    assert stripe_mapper._amount_to_cents(-500) == -500


def test_opaque_payee_strips_source_id():
    bt = {
        "type": "charge",
        "id": "txn_ABCDEF12345678",
        "source": {"id": "ch_LEAKY_SOURCE_ID"},
        "customer": "cus_LEAKY",
    }
    payee = stripe_mapper._opaque_payee(bt)
    assert "LEAKY" not in payee
    assert "12345678" in payee  # last 8 of the txn id is OK
    assert payee.startswith("Stripe charge")


def test_opaque_description_uses_reporting_category_only():
    bt = {
        "type": "charge",
        "id": "txn_X",
        "reporting_category": "charge",
        "description": "SECRET MERCHANT STATEMENT TEXT FROM CUSTOMER",
    }
    desc = stripe_mapper._opaque_description(bt)
    assert "SECRET" not in desc
    assert "MERCHANT" not in desc
    assert "Stripe charge" in desc


def test_parse_stripe_ts_handles_unix_epoch():
    # 2026-05-31 00:00:00 UTC = 1780185600 (just confirmed via date -u --date="..."
    # but the exact epoch isn't critical; assert it parses to a real date.)
    d = stripe_mapper._parse_stripe_ts(1780185600)
    assert d is not None
    assert d.year == 2026
    assert d.month == 5
    # A unix-epoch round trip should land on a sensible day, not Jan 1 1970.
    assert d.day in (30, 31)


def test_parse_stripe_ts_none_returns_none():
    assert stripe_mapper._parse_stripe_ts(None) is None
