"""Real PostgreSQL proof for the manual "resolved elsewhere" Stripe unblock."""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api.stripe_blocked_item import (
    ResolveBlockedItemRequest,
    resolve_stripe_blocked_item,
)
from app.auth import AuthContext
from app.config import get_settings
from app.connectors.stripe import mapper as stripe_mapper
from app.connectors.stripe.client import StripeCredentialBlob, StripePaginationResult
from app.data_rights.export import build_export
from app.db import SessionLocal
from app.tenancy import WorkspaceContext
from app.workspace_session import workspace_session
from scripts import stripe_sync
from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_B_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
)

pytestmark = pytest.mark.integration

CREATED = 1_794_268_800  # 2026-11-10, inside the default backfill window
HUMAN = "fixture-resolver-human"


def _txn(txn_id: str, created: int) -> dict[str, Any]:
    return {
        "id": txn_id,
        "object": "balance_transaction",
        "type": "charge",
        "amount": 1000,
        "currency": "usd",
        "fee": 0,
        "net": 1000,
        "created": created,
        "available_on": created,
        "reporting_category": "charge",
    }


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _scoped(conn: Any, workspace_id: UUID) -> None:
    await conn.execute("SET LOCAL search_path = books, public")
    await conn.execute(
        "SELECT set_config('app.current_workspace', $1, true)", str(workspace_id)
    )


async def _seed(db: Any, workspace_id: UUID, entity_id: UUID) -> None:
    async with db.acquire() as conn:
        async with conn.transaction():
            await _scoped(conn, workspace_id)
            await conn.execute(
                """
                INSERT INTO chart_of_account (
                    workspace_id, entity_id, account_number, account_name,
                    canonical_form, account_type, normal_balance,
                    is_active, is_system, notes
                ) VALUES
                    ($1, $2, '1210', 'Stripe Receivable',
                     '1210·Stripe Receivable', 'asset', 'debit', true, false,
                     'Stripe resolve integration test'),
                    ($1, $2, '4200', 'AI Infrastructure Revenue (UC-Cloud)',
                     '4200·AI Infrastructure Revenue (UC-Cloud)', 'revenue',
                     'credit', true, false, 'Stripe resolve integration test')
                ON CONFLICT (entity_id, account_number)
                DO UPDATE SET is_active = true
                """,
                str(workspace_id),
                str(entity_id),
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
                str(workspace_id),
                str(entity_id),
            )


async def _fetch(app_db: Any, workspace_id: UUID, sql: str, *args: Any) -> Any:
    async with app_db.acquire() as conn:
        async with conn.transaction():
            await _scoped(conn, workspace_id)
            return await conn.fetch(sql, *args)


def _caller(role: str) -> tuple[WorkspaceContext, AuthContext]:
    ctx = WorkspaceContext(
        TEST_WORKSPACE_ID, "integration", role, HUMAN, None, None, False
    )
    auth = AuthContext(
        HUMAN,
        None,
        (),
        {
            "azp": get_settings().keycloak_client_id,
            "auth_time": 1,
            "sid": "fixture-session",
            "preferred_username": HUMAN,
        },
        may_autoprovision=True,
    )
    return ctx, auth


async def _call(role: str, **body: Any) -> dict[str, Any]:
    ctx, auth = _caller(role)
    return await resolve_stripe_blocked_item(
        workspace_id=TEST_WORKSPACE_ID,
        body=ResolveBlockedItemRequest(**body),
        workspace=ctx,
        auth=auth,
    )


