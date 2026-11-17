"""Real PostgreSQL proof for the manual "resolved elsewhere" Stripe unblock."""
from __future__ import annotations

import asyncio
import io
import json
import zipfile
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api.approvals import DecideRequest, SkipRequest, decide_approval, skip_approval
from app.api.stripe_blocked_item import (
    ReopenResolvedItemRequest,
    ResolveBlockedItemRequest,
    reopen_stripe_resolved_item,
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


def _caller(role: str, workspace_id: UUID) -> tuple[WorkspaceContext, AuthContext]:
    ctx = WorkspaceContext(workspace_id, "integration", role, HUMAN, None, None, False)
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


async def _call(
    role: str, workspace_id: UUID = TEST_WORKSPACE_ID, **body: Any
) -> dict[str, Any]:
    ctx, auth = _caller(role, workspace_id)
    return await resolve_stripe_blocked_item(
        workspace_id=workspace_id,
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
    other_ws_blocked = f"stripe-pg-resolve-{run}-other-ws"
    feed = [
        _txn(newest, CREATED + 2),
        _txn(blocked, CREATED + 1),
        _txn(oldest, CREATED),
    ]
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    seen: list[str] = []

    async def load_credential(workspace_id: UUID, **kwargs: Any) -> StripeCredentialBlob:
        return StripeCredentialBlob(
            workspace_id=workspace_id,
            credential_id=workspace_id,
            label="postgres resolve test",
            api_key="rk_test_000000000000000000000000",
            account_id="acct_test_fictional",
            environment="test",
            entity_id=str(TEST_ENTITY_ID),
        )

    async def entity_for_credential(workspace_id: UUID, *args: Any) -> UUID:
        return TEST_ENTITY_ID

    async def paginator(*args: Any, starting_after: str | None = None, **kwargs: Any):
        items = feed
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

    # Workspace B has its own blocked item. Workspace A's journal entry is not
    # in B's books, so B cannot cite it. B's books stay empty: the RLS suite
    # relies on that.
    await _seed(db, TEST_WORKSPACE_B_ID, TEST_WORKSPACE_B_ENTITY_ID)
    b_state = {"blocked_item": {"id": other_ws_blocked, "amount": 1000, "currency": "usd"}}
    async with db.acquire() as conn:
        async with conn.transaction():
            await _scoped(conn, TEST_WORKSPACE_B_ID)
            await conn.execute(
                """
                UPDATE connector_state SET state = $2::jsonb
                WHERE entity_id = $1 AND connector_name = 'stripe_api'
                """,
                str(TEST_WORKSPACE_B_ENTITY_ID),
                json.dumps(b_state),
            )
    with pytest.raises(HTTPException) as foreign:
        await _call(
            "admin", TEST_WORKSPACE_B_ID, bt_id=other_ws_blocked, journal_entry_id=local_je
        )
    assert foreign.value.status_code == 422
    b_after = await _fetch(
        app_db,
        TEST_WORKSPACE_B_ID,
        "SELECT state FROM connector_state WHERE entity_id = $1 AND connector_name = 'stripe_api'",
        str(TEST_WORKSPACE_B_ENTITY_ID),
    )
    assert _json(b_after[0]["state"]) == b_state

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
        SELECT reasoning_trace, outcome, request_payload FROM decision_audit
        WHERE tool_name = 'stripe_blocked_item_resolve' AND id = $1
        """,
        response["audit_id"],
    )
    assert len(audits) == 1
    assert audits[0]["outcome"] == "resolved_elsewhere"
    assert HUMAN in audits[0]["reasoning_trace"]
    assert str(local_je) in audits[0]["reasoning_trace"]

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


# ---------------------------------------------------------------------------
# Races, run on real PostgreSQL row locks (review 8a42b2b7).
# ---------------------------------------------------------------------------


class _Worker:
    """Drive the real sync worker against a fake Stripe feed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, feed: list[dict[str, Any]]):
        self.feed = feed
        self.failing: set[str] = set()
        self.seen: list[str] = []
        self.mapper_entered = asyncio.Event()
        self.mapper_release: asyncio.Event | None = None
        self.app_db: Any = None

        async def load_credential(workspace_id: UUID, **kwargs: Any) -> StripeCredentialBlob:
            return StripeCredentialBlob(
                workspace_id=workspace_id,
                credential_id=workspace_id,
                label="postgres race test",
                api_key="rk_test_000000000000000000000000",
                account_id="acct_test_fictional",
                environment="test",
                entity_id=str(TEST_ENTITY_ID),
            )

        async def entity_for_credential(workspace_id: UUID, *args: Any) -> UUID:
            return TEST_ENTITY_ID

        async def paginator(*args: Any, starting_after: str | None = None, **kwargs: Any):
            items = self.feed
            if starting_after:
                ids = [item["id"] for item in items]
                items = items[ids.index(starting_after) + 1 :]
            return StripePaginationResult(
                items=items, exhausted=True, continuation_cursor=None, window_lt=CREATED + 3
            )

        async def mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
            self.seen.append(txn["id"])
            self.mapper_entered.set()
            if self.mapper_release is not None:
                await self.mapper_release.wait()
            if txn["id"] in self.failing:
                raise RuntimeError("fictional missing revenue account mapping")
            return await stripe_mapper.map_and_post_balance_transaction(
                session, txn=txn, **kwargs
            )

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

    async def run(self) -> dict[str, Any]:
        return await stripe_sync._sync_one_workspace(TEST_WORKSPACE_ID)


def _feed(run: str) -> tuple[list[dict[str, Any]], str, str, str]:
    newest = f"stripe-pg-race-{run}-newest"
    blocked = f"stripe-pg-race-{run}-blocked"
    oldest = f"stripe-pg-race-{run}-oldest"
    return (
        [_txn(newest, CREATED + 2), _txn(blocked, CREATED + 1), _txn(oldest, CREATED)],
        newest,
        blocked,
        oldest,
    )


async def _block(worker: _Worker, blocked: str) -> None:
    worker.failing = {blocked}
    outcomes = [(await worker.run())["last_outcome"] for _ in range(3)]
    assert outcomes == ["processing_error", "processing_error", "blocked_item"]


async def _state(app_db: Any) -> dict[str, Any]:
    rows = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        "SELECT state FROM connector_state WHERE entity_id = $1 AND connector_name = 'stripe_api'",
        str(TEST_ENTITY_ID),
    )
    return _json(rows[0]["state"])


