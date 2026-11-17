"""Real PostgreSQL proof for eBay item savepoint and cursor isolation."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from app.db import SessionLocal
from app.connectors.ebay.decision_state import record_ebay_decision
from app.connectors.ebay import mapper as ebay_mapper
from app.connectors.ebay.client import (
    EbayCredentialBlob,
    EbayPaginationResult,
)
from app.workspace_session import workspace_session
from scripts import ebay_sync
from tests.conftest import TEST_ENTITY_ID, TEST_WORKSPACE_ID


pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_payout_linked_late_sale_is_not_lost_from_date_window(
    db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payout's immutable link controls its pull, even for an old sale."""
    del seeded_workspace
    payout_id = "ebay-pg-late-payout"
    transaction_id = "ebay-pg-late-sale"
    last_sync = datetime(2026, 11, 10, tzinfo=timezone.utc)
    payout_date = datetime(2026, 11, 12, tzinfo=timezone.utc)
    window_end = datetime(2026, 11, 12, 0, 0, 1, tzinfo=timezone.utc)
    seen: list[str] = []

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            await conn.execute(
                """
                DELETE FROM connector_state
                WHERE entity_id = $1 AND connector_name = 'ebay_finances'
                """,
                str(TEST_ENTITY_ID),
            )
            await conn.execute(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (
                    gen_random_uuid(), $1, $2, 'ebay_finances', 'default',
                    '{}'::jsonb, $3
                )
                """,
                str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), last_sync,
            )

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return EbayCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="postgres late-settlement test",
            client_id="fictional-app",
            client_secret="fictional-secret",
            refresh_token="fictional-refresh",
            environment="sandbox",
            entity_id=str(TEST_ENTITY_ID),
        )

    class FakeClient:
        async def list_payouts(self, **kwargs: Any) -> EbayPaginationResult:
            assert kwargs["start_date"] == datetime(2026, 11, 9, tzinfo=timezone.utc)
            return EbayPaginationResult(
                items=[
                    {
                        "payoutId": payout_id,
                        "amount": {"value": "10.00", "currency": "USD"},
                        "payoutDate": payout_date.isoformat(),
                        "payoutStatus": "SUCCEEDED",
                    }
                ],
                exhausted=True,
                continuation_offset=None,
                window_start=kwargs["start_date"],
                window_end=window_end,
            )

        async def list_transactions(self, **kwargs: Any) -> EbayPaginationResult:
            if kwargs.get("payout_id") == payout_id:
                assert "start_date" not in kwargs
                assert "end_date" not in kwargs
                return EbayPaginationResult(
                    items=[
                        {
                            "transactionId": transaction_id,
                            "transactionType": "SALE",
                            "amount": {"value": "9.00", "currency": "USD"},
                            "transactionDate": "2026-11-05T00:00:00+00:00",
                        }
                    ],
                    exhausted=True,
                    continuation_offset=None,
                )
            return EbayPaginationResult(
                items=[], exhausted=True, continuation_offset=None
            )

    async def entity_for_credential(*args: Any, **kwargs: Any) -> Any:
        return TEST_ENTITY_ID

    async def mapper(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["txn"].get("transactionId") or kwargs["txn"].get("payoutId"))
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    async def no_reconciliation(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(
        ebay_sync.EbayFinancesClient,
        "from_credential",
        classmethod(lambda cls, blob: FakeClient()),
    )
    monkeypatch.setattr(ebay_sync, "map_and_post_transaction", mapper)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", no_reconciliation)

    summary = await ebay_sync._sync_one_workspace(TEST_WORKSPACE_ID)

    assert summary["last_outcome"] == "ok"
    assert transaction_id in seen
    assert payout_id in seen
    async with db.acquire() as conn:
        state = await conn.fetchrow(
            """
            SELECT last_sync_at FROM books.connector_state
            WHERE entity_id = $1 AND connector_name = 'ebay_finances'
            """,
            str(TEST_ENTITY_ID),
        )
    assert state["last_sync_at"].replace(tzinfo=timezone.utc) == window_end


@pytest.mark.asyncio
async def test_review_approval_is_deduped_across_three_mapper_passes(
    db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    transaction_id = "ebay-pg-review-dedupe"
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            # The eBay mapper resolves SALEs against the eBay-specific accounts.
            # Seed them with the same owner pool used by CI's integration setup
            # so the test reaches the review queue instead of the expected
            # production block for a fresh entity with no eBay COA.
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
                        "asset", "debit", "eBay review dedupe integration test",
                    ),
                    (
                        str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "4150",
                        "eBay Resale Revenue", "4150·eBay Resale Revenue",
                        "revenue", "credit", "eBay review dedupe integration test",
                    ),
                ],
            )
            await conn.execute(
                """
                DELETE FROM approval_queue
                WHERE entity_id = $1 AND subject_type = 'ebay_post_transaction'
                  AND payload ->> 'ebay_transaction_id' = $2
                """,
                str(TEST_ENTITY_ID), transaction_id,
            )

    async def no_existing_posts(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(ebay_mapper, "_count_existing_ebay_posts", no_existing_posts)
    txn = {
        "transactionId": transaction_id,
        "transactionType": "SALE",
        "amount": {"value": "9.00", "currency": "USD"},
        "transactionDate": "2026-11-05T00:00:00+00:00",
    }
    outcomes = []
    for _ in range(3):
        async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
            result = await ebay_mapper.map_and_post_transaction(
                session,
                workspace_id=str(TEST_WORKSPACE_ID),
                entity_id=str(TEST_ENTITY_ID),
                txn=txn,
                actor_uc_uid="agent:ebay_sync",
            )
            outcomes.append(result)

    assert all(
        result.outcome == "queued_first_run" for result in outcomes
    ), [result.outcome for result in outcomes]
    assert outcomes[0].approval_id == outcomes[1].approval_id == outcomes[2].approval_id
    async with db.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT count(*) FROM books.approval_queue
            WHERE entity_id = $1 AND subject_type = 'ebay_post_transaction'
              AND status = 'pending'
              AND payload ->> 'ebay_transaction_id' = $2
            """,
            str(TEST_ENTITY_ID), transaction_id,
        )
    assert count == 1


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
    state_json = state["state"]
    if isinstance(state_json, str):
        state_json = json.loads(state_json)
    assert state_json["_ebay_failure_counts"] == {failed_id: 1}
    assert state_json["_ebay_continuation_cursor"]["payouts"]["offset"] == 1