@pytest.mark.asyncio
async def test_manual_resolution_unblocks_without_posting(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    run = uuid4().hex[:10]
    newest = f"stripe-pg-resolve-{run}-newest"
    blocked = f"stripe-pg-resolve-{run}-blocked"
    oldest = f"stripe-pg-resolve-{run}-oldest"
    other_ws = f"stripe-pg-resolve-{run}-other-ws"
    feeds = {
        TEST_WORKSPACE_ID: [
            _txn(newest, CREATED + 2),
            _txn(blocked, CREATED + 1),
            _txn(oldest, CREATED),
        ],
        TEST_WORKSPACE_B_ID: [_txn(other_ws, CREATED)],
    }
    entities = {
        TEST_WORKSPACE_ID: TEST_ENTITY_ID,
        TEST_WORKSPACE_B_ID: TEST_WORKSPACE_B_ENTITY_ID,
    }
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    await _seed(db, TEST_WORKSPACE_B_ID, TEST_WORKSPACE_B_ENTITY_ID)

    current_ws: list[UUID] = []
    seen: list[str] = []

    async def load_credential(workspace_id: UUID, **kwargs: Any) -> StripeCredentialBlob:
        current_ws[:] = [workspace_id]
        return StripeCredentialBlob(
            workspace_id=workspace_id,
            credential_id=workspace_id,
            label="postgres resolve test",
            api_key="rk_test_000000000000000000000000",
            account_id="acct_test_fictional",
            environment="test",
            entity_id=str(entities[workspace_id]),
        )

    async def entity_for_credential(workspace_id: UUID, *args: Any) -> UUID:
        return entities[workspace_id]

    async def paginator(*args: Any, starting_after: str | None = None, **kwargs: Any):
        items = feeds[current_ws[0]]
        if starting_after:
            ids = [item["id"] for item in items]
            items = items[ids.index(starting_after) + 1 :]
        return StripePaginationResult(
            items=items, exhausted=True, continuation_cursor=None, window_lt=CREATED + 3
        )

    async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["id"])
        if txn["id"] == blocked:
            raise RuntimeError("fictional missing revenue account mapping")
        return await stripe_mapper.map_and_post_balance_transaction(session, txn=txn, **kwargs)

    async def existing_posts(*args: Any, **kwargs: Any) -> int:
        return 31

    async def reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(stripe_sync, "load_stripe_credential", load_credential)
    monkeypatch.setattr(stripe_sync, "_entity_for_credential", entity_for_credential)
    monkeypatch.setattr(stripe_sync, "paginate_balance_transactions", paginator)
    monkeypatch.setattr(stripe_sync, "map_and_post_balance_transaction", mapper)
    monkeypatch.setattr(stripe_mapper, "_count_existing_stripe_posts", existing_posts)
    monkeypatch.setattr(stripe_sync, "reconcile_one_entity", reconcile)

    # Workspace B posts one real journal entry: the foreign id used below.
    assert (await stripe_sync._sync_one_workspace(TEST_WORKSPACE_B_ID))["last_outcome"] == "ok"
    foreign_je = (
        await _fetch(
            app_db,
            TEST_WORKSPACE_B_ID,
            "SELECT id FROM journal_entry WHERE source_external_id = $1",
            other_ws,
        )
    )[0]["id"]

    # Three failing passes block the item in workspace A.
    outcomes = [
        (await stripe_sync._sync_one_workspace(TEST_WORKSPACE_ID))["last_outcome"]
        for _ in range(3)
    ]
    assert outcomes == ["processing_error", "processing_error", "blocked_item"]
    local_je = (
        await _fetch(
            app_db,
            TEST_WORKSPACE_ID,
            "SELECT id FROM journal_entry WHERE source_external_id = $1",
            newest,
        )
    )[0]["id"]

    async def stripe_post_count() -> int:
        rows = await _fetch(
            app_db,
            TEST_WORKSPACE_ID,
            "SELECT count(*) AS c FROM journal_entry WHERE entity_id = $1 AND source = 'stripe_api'",
            str(TEST_ENTITY_ID),
        )
        return rows[0]["c"]

    posts_before = await stripe_post_count()

    with pytest.raises(HTTPException) as denied:
        await _call("accountant", bt_id=blocked, journal_entry_id=local_je)
    assert denied.value.status_code == 403

    with pytest.raises(HTTPException) as wrong:
        await _call("admin", bt_id=newest, journal_entry_id=local_je)
    assert wrong.value.status_code == 409

    with pytest.raises(HTTPException) as foreign:
        await _call("admin", bt_id=blocked, journal_entry_id=foreign_je)
    assert foreign.value.status_code == 422

    response = await _call("owner", bt_id=blocked, journal_entry_id=local_je)
    assert response["posted"] is False
    assert response["resolution"]["journal_entry_id"] == str(local_je)

    state = _json(
        (
            await _fetch(
                app_db,
                TEST_WORKSPACE_ID,
                "SELECT state FROM connector_state WHERE entity_id = $1 AND connector_name = 'stripe_api'",
                str(TEST_ENTITY_ID),
            )
        )[0]["state"]
    )
    assert "blocked_item" not in state
    assert blocked not in state.get("_stripe_failure_counts", {})
    assert state["_stripe_continuation_cursor"] == blocked
    assert state["resolved_blocked_items"][blocked]["resolved_by_uc_uid"] == HUMAN

    audits = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        """
        SELECT reasoning, outcome, request_payload FROM decision_audit
        WHERE tool_name = 'stripe_blocked_item_resolve' AND id = $1
        """,
        response["audit_id"],
    )
    assert len(audits) == 1
    assert audits[0]["outcome"] == "resolved_elsewhere"
    assert HUMAN in audits[0]["reasoning"] and str(local_je) in audits[0]["reasoning"]

    approval = (
        await _fetch(
            app_db,
            TEST_WORKSPACE_ID,
            """
            SELECT status::text AS status, decided_by_uc_uid, decided_at, payload
            FROM approval_queue WHERE id = $1
            """,
            response["approval_id"],
        )
    )[0]
    assert approval["status"] == "rejected"
    assert approval["decided_by_uc_uid"] == HUMAN
    assert approval["decided_at"] is not None
    assert _json(approval["payload"])["blocked_resolution"]["journal_entry_id"] == str(local_je)

    assert await stripe_post_count() == posts_before  # nothing posted

    # Next pass continues after the resolved item; a later overlap re-read
    # returns it again and must skip it, never post it.
    seen.clear()
    assert (await stripe_sync._sync_one_workspace(TEST_WORKSPACE_ID))["last_outcome"] == "ok"
    assert seen == [oldest]
    assert (await stripe_sync._sync_one_workspace(TEST_WORKSPACE_ID))["last_outcome"] == "ok"
    assert blocked not in seen
    blocked_posts = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        "SELECT count(*) AS c FROM journal_entry WHERE source_external_id = $1",
        blocked,
    )
    assert blocked_posts[0]["c"] == 0
    assert await stripe_post_count() == posts_before + 1  # only `oldest`

    with pytest.raises(HTTPException) as again:
        await _call("admin", bt_id=blocked, not_books_relevant_reason="Second attempt must fail")
    assert again.value.status_code == 409

    # The customer export carries the resolution with its note.
    async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
        archive, _ = await build_export(
            session, TEST_WORKSPACE_ID, requested_by_uc_uid=HUMAN
        )
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        exported_approvals = json.loads(zf.read("json/approval_queue.json"))
        exported_states = json.loads(zf.read("json/connector_state.json"))
    exported = [
        row for row in exported_approvals if str(row["id"]) == response["approval_id"]
    ]
    assert len(exported) == 1
    assert _json(exported[0]["payload"])["blocked_resolution"]["journal_entry_id"] == str(local_je)
    assert any(
        blocked in (_json(row["state"]) or {}).get("resolved_blocked_items", {})
        for row in exported_states
    )