async def _posts_for(app_db: Any, bt_id: str) -> int:
    rows = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        "SELECT count(*) AS c FROM journal_entry WHERE source = 'stripe_api' AND source_external_id = $1",
        bt_id,
    )
    return rows[0]["c"]


async def _audits(app_db: Any, tool: str, bt_id: str) -> int:
    rows = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        "SELECT count(*) AS c FROM decision_audit WHERE tool_name = $1 AND request_payload ->> 'bt_id' = $2",
        tool,
        bt_id,
    )
    return rows[0]["c"]


REASON = "Fictional charge settled outside Stripe, not books-relevant"


@pytest.mark.asyncio
async def test_resolve_cannot_land_while_the_worker_posts_the_item(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer interleaving: pause at mapper entry, resolve, resume."""
    del seeded_workspace
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)

    # Config fixed: the next pass retries the blocked item. Pause it at the
    # mapper entry, i.e. while the worker holds the shared lock.
    worker.failing = set()
    worker.mapper_entered.clear()
    worker.mapper_release = asyncio.Event()
    sync = asyncio.create_task(worker.run())
    await asyncio.wait_for(worker.mapper_entered.wait(), timeout=10)
    resolve = asyncio.create_task(_call("admin", bt_id=blocked, not_books_relevant_reason=REASON))
    await asyncio.sleep(0.5)
    assert not resolve.done(), "resolution committed while the worker held the item"
    worker.mapper_release.set()
    await asyncio.wait_for(sync, timeout=10)
    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(resolve, timeout=10)
    assert refused.value.status_code == 409

    # Never both: one post, no resolution.
    assert await _posts_for(app_db, blocked) == 1
    assert await _audits(app_db, "stripe_blocked_item_resolve", blocked) == 0
    state = await _state(app_db)
    assert "blocked_item" not in state
    assert blocked not in state.get("resolved_blocked_items", {})


@pytest.mark.asyncio
async def test_resolution_committed_before_the_worker_locks_is_never_posted(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker re-reads the resolved ids AFTER taking the lock."""
    del seeded_workspace
    feed, _, blocked, oldest = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)

    real_lock = stripe_sync.lock_block_state
    at_lock = asyncio.Event()
    go = asyncio.Event()

    async def paused_lock(session: Any, entity_id: UUID, *args: Any) -> Any:
        at_lock.set()
        await go.wait()
        return await real_lock(session, entity_id, *args)

    monkeypatch.setattr(stripe_sync, "lock_block_state", paused_lock)
    worker.failing = set()
    worker.seen.clear()
    sync = asyncio.create_task(worker.run())  # loaded state still shows the block
    await asyncio.wait_for(at_lock.wait(), timeout=10)
    response = await _call("admin", bt_id=blocked, not_books_relevant_reason=REASON)
    assert response["posted"] is False
    go.set()
    await asyncio.wait_for(sync, timeout=10)

    assert blocked not in worker.seen
    assert await _posts_for(app_db, blocked) == 0
    state = await _state(app_db)
    assert "blocked_item" not in state
    assert blocked in state["resolved_blocked_items"]
    assert state["_stripe_continuation_cursor"] == blocked
    worker.seen.clear()
    await worker.run()
    assert worker.seen == [oldest]
    assert await _posts_for(app_db, blocked) == 0


