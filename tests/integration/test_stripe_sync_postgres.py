"""Real PostgreSQL proof for Stripe item savepoint isolation."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.connectors.stripe import mapper as stripe_mapper
from app.connectors.stripe.client import StripeCredentialBlob, StripePaginationResult
from scripts import stripe_sync
from tests.conftest import TEST_ENTITY_ID, TEST_WORKSPACE_ID


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_db_error_rolls_back_only_failed_stripe_item(
    db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real statement error rolls back item k, not items before it."""
    del seeded_workspace
    created = 1_794_268_800  # 2026-11-10, inside the default backfill window
    first_id = "stripe-pg-savepoint-1"
    failed_id = "stripe-pg-savepoint-2"
    transactions = [
        {
            "id": first_id,
            "object": "balance_transaction",
            "type": "charge",
            "amount": 1000,
            "currency": "usd",
            "fee": 0,
            "net": 1000,
            "created": created,
            "available_on": created,
            "reporting_category": "charge",
        },
        {
            "id": failed_id,
            "object": "balance_transaction",
            "type": "charge",
            "amount": 1100,
            "currency": "usd",
            "fee": 0,
            "net": 1100,
            "created": created - 1,
            "available_on": created - 1,
            "reporting_category": "charge",
        },
    ]

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
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

    async def load_credential(*args: Any, **kwargs: Any) -> StripeCredentialBlob:
        return StripeCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="postgres savepoint test",
            api_key="rk_test_000000000000000000000000",
            account_id="acct_test_fictional",
            environment="test",
            entity_id=str(TEST_ENTITY_ID),
        )

    async def fake_paginator(*args: Any, **kwargs: Any) -> StripePaginationResult:
        return StripePaginationResult(
            items=transactions,
            exhausted=True,
            continuation_cursor=None,
            window_lt=created + 1,
        )

    async def existing_posts(*args: Any, **kwargs: Any) -> int:
        return 31

    async def entity_for_credential(*args: Any, **kwargs: Any):
        return TEST_ENTITY_ID

    async def mapper_with_real_db_error(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        if txn["id"] == failed_id:
            await session.execute(text("SELECT 1 / 0"))
        return await stripe_mapper.map_and_post_balance_transaction(
            session, txn=txn, **kwargs
        )

    monkeypatch.setattr(stripe_sync, "load_stripe_credential", load_credential)
    monkeypatch.setattr(stripe_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(stripe_sync, "paginate_balance_transactions", fake_paginator)
    monkeypatch.setattr(stripe_sync, "map_and_post_balance_transaction", mapper_with_real_db_error)
    monkeypatch.setattr(stripe_mapper, "_count_existing_stripe_posts", existing_posts)
    monkeypatch.setattr(
        stripe_sync,
        "reconcile_one_entity",
        lambda *args, **kwargs: _reconciliation(),
    )

    summary = await stripe_sync._sync_one_workspace(TEST_WORKSPACE_ID)
    assert summary["last_outcome"] == "processing_error"

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            first = await conn.fetchval(
                """
                SELECT count(*) FROM journal_entry
                WHERE entity_id = $1 AND source = 'stripe_api'
                  AND source_external_id = $2
                """,
                str(TEST_ENTITY_ID),
                first_id,
            )
            failed = await conn.fetchval(
                """
                SELECT count(*) FROM journal_entry
                WHERE entity_id = $1 AND source = 'stripe_api'
                  AND source_external_id = $2
                """,
                str(TEST_ENTITY_ID),
                failed_id,
            )
            state = await conn.fetchrow(
                """
                SELECT last_sync_at, state, state->>'_stripe_continuation_cursor' AS cursor
                FROM connector_state
                WHERE entity_id = $1 AND connector_name = 'stripe_api'
                  AND connector_instance_name = 'default'
                """,
                str(TEST_ENTITY_ID),
            )

    assert first == 1
    assert failed == 0
    assert state["last_sync_at"] is None
    assert state["cursor"] == first_id
    assert state["state"]["_stripe_failure_counts"] == {failed_id: 1}


async def _reconciliation() -> dict[str, int]:
    return {"checked_payouts": 0, "matched": 0, "unmatched": 0}
