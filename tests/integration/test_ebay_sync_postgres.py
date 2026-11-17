"""Real PostgreSQL proof for eBay item savepoint and cursor isolation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text

from app.connectors.ebay import mapper as ebay_mapper
from app.connectors.ebay.client import (
    EbayCredentialBlob,
    EbayPaginationResult,
)
from scripts import ebay_sync
from tests.conftest import TEST_ENTITY_ID, TEST_WORKSPACE_ID


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_db_error_rolls_back_only_failed_ebay_item(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real statement error rolls back item k, not items before it."""
    del seeded_workspace
    payout_date = "2026-11-10T00:00:00.000Z"
    first_id = "ebay-pg-savepoint-1"
    failed_id = "ebay-pg-savepoint-2"
    payouts = [
        {
            "payoutId": first_id,
            "amount": {"value": "10.00", "currency": "USD"},
            "payoutDate": payout_date,
            "payoutStatus": "SUCCEEDED",
        },
        {
            "payoutId": failed_id,
            "amount": {"value": "11.00", "currency": "USD"},
            "payoutDate": payout_date,
            "payoutStatus": "SUCCEEDED",
        },
    ]

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            await conn.executemany(
                """
                INSERT INTO chart_of_account (
                    workspace_id, entity_id, account_number, account_name,
                    canonical_form, account_type, normal_balance,
                    is_active, is_system, notes
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, true, false, $8)
                ON CONFLICT (entity_id, account_number)
                DO UPDATE SET is_active = true
                """,
                [
                    (
                        str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "1220",
                        "eBay Payouts Receivable", "1220·eBay Payouts Receivable",
                        "asset", "debit", "eBay savepoint integration test",
                    ),
                    (
                        str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "4150",
                        "eBay Resale Revenue", "4150·eBay Resale Revenue",
                        "revenue", "credit", "eBay savepoint integration test",
                    ),
                ],
            )
            await conn.execute(
                """
                DELETE FROM journal_entry
                WHERE entity_id = $1 AND source = 'ebay_finances'
                  AND source_external_id = ANY($2::text[])
                """,
                str(TEST_ENTITY_ID), [first_id, failed_id],
            )
            await conn.execute(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (
                    gen_random_uuid(), $1, $2, 'ebay_finances', 'default',
                    '{}'::jsonb, NULL
                )
                ON CONFLICT (entity_id, connector_name, connector_instance_name)
                DO UPDATE SET state = '{}'::jsonb, last_sync_at = NULL
                """,
                str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID),
            )

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return EbayCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="postgres savepoint test",
            client_id="fictional-app",
            client_secret="fictional-secret",
            refresh_token="fictional-refresh",
            environment="sandbox",
            entity_id=str(TEST_ENTITY_ID),
        )

    class FakeClient:
        async def list_payouts(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(
                items=payouts,
                exhausted=True,
                continuation_offset=None,
                window_start=datetime(2026, 11, 9, tzinfo=timezone.utc),
                window_end=datetime(2026, 11, 10, 0, 0, 1, tzinfo=timezone.utc),
            )

        async def list_transactions(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(
                items=[],
                exhausted=True,
                continuation_offset=None,
            )

    async def entity_for_credential(*args: Any, **kwargs: Any):
        return TEST_ENTITY_ID

    async def mapper_with_real_db_error(
        session: Any, *, txn: dict[str, Any], **kwargs: Any
    ):
        if txn.get("transactionId") == failed_id:
            await session.execute(text("SELECT 1 / 0"))
        return await ebay_mapper.map_and_post_transaction(
            session, txn=txn, **kwargs
        )

    async def no_reconciliation(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(
        ebay_sync.EbayFinancesClient,
        "from_credential",
        classmethod(lambda cls, blob: FakeClient()),
    )
    monkeypatch.setattr(ebay_sync, "map_and_post_transaction", mapper_with_real_db_error)
    async def existing_ebay_posts(*args: Any, **kwargs: Any) -> int:
        return 31

    monkeypatch.setattr(ebay_mapper, "_count_existing_ebay_posts", existing_ebay_posts)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", no_reconciliation)

    summary = await ebay_sync._sync_one_workspace(TEST_WORKSPACE_ID)
    assert summary["last_outcome"] == "processing_error"

    async with app_db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            first = await conn.fetchval(
                """
                SELECT count(*) FROM journal_entry
                WHERE entity_id = $1 AND source = 'ebay_finances'
                  AND source_external_id = $2
                """,
                str(TEST_ENTITY_ID), first_id,
            )
            failed = await conn.fetchval(
                """
                SELECT count(*) FROM journal_entry
                WHERE entity_id = $1 AND source = 'ebay_finances'
                  AND source_external_id = $2
                """,
                str(TEST_ENTITY_ID), failed_id,
            )
            state = await conn.fetchrow(
                """
                SELECT last_sync_at, state
                FROM connector_state
                WHERE entity_id = $1 AND connector_name = 'ebay_finances'
                  AND connector_instance_name = 'default'
                """,
                str(TEST_ENTITY_ID),
            )

    assert first == 1
    assert failed == 0
    assert state["last_sync_at"] is None
    assert state["state"]["_ebay_failure_counts"] == {failed_id: 1}
    assert state["state"]["_ebay_continuation_cursor"]["payouts"]["offset"] == 1