@pytest.mark.asyncio
async def test_outcome_write_never_erases_a_resolution(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolution between the worker's read and its outcome write survives."""
    del seeded_workspace
    feed, _, blocked, oldest = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    blocked_cursor = (await _state(app_db)).get("_stripe_continuation_cursor")
    assert blocked_cursor != blocked

    real_record = stripe_sync._record_sync_outcome
    resolved_mid_write: list[dict[str, Any]] = []

    async def record_after_resolution(*args: Any, **kwargs: Any) -> int:
        if not resolved_mid_write:
            resolved_mid_write.append(
                await _call("owner", bt_id=blocked, not_books_relevant_reason=REASON)
            )
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(stripe_sync, "_record_sync_outcome", record_after_resolution)
    # Still failing: this pass wants to write the block back, with the cursor
    # from before the blocked item.
    summary = await worker.run()
    assert summary["blocked_item"]["id"] == blocked
    assert len(resolved_mid_write) == 1

    state = await _state(app_db)
    assert "blocked_item" not in state, "stale outcome write re-blocked a resolved item"
    assert blocked in state["resolved_blocked_items"]
    assert state["_stripe_continuation_cursor"] == blocked, "cursor rewound"
    assert blocked not in state.get("_stripe_failure_counts", {})
    assert state["last_outcome"] == "superseded_by_resolution"
    assert await _posts_for(app_db, blocked) == 0

    monkeypatch.setattr(stripe_sync, "_record_sync_outcome", real_record)
    worker.seen.clear()
    assert (await worker.run())["last_outcome"] == "ok"
    assert worker.seen == [oldest]


async def _reopen_call(role: str, **body: Any) -> dict[str, Any]:
    ctx, auth = _caller(role, TEST_WORKSPACE_ID)
    return await reopen_stripe_resolved_item(
        workspace_id=TEST_WORKSPACE_ID,
        body=ReopenResolvedItemRequest(**body),
        workspace=ctx,
        auth=auth,
    )


@pytest.mark.asyncio
async def test_reopen_rearms_a_resolved_item_for_normal_processing(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    feed, _, blocked, oldest = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    window = (await _state(app_db))["window_gte"], (await _state(app_db))["window_lt"]
    resolved = await _call("admin", bt_id=blocked, not_books_relevant_reason=REASON)
    worker.failing = set()
    await worker.run()  # chain moves on past the resolved item
    assert await _posts_for(app_db, blocked) == 0

    reopen_reason = "Fictional: turned out to be a real sale, book it"
    with pytest.raises(HTTPException) as denied:
        await _reopen_call("accountant", bt_id=blocked, reason=reopen_reason)
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException) as short:
        await _reopen_call("owner", bt_id=blocked, reason="short")
    assert short.value.status_code == 422
    with pytest.raises(HTTPException) as unknown:
        await _reopen_call("owner", bt_id=oldest, reason=reopen_reason)
    assert unknown.value.status_code == 409

    response = await _reopen_call("owner", bt_id=blocked, reason=reopen_reason)
    assert response["posted"] is False
    assert await _posts_for(app_db, blocked) == 0  # reopen itself posts nothing

    state = await _state(app_db)
    assert blocked not in state["resolved_blocked_items"]
    assert state["blocked_item"]["id"] == blocked
    assert (state["window_gte"], state["window_lt"]) == window
    assert "_stripe_continuation_cursor" not in state
    assert state["reopened_blocked_items"][blocked][0]["reason"] == reopen_reason

    audits = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        "SELECT reasoning_trace, outcome FROM decision_audit WHERE id = $1",
        response["audit_id"],
    )
    assert len(audits) == 1
    assert audits[0]["outcome"] == "resolution_reopened"
    assert HUMAN in audits[0]["reasoning_trace"]
    assert reopen_reason in audits[0]["reasoning_trace"]
    approval = (
        await _fetch(
            app_db,
            TEST_WORKSPACE_ID,
            "SELECT status::text AS status, payload FROM approval_queue WHERE id = $1",
            resolved["approval_id"],
        )
    )[0]
    assert approval["status"] == "rejected"  # decided history stays
    assert _json(approval["payload"])["blocked_reopen"]["reason"] == reopen_reason

    # Normal processing: the next pass retries it through the real mapper.
    worker.seen.clear()
    assert (await worker.run())["last_outcome"] == "ok"
    assert worker.seen == [blocked]
    assert await _posts_for(app_db, blocked) == 1
    assert "blocked_item" not in await _state(app_db)
    # Later passes re-read the window idempotently: still one post.
    for _ in range(2):
        await worker.run()
    assert await _posts_for(app_db, blocked) == 1