@pytest.mark.asyncio
async def test_rejected_ebay_item_is_durable_and_advances_without_reasking(
    db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    transaction_id = "ebay-pg-human-reject"
    window_start = datetime(2026, 11, 9, tzinfo=timezone.utc)
    window_end = datetime(2026, 11, 10, 0, 0, 1, tzinfo=timezone.utc)
    txn = {
        "transactionId": transaction_id,
        "transactionType": "SALE",
        "amount": {"value": "9.00", "currency": "USD"},
        "transactionDate": "2026-11-05T00:00:00+00:00",
    }

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
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "1220", "eBay receivable", "1220·eBay receivable", "asset", "debit", "eBay decision test"),
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "4150", "eBay revenue", "4150·eBay revenue", "revenue", "credit", "eBay decision test"),
                ],
            )
            await conn.execute(
                """
                DELETE FROM approval_queue
                WHERE entity_id = $1 AND payload ->> 'ebay_transaction_id' = $2
                """,
                str(TEST_ENTITY_ID), transaction_id,
            )
            await conn.execute(
                "DELETE FROM journal_entry WHERE entity_id = $1 AND source_external_id = $2",
                str(TEST_ENTITY_ID), transaction_id,
            )
            await conn.execute(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (gen_random_uuid(), $1, $2, 'ebay_finances', 'default', '{}'::jsonb, NULL)
                ON CONFLICT (entity_id, connector_name, connector_instance_name)
                DO UPDATE SET state = '{}'::jsonb, last_sync_at = NULL
                """,
                str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID),
            )

    async def no_existing_posts(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(ebay_mapper, "_count_existing_ebay_posts", no_existing_posts)

    async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
        queued = await ebay_mapper.map_and_post_transaction(
            session,
            workspace_id=str(TEST_WORKSPACE_ID),
            entity_id=str(TEST_ENTITY_ID),
            txn=txn,
            actor_uc_uid="agent:ebay_sync",
        )
    assert queued.outcome == "queued_first_run"
    assert queued.approval_id is not None

    async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
        await session.execute(
            text(
                """
                UPDATE approval_queue
                SET status = CAST('rejected' AS approval_status),
                    decided_by_uc_uid = 'fictional-reviewer', decided_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(queued.approval_id)},
        )
        await record_ebay_decision(
            session,
            entity_id=TEST_ENTITY_ID,
            transaction_id=transaction_id,
            approval_id=str(queued.approval_id),
            decision="reject",
            decided_by="fictional-reviewer",
        )

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return EbayCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="postgres human reject test",
            client_id="fictional-app",
            client_secret="fictional-secret",
            refresh_token="fictional-refresh",
            environment="sandbox",
            entity_id=str(TEST_ENTITY_ID),
        )

    class FakeClient:
        async def list_payouts(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(
                items=[], exhausted=True, continuation_offset=None,
                window_start=window_start, window_end=window_end,
            )

        async def list_transactions(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(items=[txn], exhausted=True, continuation_offset=None)

    async def entity_for_credential(*args: Any, **kwargs: Any) -> Any:
        return TEST_ENTITY_ID

    async def no_reconciliation(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(
        ebay_sync.EbayFinancesClient,
        "from_credential",
        classmethod(lambda cls, blob: FakeClient()),
    )
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", no_reconciliation)

    summaries = [
        await ebay_sync._sync_one_workspace(TEST_WORKSPACE_ID)
        for _ in range(3)
    ]
    assert all(summary["rejected"] == 1 for summary in summaries)
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            approval_counts = await conn.fetchrow(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status = 'pending') AS pending
                FROM approval_queue
                WHERE entity_id = $1 AND payload ->> 'ebay_transaction_id' = $2
                """,
                str(TEST_ENTITY_ID), transaction_id,
            )
            journal_count = await conn.fetchval(
                """
                SELECT count(*) FROM journal_entry
                WHERE entity_id = $1 AND source = 'ebay_finances'
                  AND source_external_id = $2
                """,
                str(TEST_ENTITY_ID), transaction_id,
            )
            state = await conn.fetchrow(
                """
                SELECT last_sync_at, state FROM connector_state
                WHERE entity_id = $1 AND connector_name = 'ebay_finances'
                """,
                str(TEST_ENTITY_ID),
            )
    assert approval_counts["total"] == 1
    assert approval_counts["pending"] == 0
    assert journal_count == 0
    assert state["last_sync_at"].replace(tzinfo=timezone.utc) == window_end
    state_json = state["state"]
    if isinstance(state_json, str):
        state_json = json.loads(state_json)
    assert state_json["rejected_ebay_items"][transaction_id]


@pytest.mark.asyncio
async def test_approved_first_run_batch_posts_once_and_replays_idempotently(
    db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    prefix = "ebay-pg-human-approve"
    transactions = [
        {
            "transactionId": f"{prefix}-{index:02d}",
            "transactionType": "SALE",
            "amount": {"value": "9.00", "currency": "USD"},
            "transactionDate": "2026-11-05T00:00:00+00:00",
        }
        for index in range(30)
    ]
    window_start = datetime(2026, 11, 9, tzinfo=timezone.utc)
    window_end = datetime(2026, 11, 10, 0, 0, 1, tzinfo=timezone.utc)

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
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "1220", "eBay receivable", "1220·eBay receivable", "asset", "debit", "eBay decision batch test"),
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "4150", "eBay revenue", "4150·eBay revenue", "revenue", "credit", "eBay decision batch test"),
                ],
            )
            ids = [txn["transactionId"] for txn in transactions]
            await conn.execute(
                "DELETE FROM approval_queue WHERE entity_id = $1 AND payload ->> 'ebay_transaction_id' = ANY($2::text[])",
                str(TEST_ENTITY_ID), ids,
            )
            await conn.execute(
                "DELETE FROM journal_entry WHERE entity_id = $1 AND source = 'ebay_finances' AND source_external_id = ANY($2::text[])",
                str(TEST_ENTITY_ID), ids,
            )
            await conn.execute(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (gen_random_uuid(), $1, $2, 'ebay_finances', 'default', '{}'::jsonb, NULL)
                ON CONFLICT (entity_id, connector_name, connector_instance_name)
                DO UPDATE SET state = '{}'::jsonb, last_sync_at = NULL
                """,
                str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID),
            )

    async def no_existing_posts(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(ebay_mapper, "_count_existing_ebay_posts", no_existing_posts)
    queued_ids: list[tuple[str, Any]] = []
    for txn in transactions:
        async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
            result = await ebay_mapper.map_and_post_transaction(
                session,
                workspace_id=str(TEST_WORKSPACE_ID),
                entity_id=str(TEST_ENTITY_ID),
                txn=txn,
                actor_uc_uid="agent:ebay_sync",
            )
            assert result.outcome == "queued_first_run"
            queued_ids.append((txn["transactionId"], result.approval_id))

    async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
        for transaction_id, approval_id in queued_ids:
            await session.execute(
                text(
                    """
                    UPDATE approval_queue
                    SET status = CAST('approved' AS approval_status),
                        decided_by_uc_uid = 'fictional-reviewer', decided_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": str(approval_id)},
            )
            await record_ebay_decision(
                session,
                entity_id=TEST_ENTITY_ID,
                transaction_id=transaction_id,
                approval_id=str(approval_id),
                decision="approve",
                decided_by="fictional-reviewer",
            )

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return EbayCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="postgres human approve test",
            client_id="fictional-app",
            client_secret="fictional-secret",
            refresh_token="fictional-refresh",
            environment="sandbox",
            entity_id=str(TEST_ENTITY_ID),
        )

    class FakeClient:
        async def list_payouts(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(
                items=[], exhausted=True, continuation_offset=None,
                window_start=window_start, window_end=window_end,
            )

        async def list_transactions(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(items=transactions, exhausted=True, continuation_offset=None)

    async def entity_for_credential(*args: Any, **kwargs: Any) -> Any:
        return TEST_ENTITY_ID

    async def no_reconciliation(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(
        ebay_sync.EbayFinancesClient,
        "from_credential",
        classmethod(lambda cls, blob: FakeClient()),
    )
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", no_reconciliation)

    first = await ebay_sync._sync_one_workspace(TEST_WORKSPACE_ID)
    second = await ebay_sync._sync_one_workspace(TEST_WORKSPACE_ID)
    assert first["last_outcome"] == "ok"
    assert second["last_outcome"] == "ok"
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            journal_count = await conn.fetchval(
                """
                SELECT count(*) FROM journal_entry
                WHERE entity_id = $1 AND source = 'ebay_finances'
                  AND source_external_id LIKE $2
                """,
                str(TEST_ENTITY_ID), f"{prefix}-%",
            )
            approval_counts = await conn.fetchrow(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status = 'pending') AS pending
                FROM approval_queue
                WHERE entity_id = $1 AND payload ->> 'ebay_transaction_id' LIKE $2
                """,
                str(TEST_ENTITY_ID), f"{prefix}-%",
            )
    assert journal_count == len(transactions)
    assert approval_counts["total"] == len(transactions)
    assert approval_counts["pending"] == 0


@pytest.mark.parametrize("amount", ["80.00", "1500.00"])
@pytest.mark.asyncio
async def test_approved_m_and_h_items_post_once_and_consume_arm(
    amount: str, db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    transaction_id = f"ebay-pg-attested-{amount.replace('.', '-') }"
    txn = {
        "transactionId": transaction_id,
        "transactionType": "SALE",
        "amount": {"value": amount, "currency": "USD"},
        "transactionDate": "2026-11-05T00:00:00+00:00",
    }
    window_start = datetime(2026, 11, 9, tzinfo=timezone.utc)
    window_end = datetime(2026, 11, 10, 0, 0, 1, tzinfo=timezone.utc)

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
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "1220",
                     "eBay receivable", "1220·eBay receivable", "asset", "debit",
                     "eBay approval attestation test"),
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "4150",
                     "eBay revenue", "4150·eBay revenue", "revenue", "credit",
                     "eBay approval attestation test"),
                ],
            )
            await conn.execute(
                "DELETE FROM approval_queue WHERE entity_id = $1 AND payload ->> 'ebay_transaction_id' = $2",
                str(TEST_ENTITY_ID), transaction_id,
            )
            await conn.execute(
                "DELETE FROM journal_entry WHERE entity_id = $1 AND source_external_id = $2",
                str(TEST_ENTITY_ID), transaction_id,
            )
            await conn.execute(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (gen_random_uuid(), $1, $2, 'ebay_finances', 'default', '{}'::jsonb, NULL)
                ON CONFLICT (entity_id, connector_name, connector_instance_name)
                DO UPDATE SET state = '{}'::jsonb, last_sync_at = NULL
                """,
                str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID),
            )

    async def no_existing_posts(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(ebay_mapper, "_count_existing_ebay_posts", no_existing_posts)
    async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
        queued = await ebay_mapper.map_and_post_transaction(
            session,
            workspace_id=str(TEST_WORKSPACE_ID),
            entity_id=str(TEST_ENTITY_ID),
            txn=txn,
            actor_uc_uid="agent:ebay_sync",
        )
    assert queued.outcome == "queued_first_run"

    async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
        row = (
            await session.execute(
                text("SELECT payload FROM approval_queue WHERE id = :id"),
                {"id": str(queued.approval_id)},
            )
        ).first()
        payload = row.payload if isinstance(row.payload, dict) else json.loads(row.payload)
        await session.execute(
            text(
                """
                UPDATE approval_queue
                SET status = CAST('approved' AS approval_status),
                    decided_by_uc_uid = 'fictional-reviewer', decided_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(queued.approval_id)},
        )
        await record_ebay_decision(
            session,
            entity_id=TEST_ENTITY_ID,
            transaction_id=transaction_id,
            approval_id=str(queued.approval_id),
            decision="approve",
            decided_by="fictional-reviewer",
            approval_payload=payload,
        )

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return EbayCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="postgres attestation test",
            client_id="fictional-app",
            client_secret="fictional-secret",
            refresh_token="fictional-refresh",
            environment="sandbox",
            entity_id=str(TEST_ENTITY_ID),
        )

    class FakeClient:
        async def list_payouts(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(
                items=[], exhausted=True, continuation_offset=None,
                window_start=window_start, window_end=window_end,
            )

        async def list_transactions(self, **kwargs: Any) -> EbayPaginationResult:
            if kwargs.get("payout_id"):
                return EbayPaginationResult(items=[], exhausted=True, continuation_offset=None)
            return EbayPaginationResult(
                items=[txn], exhausted=True, continuation_offset=None,
            )

    async def entity_for_credential(*args: Any, **kwargs: Any) -> Any:
        return TEST_ENTITY_ID

    async def no_reconciliation(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(
        ebay_sync.EbayFinancesClient,
        "from_credential",
        classmethod(lambda cls, blob: FakeClient()),
    )
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", no_reconciliation)

    summary = await ebay_sync._sync_one_workspace(TEST_WORKSPACE_ID)
    assert summary["last_outcome"] == "ok"

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            posted = await conn.fetchrow(
                """
                SELECT amount FROM journal_entry
                WHERE entity_id = $1 AND source = 'ebay_finances'
                  AND source_external_id = $2
                """,
                str(TEST_ENTITY_ID), transaction_id,
            )
            state = await conn.fetchval(
                "SELECT state FROM connector_state WHERE entity_id = $1 AND connector_name = 'ebay_finances'",
                str(TEST_ENTITY_ID),
            )
    assert posted["amount"] == float(amount)
    state = json.loads(state) if isinstance(state, str) else state
    assert transaction_id not in state.get("approved_ebay_items", {})


@pytest.mark.asyncio
async def test_stale_approval_snapshot_is_cleared_and_requeued(
    db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    transaction_id = "ebay-pg-stale-approval"
    current_txn = {
        "transactionId": transaction_id,
        "transactionType": "SALE",
        "amount": {"value": "60.00", "currency": "USD"},
        "transactionDate": "2026-11-05T00:00:00+00:00",
    }
    old_payload = {
        "action": "ebay_post_transaction",
        "ebay_transaction_id": transaction_id,
        "amount_cents": 900,
        "currency": "USD",
        "transactionType": "SALE",
        "transactionDate": "2026-11-05T00:00:00+00:00",
    }
    old_arm = {
        "approval_id": "00000000-0000-0000-0000-000000000001",
        "amount_cents": 900,
        "currency": "USD",
        "transactionType": "SALE",
        "transactionDate": "2026-11-05T00:00:00+00:00",
    }
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
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "1220",
                     "eBay receivable", "1220·eBay receivable", "asset", "debit",
                     "eBay stale approval test"),
                    (str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), "4150",
                     "eBay revenue", "4150·eBay revenue", "revenue", "credit",
                     "eBay stale approval test"),
                ],
            )
            await conn.execute(
                "DELETE FROM approval_queue WHERE entity_id = $1 AND payload ->> 'ebay_transaction_id' = $2",
                str(TEST_ENTITY_ID), transaction_id,
            )
            await conn.execute(
                "DELETE FROM journal_entry WHERE entity_id = $1 AND source_external_id = $2",
                str(TEST_ENTITY_ID), transaction_id,
            )
            await conn.execute(
                """
                INSERT INTO approval_queue (
                    id, workspace_id, entity_id, subject_type, subject_id,
                    amount, currency_code, usd_equivalent_at_event_time,
                    status, reasoning, requested_by_identity, payload,
                    decided_by_uc_uid, decided_at
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001', $1, $2,
                    'ebay_post_transaction', gen_random_uuid(), 9.00, 'USD', 9.00,
                    'approved', 'old amount', 'agent:ebay_sync', $3::jsonb,
                    'fictional-reviewer', now()
                )
                ON CONFLICT (id) DO UPDATE SET status = 'approved', payload = EXCLUDED.payload
                """,
                str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), json.dumps(old_payload),
            )
            await conn.execute(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (
                    gen_random_uuid(), $1, $2, 'ebay_finances', 'default', $3::jsonb, NULL
                )
                ON CONFLICT (entity_id, connector_name, connector_instance_name)
                DO UPDATE SET state = EXCLUDED.state, last_sync_at = NULL
                """,
                str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID),
                json.dumps({"approved_ebay_items": {transaction_id: old_arm}}),
            )

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return EbayCredentialBlob(
            workspace_id=TEST_WORKSPACE_ID,
            credential_id=TEST_WORKSPACE_ID,
            label="postgres stale approval test",
            client_id="fictional-app",
            client_secret="fictional-secret",
            refresh_token="fictional-refresh",
            environment="sandbox",
            entity_id=str(TEST_ENTITY_ID),
        )

    class FakeClient:
        async def list_payouts(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(
                items=[], exhausted=True, continuation_offset=None,
                window_start=datetime(2026, 11, 9, tzinfo=timezone.utc),
                window_end=datetime(2026, 11, 10, 0, 0, 1, tzinfo=timezone.utc),
            )

        async def list_transactions(self, **kwargs: Any) -> EbayPaginationResult:
            return EbayPaginationResult(
                items=[current_txn] if not kwargs.get("payout_id") else [],
                exhausted=True,
                continuation_offset=None,
            )

    async def entity_for_credential(*args: Any, **kwargs: Any) -> Any:
        return TEST_ENTITY_ID

    async def no_reconciliation(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(
        ebay_sync.EbayFinancesClient,
        "from_credential",
        classmethod(lambda cls, blob: FakeClient()),
    )
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", no_reconciliation)

    summary = await ebay_sync._sync_one_workspace(TEST_WORKSPACE_ID)
    assert summary["last_outcome"] == "awaiting_review"

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL search_path = books, public")
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(TEST_WORKSPACE_ID),
            )
            pending = await conn.fetchrow(
                """
                SELECT status, payload->>'amount_cents' AS amount_cents
                FROM approval_queue
                WHERE entity_id = $1 AND payload ->> 'ebay_transaction_id' = $2
                  AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                str(TEST_ENTITY_ID), transaction_id,
            )
            state = await conn.fetchval(
                "SELECT state FROM connector_state WHERE entity_id = $1 AND connector_name = 'ebay_finances'",
                str(TEST_ENTITY_ID),
            )
    assert pending["status"] == "pending"
    assert pending["amount_cents"] == "6000"
    state = json.loads(state) if isinstance(state, str) else state
    assert transaction_id not in state.get("approved_ebay_items", {})
