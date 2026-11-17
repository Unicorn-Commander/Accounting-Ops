"""PostgreSQL proof for the Stripe approval turn-on path."""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from app.connectors.stripe.client import StripeCredentialBlob, StripePaginationResult
from scripts import stripe_sync
from tests.conftest import TEST_ENTITY_ID, TEST_WORKSPACE_ID, StackConfig
from tests.integration.test_approvals import _decide

pytestmark = pytest.mark.integration


CREATED = 1_794_268_800  # 2026-11-10, inside the default backfill window


def _txn(txn_id: str, amount: int) -> dict[str, Any]:
    return {
        "id": txn_id,
        "object": "balance_transaction",
        "type": "charge",
        "amount": amount,
        "currency": "usd",
        "fee": 0,
        "net": amount,
        "created": CREATED,
        "available_on": CREATED,
        "reporting_category": "charge",
    }


async def _prepare(db: Any, txn: dict[str, Any]) -> None:
    """Seed Stripe COA/state and remove only this test's prior external ref."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            await conn.execute(
                """
                INSERT INTO chart_of_account (
                    workspace_id, entity_id, account_number, account_name,
                    canonical_form, account_type, normal_balance,
                    is_active, is_system, notes
                ) VALUES
                    ($1, $2, '1210', 'Stripe Receivable',
                     '1210·Stripe Receivable', 'asset', 'debit', true, false,
                     'Stripe approval integration test'),
                    ($1, $2, '4200', 'AI Infrastructure Revenue (UC-Cloud)',
                     '4200·AI Infrastructure Revenue (UC-Cloud)', 'revenue',
                     'credit', true, false, 'Stripe approval integration test')
                ON CONFLICT (entity_id, account_number)
                DO UPDATE SET is_active = true
                """,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
            )
            await conn.execute(
                """
                DELETE FROM approval_queue
                WHERE entity_id = $1
                  AND payload ->> 'stripe_balance_txn_id' = $2
                """,
                str(TEST_ENTITY_ID),
                txn["id"],
            )
            await conn.execute(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (
                    gen_random_uuid(), $1, $2, 'stripe_api', 'default',
                    '{}'::jsonb, NULL
                )
                ON CONFLICT (entity_id, connector_name, connector_instance_name)
                DO UPDATE SET state = '{}'::jsonb, last_sync_at = NULL
                """,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
            )