@pytest.mark.asyncio
async def test_outcome_write_never_overwrites_a_reopen(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopen landing before a normal pass's outcome write keeps its window."""
    del seeded_workspace
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    window = (await _state(app_db))["window_gte"], (await _state(app_db))["window_lt"]
    await _call("admin", bt_id=blocked, not_books_relevant_reason=REASON)
    worker.failing = set()

    real_record = stripe_sync._record_sync_outcome
    reopened: list[dict[str, Any]] = []

    async def record_after_reopen(*args: Any, **kwargs: Any) -> int:
        if not reopened:
            reopened.append(
                await _reopen_call(
                    "owner", bt_id=blocked, reason="Fictional: reopen during a sync pass"
                )
            )
        return await real_record(*args, **kwargs)

    monkeypatch.setattr(stripe_sync, "_record_sync_outcome", record_after_reopen)
    await worker.run()  # this pass loaded no block and wants to clear the window
    assert len(reopened) == 1

    state = await _state(app_db)
    assert state["blocked_item"]["id"] == blocked
    assert (state["window_gte"], state["window_lt"]) == window
    assert "_stripe_continuation_cursor" not in state
    assert blocked not in state["resolved_blocked_items"]

    monkeypatch.setattr(stripe_sync, "_record_sync_outcome", real_record)
    worker.seen.clear()
    await worker.run()
    assert worker.seen == [blocked]
    assert await _posts_for(app_db, blocked) == 1


# ---------------------------------------------------------------------------
# Review 8a42b2b7 round 2, on the tree stacked on #84.
# ---------------------------------------------------------------------------


async def _approval_rows(app_db: Any, bt_id: str) -> list[Any]:
    return await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        """
        SELECT id::text AS id, status::text AS status, payload
        FROM approval_queue
        WHERE subject_type = 'stripe_post_balance_transaction'
          AND payload ->> 'stripe_balance_txn_id' = $1
        ORDER BY created_at
        """,
        bt_id,
    )


async def _decide_in_process(
    role: str, approval_id: str, decision: str, note: str | None = None
) -> dict[str, Any]:
    ctx, auth = _caller(role, TEST_WORKSPACE_ID)
    return await decide_approval(
        workspace_id=TEST_WORKSPACE_ID,
        approval_id=UUID(approval_id),
        body=DecideRequest(decision=decision, note=note),
        workspace=ctx,
        auth=auth,
    )


@pytest.mark.asyncio
async def test_reopen_survives_worker_that_loaded_the_same_block_id(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve then reopen X while a pass that loaded block X is running.

    The blocked id the pass carried equals the blocked id it finds under the
    lock, so only the block revision tells the pass it is stale.
    """
    del seeded_workspace
    feed, _, blocked, oldest = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    blocked_state = await _state(app_db)
    window = blocked_state["window_gte"], blocked_state["window_lt"]
    revision_at_block = blocked_state["block_revision"]

    real_lock = stripe_sync.lock_block_state
    at_lock = asyncio.Event()
    go = asyncio.Event()

    async def paused_lock(session: Any, entity_id: UUID, *args: Any) -> Any:
        at_lock.set()
        await go.wait()
        return await real_lock(session, entity_id, *args)

    monkeypatch.setattr(stripe_sync, "lock_block_state", paused_lock)
    worker.failing = set()  # config fixed: this pass will post X and clear
    worker.seen.clear()
    sync = asyncio.create_task(worker.run())  # loads block X, revision r
    await asyncio.wait_for(at_lock.wait(), timeout=10)
    await _call("admin", bt_id=blocked, not_books_relevant_reason=REASON)
    reopen = await _reopen_call(
        "owner", bt_id=blocked, reason="Fictional: reopen while a stale pass runs"
    )
    reopened_state = await _state(app_db)
    assert reopened_state["blocked_item"]["id"] == blocked  # the SAME id
    assert reopened_state["block_revision"] == revision_at_block + 2
    go.set()
    await asyncio.wait_for(sync, timeout=10)
    monkeypatch.setattr(stripe_sync, "lock_block_state", real_lock)

    # The stale pass re-read the block revision under the lock, saw the
    # resolve and reopen, and mapped and wrote nothing: the reopen's block,
    # window and fresh failure count are all still there.
    assert worker.seen == []
    state = await _state(app_db)
    assert state["blocked_item"]["id"] == blocked, "stale pass erased the reopen"
    assert state["blocked_item"]["reopened"]["audit_id"] == reopen["audit_id"]
    assert (state["window_gte"], state["window_lt"]) == window
    assert "_stripe_continuation_cursor" not in state
    assert blocked not in state.get("_stripe_failure_counts", {})
    assert state["block_revision"] == revision_at_block + 2
    assert await _posts_for(app_db, blocked) == 0

    # A fresh pass carries the new revision: it posts the reopened item,
    # clears the block itself and moves on. One post.
    worker.seen.clear()
    assert (await worker.run())["last_outcome"] == "ok"
    assert worker.seen == [blocked]
    state = await _state(app_db)
    assert "blocked_item" not in state
    assert state["block_revision"] == revision_at_block + 3
    worker.seen.clear()
    await worker.run()
    assert oldest in worker.seen
    assert await _posts_for(app_db, blocked) == 1


@pytest.mark.asyncio
async def test_reopen_supersedes_the_rejection_but_ordinary_rejection_still_holds(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    resolved = await _call("admin", bt_id=blocked, not_books_relevant_reason=REASON)
    reopen = await _reopen_call(
        "owner", bt_id=blocked, reason="Fictional: book it after all, retry it"
    )
    assert reopen["superseded_rejection_ids"] == [resolved["approval_id"]]
    rows = await _approval_rows(app_db, blocked)
    assert [row["status"] for row in rows] == ["rejected"]
    marker = _json(rows[0]["payload"])["superseded_by_reopen"]
    assert marker["audit_id"] == reopen["audit_id"]

    # Still failing after the reopen: the worker runs the mapper again (the
    # superseded rejection does not hold it), counts up and raises a FRESH
    # pending "(blocked)" row.
    worker.seen.clear()
    outcomes = [(await worker.run())["last_outcome"] for _ in range(3)]
    assert outcomes == ["processing_error", "processing_error", "blocked_item"]
    assert worker.seen == [blocked, blocked, blocked]
    rows = await _approval_rows(app_db, blocked)
    assert [row["status"] for row in rows] == ["rejected", "pending"]

    # An ORDINARY rejection of that fresh row never clears the block: the
    # worker holds the item without running the mapper, nothing is posted.
    decided = await _decide_in_process(
        "admin", rows[1]["id"], "reject", note="Fictional ordinary rejection"
    )
    assert decided["decision"] == "rejected"
    worker.failing = set()  # even with the config fixed
    worker.seen.clear()
    for _ in range(2):
        assert (await worker.run())["last_outcome"] == "blocked_item"
    assert worker.seen == []
    state = await _state(app_db)
    assert state["blocked_item"]["id"] == blocked
    assert await _posts_for(app_db, blocked) == 0
    rows = await _approval_rows(app_db, blocked)
    assert "superseded_by_reopen" not in _json(rows[1]["payload"])


@pytest.mark.asyncio
async def test_rejection_committed_after_the_load_is_honoured_under_the_lock(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 79c78f14 #1: worker past its load, REJECT commits, worker resumes.

    The rejection is read only after the worker holds the shared lock, inside
    the transaction that would map and post. With the config fixed the mapper
    would succeed, so only that locked read keeps the refused item out.
    """
    del seeded_workspace
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    before = await _state(app_db)
    pending = [row for row in await _approval_rows(app_db, blocked) if row["status"] == "pending"]
    assert len(pending) == 1

    real_lock = stripe_sync.lock_block_state
    at_lock = asyncio.Event()
    go = asyncio.Event()

    async def paused_lock(session: Any, entity_id: UUID, *args: Any) -> Any:
        at_lock.set()
        await go.wait()
        return await real_lock(session, entity_id, *args)

    monkeypatch.setattr(stripe_sync, "lock_block_state", paused_lock)
    worker.failing = set()  # config fixed: the mapper WOULD post it
    worker.seen.clear()
    sync = asyncio.create_task(worker.run())  # loaded the block, no rejection yet
    await asyncio.wait_for(at_lock.wait(), timeout=10)
    decided = await _decide_in_process(
        "admin", pending[0]["id"], "reject", note="Fictional rejection mid-pass"
    )
    assert decided["decision"] == "rejected"
    go.set()
    summary = await asyncio.wait_for(sync, timeout=10)
    monkeypatch.setattr(stripe_sync, "lock_block_state", real_lock)

    assert summary["last_outcome"] == "blocked_item"
    assert worker.seen == []
    assert await _posts_for(app_db, blocked) == 0
    state = await _state(app_db)
    assert state["blocked_item"]["id"] == blocked
    assert state.get("_stripe_continuation_cursor") == before.get("_stripe_continuation_cursor")
    assert (state["window_gte"], state["window_lt"]) == (before["window_gte"], before["window_lt"])
    assert state["_stripe_failure_counts"].get(blocked) == 3
    # And it stays held on every later pass.
    assert (await worker.run())["last_outcome"] == "blocked_item"
    assert worker.seen == [] and await _posts_for(app_db, blocked) == 0


@pytest.mark.asyncio
async def test_resolve_waits_for_approval_posting_then_409s(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approval posting holds the Stripe state lock; a racing resolve waits."""
    del seeded_workspace
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    pending = [row for row in await _approval_rows(app_db, blocked) if row["status"] == "pending"]
    assert len(pending) == 1

    real_map = stripe_mapper.map_and_post_balance_transaction
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_map(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return await real_map(*args, **kwargs)

    # app/api/approvals.py imports the mapper at call time from this module.
    monkeypatch.setattr(stripe_mapper, "map_and_post_balance_transaction", paused_map)
    approve = asyncio.create_task(
        _decide_in_process("admin", pending[0]["id"], "approve", note="Fictional approval")
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    resolve = asyncio.create_task(
        _call("owner", bt_id=blocked, not_books_relevant_reason=REASON)
    )
    await asyncio.sleep(0.5)
    assert not resolve.done(), "resolve did not wait for the approval posting"
    release.set()
    approved = await asyncio.wait_for(approve, timeout=10)
    assert approved["applied"] is True and approved["journal_entry_id"]
    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(resolve, timeout=10)
    assert refused.value.status_code == 409
    monkeypatch.setattr(stripe_mapper, "map_and_post_balance_transaction", real_map)

    # No double book: one post, no resolution, no resolve audit.
    assert await _posts_for(app_db, blocked) == 1
    assert await _audits(app_db, "stripe_blocked_item_resolve", blocked) == 0
    state = await _state(app_db)
    assert blocked not in state.get("resolved_blocked_items", {})
    # The worker confirms the approval's post and clears the block itself.
    worker.failing = set()
    assert (await worker.run())["last_outcome"] == "ok"
    assert "blocked_item" not in await _state(app_db)
    assert await _posts_for(app_db, blocked) == 1


@pytest.mark.asyncio
async def test_approval_of_an_item_resolved_elsewhere_is_refused(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve first: the approval path re-checks under the state lock."""
    del seeded_workspace
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    pending = [row for row in await _approval_rows(app_db, blocked) if row["status"] == "pending"]
    # A second pending review row for the same item (e.g. an older first-run
    # review) that the resolve does not decide.
    await _fetch(
        db,
        TEST_WORKSPACE_ID,
        """
        INSERT INTO approval_queue (
            id, workspace_id, entity_id, subject_type, subject_id,
            amount, currency_code, usd_equivalent_at_event_time,
            status, reasoning, requested_by_identity, payload, created_at
        )
        SELECT gen_random_uuid(), workspace_id, entity_id, subject_type,
               gen_random_uuid(), amount, currency_code,
               usd_equivalent_at_event_time, 'pending',
               'fictional second review row', requested_by_identity, payload,
               created_at + interval '1 second'
        FROM approval_queue WHERE id = $1::uuid
        RETURNING id
        """,
        pending[0]["id"],
    )
    await _call("admin", bt_id=blocked, not_books_relevant_reason=REASON)

    rows = await _approval_rows(app_db, blocked)
    decided = [row for row in rows if row["id"] == pending[0]["id"]][0]
    other = [row for row in rows if row["id"] != pending[0]["id"]][0]
    assert decided["status"] == "rejected" and other["status"] == "pending"
    with pytest.raises(HTTPException) as already:
        await _decide_in_process("admin", decided["id"], "approve")
    assert already.value.status_code == 409
    with pytest.raises(HTTPException) as resolved_elsewhere:
        await _decide_in_process("admin", other["id"], "approve")
    assert resolved_elsewhere.value.status_code == 409
    assert "resolved elsewhere" in resolved_elsewhere.value.detail
    assert await _posts_for(app_db, blocked) == 0


# ---------------------------------------------------------------------------
# Review 4bba864c round 4: the side writers of the Stripe state row
# (payout reconciliation, webhook delivery log) take the shared lock and merge
# only their own keys. Each regression pauses the writer right after its read
# while a resolve, then a reopen, try to commit. Before the fix the writer
# replaced the whole JSON and rolled block_revision back.
# ---------------------------------------------------------------------------


async def _race_side_writer(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    write: Any,
    human: Any,
) -> dict[str, Any]:
    """Run ``write`` paused after its locked read; run ``human`` meanwhile."""
    real_lock = module.lock_stripe_state_row
    read = asyncio.Event()
    go = asyncio.Event()

    async def paused_lock(*args: Any, **kwargs: Any) -> Any:
        locked = await real_lock(*args, **kwargs)
        read.set()
        await go.wait()
        return locked

    monkeypatch.setattr(module, "lock_stripe_state_row", paused_lock)
    try:
        writer = asyncio.create_task(write())
        await asyncio.wait_for(read.wait(), 10)
        human_task = asyncio.create_task(human())
        await asyncio.sleep(0.5)
        assert not human_task.done()  # the human call waits on the row lock
        go.set()
        await writer
        return await human_task
    finally:
        go.set()
        monkeypatch.setattr(module, "lock_stripe_state_row", real_lock)


async def _side_writer_keeps_resolve_and_reopen(
    app_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    write: Any,
    side_key: str,
) -> None:
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    before = await _state(app_db)
    revision = before["block_revision"]

    await _race_side_writer(
        monkeypatch,
        module,
        write,
        lambda: _call("admin", bt_id=blocked, not_books_relevant_reason=REASON),
    )
    state = await _state(app_db)
    assert "blocked_item" not in state
    assert blocked in state["resolved_blocked_items"]
    assert state["block_revision"] == revision + 1
    assert side_key in state

    await _race_side_writer(
        monkeypatch,
        module,
        write,
        lambda: _reopen_call(
            "owner", bt_id=blocked, reason="Fictional: reopen during a side write"
        ),
    )
    state = await _state(app_db)
    assert state["blocked_item"]["id"] == blocked
    assert blocked not in state["resolved_blocked_items"]
    assert state["block_revision"] == revision + 2
    assert (state["window_gte"], state["window_lt"]) == (
        before["window_gte"],
        before["window_lt"],
    )
    assert side_key in state
    assert await _posts_for(app_db, blocked) == 0


@pytest.mark.asyncio
async def test_reconciliation_never_erases_a_resolve_or_reopen(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    from app.connectors.stripe import reconcile as reconcile_module

    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)

    async def write() -> None:
        async with workspace_session(SessionLocal, TEST_WORKSPACE_ID) as session:
            await reconcile_module.reconcile_one_entity(
                session, workspace_id=TEST_WORKSPACE_ID, entity_id=TEST_ENTITY_ID
            )

    await _side_writer_keeps_resolve_and_reopen(
        app_db, monkeypatch, reconcile_module, write, "reconciliation"
    )


@pytest.mark.asyncio
async def test_webhook_log_never_erases_a_resolve_or_reopen(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    del seeded_workspace
    from datetime import datetime, timezone

    from app.api import stripe_oauth

    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    events: list[str] = []

    async def write() -> None:
        event_id = f"evt_fictional_{uuid4().hex[:12]}"
        events.append(event_id)
        await stripe_oauth._record_webhook_delivery(
            TEST_WORKSPACE_ID,
            event_id=event_id,
            event_type="balance.available",
            livemode=False,
            api_version="2026-01-01",
            received_at=datetime.now(timezone.utc),
            outcome="handled",
        )

    await _side_writer_keeps_resolve_and_reopen(
        app_db, monkeypatch, stripe_oauth, write, "webhook_log"
    )
    logged = [entry["event_id"] for entry in (await _state(app_db))["webhook_log"]]
    assert logged[-2:] == events


# ---------------------------------------------------------------------------
# Review 706 / 718 P1: a human rejection protects the item on every later pass
# ---------------------------------------------------------------------------


async def _first_run_posts(*args: Any, **kwargs: Any) -> int:
    return 0


async def _reject_on_first_run(
    worker: _Worker, monkeypatch: pytest.MonkeyPatch, bt_id: str
) -> str:
    """Pass 1 queues the item for first-run review; a human rejects it; pass 2
    honours the rejection, advances and clears awaiting_review_item."""
    monkeypatch.setattr(stripe_mapper, "_count_existing_stripe_posts", _first_run_posts)
    assert (await worker.run())["last_outcome"] == "awaiting_review"
    assert (await _state(worker.app_db))["awaiting_review_item"]["id"] == bt_id
    rows = await _approval_rows(worker.app_db, bt_id)
    assert [row["status"] for row in rows] == ["pending"]
    decided = await _decide_in_process(
        "admin", rows[0]["id"], "reject", note="Fictional: not our revenue, never post"
    )
    assert decided["decision"] == "rejected"
    second = await worker.run()
    assert second["last_outcome"] == "ok" and second["skipped"] == 1
    state = await _state(worker.app_db)
    assert "awaiting_review_item" not in state, "the marker the old check relied on is gone"
    return rows[0]["id"]


async def _overlap_pass_posts_nothing(
    worker: _Worker, monkeypatch: pytest.MonkeyPatch, bt_id: str, approval_id: str
) -> None:
    """The first-run guard is off now, so the mapper WOULD post the item."""

    async def past_first_run(*args: Any, **kwargs: Any) -> int:
        return 31

    monkeypatch.setattr(stripe_mapper, "_count_existing_stripe_posts", past_first_run)
    worker.seen.clear()
    for _ in range(2):  # overlap re-read, twice
        summary = await worker.run()
        assert summary["last_outcome"] == "ok"
        assert summary["skipped"] == 1 and summary["posted"] == 0
    assert worker.seen == [], "the mapper ran for a rejected item"
    assert await _posts_for(worker.app_db, bt_id) == 0
    rows = await _approval_rows(worker.app_db, bt_id)
    assert [(row["id"], row["status"]) for row in rows] == [(approval_id, "rejected")]
    record = (await _state(worker.app_db))["rejected_stripe_items"][bt_id]
    assert record["approval_id"] == approval_id


@pytest.mark.asyncio
async def test_rejected_review_stays_unposted_on_next_overlap(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real worker, state loader and outcome writer over three passes."""
    del seeded_workspace
    bt_id = f"stripe-pg-overlap-reject-{uuid4().hex[:10]}"
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, [_txn(bt_id, CREATED + 1)])
    worker.app_db = app_db
    approval_id = await _reject_on_first_run(worker, monkeypatch, bt_id)
    record = (await _state(app_db))["rejected_stripe_items"][bt_id]
    assert record["source"] == "approval_decision"
    assert record["rejected_by_uc_uid"] == HUMAN
    await _overlap_pass_posts_nothing(worker, monkeypatch, bt_id, approval_id)


@pytest.mark.asyncio
async def test_older_rejection_without_the_record_is_honoured_and_recorded(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection decided before the durable record existed still holds."""
    del seeded_workspace
    bt_id = f"stripe-pg-overlap-legacy-{uuid4().hex[:10]}"
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, [_txn(bt_id, CREATED + 1)])
    worker.app_db = app_db
    approval_id = await _reject_on_first_run(worker, monkeypatch, bt_id)
    async with db.acquire() as conn:
        async with conn.transaction():
            await _scoped(conn, TEST_WORKSPACE_ID)
            await conn.execute(
                """
                UPDATE connector_state SET state = state - 'rejected_stripe_items'
                WHERE entity_id = $1 AND connector_name = 'stripe_api'
                """,
                str(TEST_ENTITY_ID),
            )
    await _overlap_pass_posts_nothing(worker, monkeypatch, bt_id, approval_id)
    record = (await _state(app_db))["rejected_stripe_items"][bt_id]
    assert record["source"] == "sync_worker_backfill"


# ---------------------------------------------------------------------------
# Review 706 / 718 P2: resolving a block whose approval is already decided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["skipped", "rejected"])
async def test_resolve_links_an_approval_already_rejected_or_skipped(
    db: Any, app_db: Any, seeded_workspace: Any, monkeypatch: pytest.MonkeyPatch, prior: str
) -> None:
    del seeded_workspace
    feed, _, blocked, _ = _feed(uuid4().hex[:10])
    await _seed(db, TEST_WORKSPACE_ID, TEST_ENTITY_ID)
    worker = _Worker(monkeypatch, feed)
    await _block(worker, blocked)
    rows = await _approval_rows(app_db, blocked)
    assert [row["status"] for row in rows] == ["pending"]
    approval_id = rows[0]["id"]
    ctx, auth = _caller("admin", TEST_WORKSPACE_ID)
    if prior == "rejected":
        await _decide_in_process("admin", approval_id, "reject", note="Fictional earlier rejection")
    else:
        await skip_approval(
            workspace_id=TEST_WORKSPACE_ID,
            approval_id=UUID(approval_id),
            body=SkipRequest(note="Fictional: decide later"),
            workspace=ctx,
        )
    before = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        """
        SELECT status::text AS status, decided_by_uc_uid, decided_at, reasoning
        FROM approval_queue WHERE id = $1
        """,
        approval_id,
    )

    resolved = await _call("admin", bt_id=blocked, not_books_relevant_reason=REASON)

    assert resolved["approval_id"] == approval_id
    assert resolved["resolution"]["approval_prior_decision"]["status"] == prior
    after = await _fetch(
        app_db,
        TEST_WORKSPACE_ID,
        """
        SELECT status::text AS status, decided_by_uc_uid, decided_at, reasoning, payload
        FROM approval_queue WHERE id = $1
        """,
        approval_id,
    )
    assert after[0]["status"] == prior
    assert after[0]["decided_by_uc_uid"] == before[0]["decided_by_uc_uid"]
    assert after[0]["decided_at"] == before[0]["decided_at"]
    assert after[0]["reasoning"].startswith(before[0]["reasoning"] + " | Resolved elsewhere")
    linked = _json(after[0]["payload"])["blocked_resolution"]
    assert linked["approval_id"] == approval_id
    assert linked["audit_id"] == resolved["audit_id"]
    state = await _state(app_db)
    assert "blocked_item" not in state
    assert state["resolved_blocked_items"][blocked]["approval_id"] == approval_id
    assert await _posts_for(app_db, blocked) == 0
    assert await _audits(app_db, "stripe_blocked_item_resolve", blocked) == 1