async def _run_sync(
    monkeypatch: pytest.MonkeyPatch,
    txn: dict[str, Any],
    seeded_workspace: StackConfig,
) -> dict[str, Any]:
    async def load_credential(*args: Any, **kwargs: Any) -> StripeCredentialBlob:
        return StripeCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="stripe approval integration test",
            api_key="rk_test_000000000000000000000000",
            account_id="acct_test_fictional",
            environment="test",
            entity_id=str(TEST_ENTITY_ID),
        )

    async def fake_paginator(*args: Any, **kwargs: Any) -> StripePaginationResult:
        return StripePaginationResult(
            items=[txn],
            exhausted=True,
            continuation_cursor=None,
            window_lt=CREATED + 1,
        )

    async def entity_for_credential(*args: Any, **kwargs: Any):
        return TEST_ENTITY_ID

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(stripe_sync, "load_stripe_credential", load_credential)
    monkeypatch.setattr(stripe_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(stripe_sync, "paginate_balance_transactions", fake_paginator)
    monkeypatch.setattr(stripe_sync, "reconcile_one_entity", fake_reconcile)
    return await stripe_sync._sync_one_workspace(TEST_WORKSPACE_ID)


async def _pending_id(db: Any, txn_id: str) -> str:
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            return await conn.fetchval(
                """
                SELECT id::text FROM approval_queue
                WHERE entity_id = $1 AND subject_type = 'stripe_post_balance_transaction'
                  AND status = 'pending'
                  AND payload ->> 'stripe_balance_txn_id' = $2
                ORDER BY created_at DESC LIMIT 1
                """,
                str(TEST_ENTITY_ID),
                txn_id,
            )


async def _counts(db: Any, txn_id: str) -> tuple[int, int, str | None, dict[str, Any]]:
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM journal_entry
                     WHERE entity_id = $1 AND source = 'stripe_api'
                       AND source_external_id = $2) AS journal_count,
                    (SELECT count(*) FROM decision_audit
                     WHERE entity_id = $1 AND tool_name = 'approval_decision'
                       AND reasoning_trace LIKE '%fictional reviewer reason%') AS audit_count,
                    (SELECT status::text FROM approval_queue
                     WHERE entity_id = $1
                       AND payload ->> 'stripe_balance_txn_id' = $2
                     ORDER BY created_at DESC LIMIT 1) AS approval_status,
                    (SELECT state FROM connector_state
                     WHERE entity_id = $1 AND connector_name = 'stripe_api'
                       AND connector_instance_name = 'default') AS state
                """,
                str(TEST_ENTITY_ID),
                txn_id,
            )
    state = row["state"]
    if isinstance(state, str):
        state = json.loads(state)
    return row["journal_count"], row["audit_count"], row["approval_status"], state


async def _persist_block(db: Any, txn: dict[str, Any], cursor: str) -> None:
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                """
                UPDATE connector_state
                SET state = jsonb_build_object(
                    'blocked_item', jsonb_build_object(
                        'id', $2, 'amount', $3, 'currency', $4,
                        'error', 'fictional persisted mapper failure'
                    ),
                    '_stripe_continuation_cursor', $5,
                    'window_gte', $6,
                    'window_lt', $7
                )
                WHERE entity_id = $1 AND connector_name = 'stripe_api'
                  AND connector_instance_name = 'default'
                """,
                str(TEST_ENTITY_ID),
                txn["id"],
                txn["amount"],
                txn["currency"],
                cursor,
                CREATED,
                CREATED + 1,
            )


@pytest.mark.asyncio
async def test_first_run_approve_posts_and_next_sync_advances(
    db: Any, seeded_workspace: StackConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn = _txn(f"stripe-pg-approval-post-{uuid4().hex}", 1000)
    await _prepare(db, txn)

    first = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert first["last_outcome"] == "awaiting_review"
    approval_id = await _pending_id(db, txn["id"])
    assert approval_id

    decided = await _decide(
        seeded_workspace.base_url,
        seeded_workspace.token,
        approval_id,
        "approve",
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["applied"] is True
    journal_count, _, status, _ = await _counts(db, txn["id"])
    assert journal_count == 1
    assert status == "approved"

    # A retried approval is a no-op, and the external ref remains unique.
    repeated = await _decide(
        seeded_workspace.base_url,
        seeded_workspace.token,
        approval_id,
        "approve",
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["idempotent"] is True
    journal_count, _, _, _ = await _counts(db, txn["id"])
    assert journal_count == 1

    second = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert second["last_outcome"] == "ok"
    _, _, _, state = await _counts(db, txn["id"])
    assert state.get("_stripe_continuation_cursor") is None
    assert state.get("last_outcome") == "ok"


@pytest.mark.asyncio
async def test_materiality_blocked_approval_returns_409_and_holds_chain(
    db: Any, seeded_workspace: StackConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn = _txn(f"stripe-pg-approval-materiality-{uuid4().hex}", 150000)
    await _prepare(db, txn)

    first = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert first["last_outcome"] == "awaiting_review"
    approval_id = await _pending_id(db, txn["id"])
    response = await _decide(
        seeded_workspace.base_url,
        seeded_workspace.token,
        approval_id,
        "approve",
    )
    assert response.status_code == 409
    assert "requires explicit human confirmation" in response.text
    journal_count, _, status, _ = await _counts(db, txn["id"])
    assert journal_count == 0
    assert status == "pending"

    held = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert held["last_outcome"] == "awaiting_review"
    _, _, _, state = await _counts(db, txn["id"])
    assert state.get("_stripe_continuation_cursor") is None
    assert state.get("last_outcome") == "awaiting_review"


@pytest.mark.asyncio
async def test_reject_records_reason_and_next_sync_advances_without_posting(
    db: Any, seeded_workspace: StackConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn = _txn(f"stripe-pg-approval-reject-{uuid4().hex}", 1000)
    await _prepare(db, txn)

    first = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert first["last_outcome"] == "awaiting_review"
    approval_id = await _pending_id(db, txn["id"])
    response = await _decide(
        seeded_workspace.base_url,
        seeded_workspace.token,
        approval_id,
        "reject",
        {"note": "fictional reviewer reason"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is False

    second = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert second["last_outcome"] == "ok"
    journal_count, audit_count, status, state = await _counts(db, txn["id"])
    assert journal_count == 0
    assert audit_count >= 1
    assert status == "rejected"
    assert state.get("last_outcome") == "ok"


@pytest.mark.asyncio
async def test_reject_blocked_approval_keeps_block_and_cursor(
    db: Any, seeded_workspace: StackConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn = _txn(f"stripe-pg-approval-blocked-reject-{uuid4().hex}", 1000)
    await _prepare(db, txn)

    first = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert first["last_outcome"] == "awaiting_review"
    approval_id = await _pending_id(db, txn["id"])
    cursor = f"stripe-pg-blocked-cursor-{uuid4().hex}"
    await _persist_block(db, txn, cursor)

    response = await _decide(
        seeded_workspace.base_url,
        seeded_workspace.token,
        approval_id,
        "reject",
        {"note": "fictional blocked rejection"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] is False

    second = await _run_sync(monkeypatch, txn, seeded_workspace)
    assert second["last_outcome"] == "blocked_item"
    journal_count, _, status, state = await _counts(db, txn["id"])
    assert journal_count == 0
    assert status == "rejected"
    assert state["blocked_item"]["id"] == txn["id"]
    assert state["_stripe_continuation_cursor"] == cursor
