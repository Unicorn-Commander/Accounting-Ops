"""Stripe API → journal sync worker.

Run modes
~~~~~~~~~
* ``python scripts/stripe_sync.py``           — single pass; sync every workspace
                                                with a stripe_api_key credential.
* ``python scripts/stripe_sync.py --loop``    — long-running loop with a sleep
                                                between passes (default 3600s
                                                = hourly), used by the compose
                                                ``stripe-sync`` service entrypoint.
* ``python scripts/stripe_sync.py --workspace <uuid>`` — sync only one workspace.

Per pass, for each workspace:
  1. Load + decrypt the stripe_api_key credential blob (via the same CredCrypto
     envelope the rest of the app uses).
  2. Open a Stripe client (Bearer auth; no token refresh needed).
  3. Pull balance_transactions created since `connector_state.last_sync_at`
     (default backstop: 30 days).
  4. Pass every txn through ``app.connectors.stripe.mapper.map_and_post_balance_transaction`` —
     the first 30 rows queue for human review, then auto-post by materiality.
  5. Run reconcile (Stripe payout ↔ BoA deposit ±$1/±2d).
  6. UPDATE connector_state.last_sync_at + state.last_outcome.

Safety
~~~~~~
* Multi-row UPDATE only against connector_state (state.* + last_sync_at).
* journal_entry is INSERT-only (the materiality gate refuses; the DB role
  REVOKEs UPDATE/DELETE).
* On any exception, the per-workspace transaction rolls back and the loop
  continues to the next workspace. Persistent failures show up in
  ``connector_state.state['last_outcome']`` so the UI sees them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.stripe import StripeApiError, StripeClient, load_stripe_credential
from app.connectors.stripe.blocked_resolution import (
    BLOCK_REVISION_KEY,
    MERGE_STATE_SQL,
    REOPENED_STATE_KEY,
    RESOLVED_STATE_KEY,
    SUPERSEDED_BY_REOPEN_KEY,
    block_revision,
    load_resolved_blocked_ids,
    lock_stripe_state_row,
    merge_params,
    resolved_ids,
)
from app.connectors.stripe.client import paginate_balance_transactions
from app.connectors.stripe.mapper import (
    DEFAULT_REVENUE_ACCOUNT,
    enqueue_blocked_balance_transaction,
    map_and_post_balance_transaction,
)
from app.connectors.stripe.reconcile import reconcile_one_entity
from app.db import SessionLocal
from app.workspace_session import workspace_session


_log = logging.getLogger("acctops.stripe_sync")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

DEFAULT_BACKFILL_DAYS = 30
DEFAULT_LOOP_INTERVAL_S = int(os.getenv("STRIPE_SYNC_INTERVAL_S", "3600"))
STRIPE_WINDOW_OVERLAP_S = 24 * 60 * 60

# A mapper refusal is not proof that the item was handled. Only these
# outcomes mean that the external_ref is safe to move past: a journal entry
# was written, a duplicate was confirmed, or the item is an intentional zero
# amount skip. Review outcomes remain at the head of the chain until their
# approval path has actually written the journal entry.
_ADVANCE_MAPPER_OUTCOMES = frozenset({
    "posted",
    "blocked_idempotent",
    "skipped_zero_amount",
    "human_rejected",
})
_REVIEW_MAPPER_OUTCOMES = frozenset({
    "queued_for_confirm",
    "queued_first_run",
    "flagged_for_cpa",
})


# ---------------------------------------------------------------------------
# Workspace + entity discovery
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Cross-workspace discovery session
# ---------------------------------------------------------------------------
#
# Discovery has to look ACROSS workspaces, and app.db.SessionLocal connects as
# the runtime role. public.tenant_credentials has RLS enabled (not forced) with
# a workspace_isolation policy keyed on app.current_workspace — a GUC a
# cross-workspace query has no single value for. The runtime role therefore
# matched zero rows and discovery returned [] on a database that plainly held
# the credential: the worker looked healthy and did nothing, for months.
#
# accounting_ops_admin OWNS that table and the table is not FORCE RLS, so the
# owner is exempt and sees every workspace's row. Per-workspace work below still
# goes through workspace_session() as the runtime role, which is what actually
# enforces isolation — only this one discovery query is privileged.
_ADMIN_SESSIONMAKER = None


def _admin_sessionmaker():
    """Sessionmaker bound to ADMIN_DATABASE_URL (falls back to DATABASE_URL)."""
    global _ADMIN_SESSIONMAKER
    if _ADMIN_SESSIONMAKER is None:
        url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
        if not url:
            raise SystemExit(
                "stripe_sync: neither ADMIN_DATABASE_URL nor DATABASE_URL is set"
            )
        _ADMIN_SESSIONMAKER = async_sessionmaker(
            create_async_engine(url, pool_pre_ping=True),
            expire_on_commit=False,
        )
    return _ADMIN_SESSIONMAKER


async def _workspaces_with_stripe() -> list[dict[str, Any]]:
    """Find every workspace that has at least one stripe_api_key credential."""
    async with _admin_sessionmaker()() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT
                            tc.workspace_id::text AS workspace_id,
                            w.slug              AS slug,
                            w.display_name      AS display_name
                        FROM public.tenant_credentials tc
                        JOIN public.workspace w ON w.id = tc.workspace_id
                        WHERE tc.connector = 'stripe_api_key'
                          AND w.status = 'active'
                        ORDER BY w.display_name
                        """
                    )
                )
            ).mappings().all()
    return [dict(r) for r in rows]


async def _entity_for_credential(
    workspace_id: UUID, entity_id_hint: str | None
) -> UUID | None:
    """Pick the entity to attribute Stripe revenue to.

    Priority:
      1. The entity_id encoded in the credential blob.
      2. Connector_state row with connector_name='stripe_api'.
      3. The single c_corp / s_corp / llc entity in the workspace.
      4. None — caller should skip.
    """
    if entity_id_hint:
        try:
            return UUID(entity_id_hint)
        except ValueError:
            pass
    async with workspace_session(SessionLocal, workspace_id) as session:
        r = (
            await session.execute(
                text(
                    """
                    SELECT entity_id::text FROM connector_state
                    WHERE connector_name = 'stripe_api'
                    ORDER BY last_sync_at DESC NULLS LAST
                    LIMIT 1
                    """
                )
            )
        ).first()
        if r:
            return UUID(r.entity_id)
        r = (
            await session.execute(
                text(
                    """
                    SELECT id::text FROM entity
                    WHERE status = 'active' AND kind IN ('c_corp', 's_corp', 'llc')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """
                )
            )
        ).first()
        if r:
            return UUID(r.id)
    return None


# ---------------------------------------------------------------------------
# connector_state read/write
# ---------------------------------------------------------------------------


async def _load_last_sync(
    workspace_id: UUID, entity_id: UUID
) -> datetime | None:
    async with workspace_session(SessionLocal, workspace_id) as session:
        r = (
            await session.execute(
                text(
                    """
                    SELECT last_sync_at FROM connector_state
                    WHERE entity_id = :eid AND connector_name = 'stripe_api'
                    LIMIT 1
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).first()
    return r.last_sync_at if r and r.last_sync_at else None


async def _load_block_revision(workspace_id: UUID, entity_id: UUID) -> int:
    """Read the block revision a pass carries (see BLOCK_REVISION_KEY).

    Called BEFORE ``_load_sync_position``. Any resolve, reopen or block
    transition after this read raises the stored revision above the carried
    one, so the pass's outcome write is treated as stale. A change that lands
    between the two reads makes the pass look stale while its position is in
    fact fresh; that only costs one pass that does not move the position.
    """
    async with workspace_session(SessionLocal, workspace_id) as session:
        r = (
            await session.execute(
                text(
                    """
                    SELECT state FROM connector_state
                    WHERE entity_id = :eid AND connector_name = 'stripe_api'
                      AND connector_instance_name = 'default'
                    LIMIT 1
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).first()
    state = r.state if r is not None and isinstance(r.state, dict) else {}
    return block_revision(state)


async def _load_sync_position(
    workspace_id: UUID, entity_id: UUID
) -> tuple[
    datetime | None,
    str | None,
    int | None,
    int | None,
    dict[str, int],
    dict[str, Any] | None,
]:
    """Load the durable Stripe chain position and its fixed time window."""
    async with workspace_session(SessionLocal, workspace_id) as session:
        r = (
            await session.execute(
                text(
                    """
                    SELECT last_sync_at, state FROM connector_state
                    WHERE entity_id = :eid AND connector_name = 'stripe_api'
                    LIMIT 1
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).first()
    if r is None:
        return None, None, None, None, {}, None
    state = r.state if isinstance(r.state, dict) else {}
    cursor = state.get("_stripe_continuation_cursor")
    blocked_item = state.get("blocked_item")
    if not isinstance(blocked_item, dict) or not isinstance(blocked_item.get("id"), str):
        blocked_item = None

    def _epoch(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    window_gte = _epoch(state.get("window_gte"))
    window_lt = _epoch(state.get("window_lt"))
    # Cursors written by the first revision predate fixed windows. Re-read
    # from the time overlap rather than carrying a cursor whose filter can no
    # longer be reconstructed safely.
    if (window_gte is None or window_lt is None) and blocked_item is None:
        cursor = None
        window_gte = None
        window_lt = None

    failure_counts = state.get("_stripe_failure_counts")
    if not isinstance(failure_counts, dict):
        failure_counts = {}
    failure_counts = {
        str(key): int(value)
        for key, value in failure_counts.items()
        if isinstance(key, str)
        and not isinstance(value, bool)
        and isinstance(value, (int, str))
        and str(value).isdigit()
        and int(value) > 0
    }
    return (
        r.last_sync_at,
        cursor if isinstance(cursor, str) else None,
        window_gte,
        window_lt,
        failure_counts,
        blocked_item,
    )


async def _record_sync_outcome(
    workspace_id: UUID,
    entity_id: UUID,
    *,
    outcome_summary: dict[str, Any],
    last_sync_at: datetime | None,
    continuation_cursor: str | None,
    window_gte: int | None,
    window_lt: int | None,
    failure_counts: dict[str, int] | None = None,
    blocked_item: dict[str, Any] | None = None,
    clear_blocked_item: bool = False,
    carried_blocked_id: str | None = None,
    carried_revision: int | None = None,
    instance_name: str = "default",
) -> int:
    """Merge this pass's keys into connector_state under the shared lock.

    Takes the same row lock as the manual resolve/reopen
    (app/connectors/stripe/blocked_resolution.py) and writes a jsonb merge of
    only the worker's own keys, so a resolution committed while this pass ran
    is never erased. ``carried_blocked_id`` is the blocked item this pass
    loaded at its start. If a human changed the block since (resolved the
    carried item, or reopened an item into the blocked slot), this pass is
    stale: its block, cursor, window and last_sync_at are not written, so a
    resolved item neither comes back nor rewinds the chain and a reopened item
    keeps the window it was re-armed in.

    ``carried_revision`` is the block revision the pass loaded before its
    position. A stored revision above it means a resolve, reopen or another
    block transition happened since, even when the blocked id is the same one
    the pass loaded (resolve then reopen of that id). Such a write is stale.
    A non-stale write that sets a new blocked item or clears one bumps the
    revision. Returns the revision the pass should carry into its next write:
    the stored one after a fresh write, the old carried one after a stale
    write (so every later write of that pass stays stale).
    """
    async with workspace_session(SessionLocal, workspace_id) as session:
        locked = await lock_stripe_state_row(session, entity_id, instance_name)
        existing_id, locked_state = locked if locked is not None else (None, {})
        resolved = resolved_ids(locked_state)
        owned_by_human = {RESOLVED_STATE_KEY, REOPENED_STATE_KEY, "blocked_item"}
        patch: dict[str, Any] = {
            key: value
            for key, value in outcome_summary.items()
            if key not in owned_by_human
        }
        drop: set[str] = set()
        if blocked_item is not None:
            patch["blocked_item"] = blocked_item
        elif clear_blocked_item:
            drop.add("blocked_item")
        last_outcome = outcome_summary.get("last_outcome")
        if last_outcome == "awaiting_review":
            patch["awaiting_review_item"] = outcome_summary.get(
                "awaiting_review_item"
            )
        elif last_outcome in {"ok", "processing_error", "blocked_item"}:
            patch.pop("awaiting_review_item", None)
            drop.add("awaiting_review_item")
        if continuation_cursor is None:
            drop.add("_stripe_continuation_cursor")
        else:
            patch["_stripe_continuation_cursor"] = continuation_cursor
        if window_gte is None or window_lt is None:
            drop.update({"window_gte", "window_lt"})
        else:
            patch["window_gte"] = window_gte
            patch["window_lt"] = window_lt
        counts = {
            key: value
            for key, value in (failure_counts or {}).items()
            if key not in resolved
        }
        if counts:
            patch["_stripe_failure_counts"] = counts
        else:
            drop.add("_stripe_failure_counts")
        write_last_sync = True
        locked_block = locked_state.get("blocked_item")
        locked_block_id = (
            locked_block.get("id") if isinstance(locked_block, dict) else None
        )
        writing_block_id = (
            blocked_item.get("id") if isinstance(blocked_item, dict) else None
        )
        locked_revision = block_revision(locked_state)
        new_revision = locked_revision
        stale = (
            (carried_revision is not None and carried_revision < locked_revision)
            or (carried_blocked_id is not None and carried_blocked_id in resolved)
            or (writing_block_id is not None and writing_block_id in resolved)
            or (locked_block_id is not None and locked_block_id != carried_blocked_id)
        )
        if stale:
            # A human resolved or reopened a blocked item while this pass ran.
            # Keep the human's position; re-blocking or rewinding here would
            # undo that decision.
            _log.warning(
                "stripe_sync_stale_outcome workspace=%s entity=%s carried=%s "
                "now_blocked=%s carried_revision=%s now_revision=%s; "
                "human change during this pass, position kept",
                workspace_id,
                entity_id,
                carried_blocked_id,
                locked_block_id,
                carried_revision,
                locked_revision,
            )
            for key in (
                "blocked_item",
                "_stripe_continuation_cursor",
                "window_gte",
                "window_lt",
                "awaiting_review_item",
                "_stripe_failure_counts",
            ):
                patch.pop(key, None)
                drop.discard(key)
            patch["last_outcome"] = "superseded_by_resolution"
            write_last_sync = False
        elif ("blocked_item" in patch and writing_block_id != locked_block_id) or (
            "blocked_item" in drop and locked_block_id is not None
        ):
            # This pass moves the block itself: a new item blocked, or the
            # carried block cleared after it posted.
            new_revision = locked_revision + 1
        patch.pop(BLOCK_REVISION_KEY, None)
        drop.discard(BLOCK_REVISION_KEY)
        if new_revision != locked_revision:
            patch[BLOCK_REVISION_KEY] = new_revision
        result_revision = (
            carried_revision if stale and carried_revision is not None else new_revision
        )
        if existing_id is not None:
            await session.execute(
                text(
                    MERGE_STATE_SQL.replace(
                        "WHERE id = :id",
                        ", last_sync_at = :last_sync_at WHERE id = :id",
                    )
                    if write_last_sync
                    else MERGE_STATE_SQL
                ),
                {
                    **merge_params(existing_id, patch, drop),
                    **({"last_sync_at": last_sync_at} if write_last_sync else {}),
                },
            )
            return result_revision
        await session.execute(
            text(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                )
                VALUES (
                    gen_random_uuid(), :wid, :eid, 'stripe_api',
                    :instance, CAST(:state AS jsonb), :last_sync_at
                )
                ON CONFLICT (entity_id, connector_name, connector_instance_name) DO UPDATE
                SET last_sync_at = EXCLUDED.last_sync_at,
                    state = (COALESCE(connector_state.state, '{}'::jsonb)
                             - CAST(:drop_keys AS text[])) || EXCLUDED.state
                """
            ),
            {
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "instance": instance_name,
                "state": json.dumps(patch),
                "drop_keys": sorted(drop - set(patch)),
                "last_sync_at": last_sync_at,
            },
        )
        return result_revision


def _durable_position_after_item(
    item: dict[str, Any] | None,
    *,
    fallback_last_sync: datetime | None,
    fallback_cursor: str | None,
) -> tuple[datetime | None, str | None]:
    """Return a position no later than the last successfully mapped item."""
    if item is None:
        return fallback_last_sync, fallback_cursor
    item_id = item.get("id")
    created = item.get("created")
    if not isinstance(item_id, str) or not item_id or created is None:
        return fallback_last_sync, fallback_cursor
    try:
        item_sync = datetime.fromtimestamp(int(created), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return fallback_last_sync, fallback_cursor
    return item_sync, item_id


def _is_missing_starting_after_error(exc: StripeApiError) -> bool:
    """Only Stripe's deleted-cursor response may restart a pinned window."""
    if exc.status_code != 400:
        return False
    try:
        body = json.loads(exc.body)
    except (TypeError, ValueError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    return (
        isinstance(error, dict)
        and error.get("code") == "resource_missing"
        and error.get("param") == "starting_after"
    )


async def _journal_entry_exists(
    session: Any, *, entity_id: UUID, source_external_id: str
) -> bool:
    """Confirm an idempotent mapper result has a durable journal row."""
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM journal_entry
                WHERE entity_id = :eid AND source = 'stripe_api'
                  AND source_external_id = :ext
                LIMIT 1
                """
            ),
            {"eid": str(entity_id), "ext": source_external_id},
        )
    ).first()
    return row is not None


async def _stripe_item_was_rejected(
    session: Any,
    *,
    entity_id: UUID,
    stripe_balance_txn_id: str,
    awaiting_review_only: bool = False,
) -> bool:
    """Return whether a human explicitly rejected this Stripe item.

    Normal cursor advancement is limited to the worker's persisted
    ``awaiting_review_item`` marker, which is only written for review outcomes
    that may be passed by an explicit human decision. The blocked-item path
    deliberately uses the broader row check so a rejected blocked approval can
    be held without invoking the mapper.

    A rejection marked ``superseded_by_reopen`` is ignored: an audited reopen
    (app/connectors/stripe/blocked_resolution.py reopen_resolved_item) is the
    later human decision to run the mapper again. An ordinary rejection never
    carries that marker, so it still never clears a block.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM approval_queue
                WHERE entity_id = :eid
                  AND subject_type = 'stripe_post_balance_transaction'
                  AND status = 'rejected'
                  AND payload ->> 'stripe_balance_txn_id' = :stripe_id
                  AND payload -> :superseded_key IS NULL
                  AND (
                      :awaiting_review_only = false
                      OR EXISTS (
                          SELECT 1 FROM connector_state
                          WHERE entity_id = :eid
                            AND connector_name = 'stripe_api'
                            AND connector_instance_name = 'default'
                            AND state -> 'awaiting_review_item' ->> 'id' = :stripe_id
                      )
                  )
                ORDER BY decided_at DESC NULLS LAST, created_at DESC
                LIMIT 1
                """
            ),
            {
                "eid": str(entity_id),
                "stripe_id": stripe_balance_txn_id,
                "awaiting_review_only": awaiting_review_only,
                "superseded_key": SUPERSEDED_BY_REOPEN_KEY,
            },
        )
    ).first()
    return row is not None


# ---------------------------------------------------------------------------
# Per-workspace sync
# ---------------------------------------------------------------------------


async def _sync_one_workspace(workspace_id: UUID) -> dict[str, Any]:
    """Pull + post for one workspace. Returns a structured summary."""
    summary: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "started_at": datetime.now(tz=timezone.utc).isoformat(),
        "balance_transactions_seen": 0,
        "posted": 0,
        "queued": 0,
        "skipped": 0,
        "flagged_for_cpa": 0,
        "errors": [],
    }

    cred = await load_stripe_credential(
        workspace_id,
        requesting_service="stripe_sync",
        actor_uc_uid="agent:stripe_sync",
    )
    if cred is None:
        summary["last_outcome"] = "no_credential"
        return summary

    entity_id = await _entity_for_credential(workspace_id, cred.entity_id)
    if entity_id is None:
        summary["last_outcome"] = "no_entity"
        summary["errors"].append("No active entity to attribute Stripe revenue to.")
        return summary

    # Revision first, then position: see _load_block_revision.
    loaded_revision: int | None = await _load_block_revision(workspace_id, entity_id)
    (
        last_sync,
        continuation_cursor,
        window_gte,
        window_lt,
        failure_counts,
        blocked_item,
    ) = await _load_sync_position(workspace_id, entity_id)
    loaded_continuation_cursor = continuation_cursor
    # The block this pass started from. Outcome writes pass it so a human
    # resolve/reopen that lands mid-pass is never overwritten.
    loaded_blocked_id = blocked_item["id"] if blocked_item is not None else None
    loaded_window_lt = window_lt
    if window_gte is None:
        start_date = (
            last_sync - timedelta(seconds=STRIPE_WINDOW_OVERLAP_S)
            if last_sync
            else datetime.now(tz=timezone.utc) - timedelta(days=DEFAULT_BACKFILL_DAYS)
        )
        # Stripe filter is unix epoch seconds. The lower bound is pinned in
        # state for every pass of this chain.
        window_gte = int(start_date.timestamp())
    else:
        start_date = datetime.fromtimestamp(window_gte, tz=timezone.utc)

    client = StripeClient.from_credential(cred)

    # ---- 1. Pull balance_transactions ----
    bts = None
    try:
        bts = await paginate_balance_transactions(
            client,
            starting_after=continuation_cursor,
            created_gte=window_gte,
            created_lt=window_lt,
            max_pages=50,
        )
    except StripeApiError as exc:
        # Stripe cursors are object IDs, not durable tokens. A deleted or
        # expired object returns 400. Re-read the same pinned window from its
        # newest end so the chain cannot wedge forever.
        if continuation_cursor and _is_missing_starting_after_error(exc):
            _log.warning(
                "stripe_sync_invalid_continuation workspace=%s entity=%s "
                "window_gte=%s window_lt=%s; restarting pinned window",
                workspace_id,
                entity_id,
                window_gte,
                window_lt,
            )
            continuation_cursor = None
            try:
                bts = await paginate_balance_transactions(
                    client,
                    created_gte=window_gte,
                    created_lt=window_lt,
                    max_pages=50,
                )
            except StripeApiError as retry_exc:
                exc = retry_exc
            else:
                # Continue through the normal mapping path below.
                pass
        if bts is None:
            summary["last_outcome"] = "api_error_balance_transactions"
            summary["errors"].append(f"list_balance_transactions: {exc}")
            if blocked_item is not None:
                summary["blocked_item"] = blocked_item
            await _record_sync_outcome(
                workspace_id,
                entity_id,
                outcome_summary=summary,
                last_sync_at=last_sync,
                continuation_cursor=continuation_cursor,
                window_gte=window_gte if window_lt is not None else None,
                window_lt=window_lt,
                failure_counts=failure_counts,
                carried_blocked_id=loaded_blocked_id,
                carried_revision=loaded_revision,
                blocked_item=blocked_item,
            )
            return summary
    window_lt = bts.window_lt or window_lt
    if loaded_window_lt is None and window_lt is not None:
        # Establish the chain window before mapping. If the worker dies while
        # mapping, the retry still uses this exact Stripe-derived upper bound.
        loaded_revision = await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=last_sync,
            continuation_cursor=continuation_cursor,
            window_gte=window_gte,
            window_lt=window_lt,
            failure_counts=failure_counts,
            carried_blocked_id=loaded_blocked_id,
            carried_revision=loaded_revision,
            blocked_item=blocked_item,
        )
    summary["balance_transactions_seen"] = len(bts)
    _log.info(
        "stripe_sync workspace=%s entity=%s pulled %d balance_transactions since %s",
        workspace_id, entity_id, len(bts), start_date.isoformat(),
    )

    retrying_blocked = blocked_item is not None
    awaiting_review = False
    items_to_process = bts
    if retrying_blocked:
        blocked_id = blocked_item["id"]
        exact_item = next(
            (item for item in bts if item.get("id") == blocked_id),
            None,
        )
        if exact_item is None:
            summary["blocked_item"] = blocked_item
            summary["last_outcome"] = "blocked_item"
            summary["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
            summary["errors"].append(
                f"persisted blocked Stripe item {blocked_id} was not returned"
            )
            # A capped pull is only a partial search. Persist its continuation
            # inside the pinned window so a later pass can reach the exact
            # blocked row. An exhausted pull proved that the row is absent;
            # either way the financial position stays where it was.
            search_cursor = (
                bts.continuation_cursor if not bts.exhausted else None
            )
            await _record_sync_outcome(
                workspace_id,
                entity_id,
                outcome_summary=summary,
                last_sync_at=last_sync,
                continuation_cursor=search_cursor,
                window_gte=window_gte,
                window_lt=window_lt,
                failure_counts=failure_counts,
                carried_blocked_id=loaded_blocked_id,
                carried_revision=loaded_revision,
                blocked_item=blocked_item,
            )
            return summary
        items_to_process = [exact_item]

    # ---- 2. Map + post each ----
    revenue_account = cred.default_revenue_account or DEFAULT_REVENUE_ACCOUNT
    async def _map_or_skip_rejected(session: Any, bt: dict[str, Any]) -> Any:
        bt_id = bt.get("id")
        if await _stripe_item_was_rejected(
            session,
            entity_id=entity_id,
            stripe_balance_txn_id=str(bt_id or ""),
            awaiting_review_only=True,
        ):
            # A rejected review is the one explicit human decision that
            # permits the cursor to pass without a journal row.
            return type("RejectedStripeItem", (), {
                "outcome": "human_rejected",
                "reason": "Rejected by human reviewer",
            })()
        # Keep every statement for one balance transaction inside its own
        # savepoint. A DBAPI error aborts the current PostgreSQL transaction
        # until that savepoint is rolled back; without this boundary, earlier
        # posts are lost when the outer workspace transaction exits.
        async with session.begin_nested():
            return await map_and_post_balance_transaction(
                session,
                workspace_id=str(workspace_id),
                entity_id=str(entity_id),
                txn=bt,
                actor_uc_uid="agent:stripe_sync",
                actor_agent_id="stripe_sync_worker",
                revenue_account=revenue_account,
            )

    last_processed: dict[str, Any] | None = None
    processing_error = False
    async with workspace_session(SessionLocal, workspace_id) as session:
        # Take the shared Stripe state lock FIRST and only then read the ids a
        # human resolved elsewhere (manual journal entry or "not
        # books-relevant"). The lock is held until this transaction commits,
        # so no resolution can land between this read and a post below; a
        # resolve call waits for this pass (blocked_resolution.LOCK_TIMEOUT_MS)
        # and then refuses because the item is posted. Posting a resolved item
        # would double-book it, so a re-read moves past it without the mapper.
        resolved_elsewhere = await load_resolved_blocked_ids(session, entity_id)
        for bt in items_to_process:
            bt_id = bt.get("id")
            if isinstance(bt_id, str) and bt_id in resolved_elsewhere:
                last_processed = bt
                failure_counts.pop(bt_id, None)
                summary["skipped"] += 1
                continue
            try:
                if retrying_blocked and await _stripe_item_was_rejected(
                    session,
                    entity_id=entity_id,
                    stripe_balance_txn_id=str(bt_id or ""),
                ):
                    # An ordinary rejection can release an awaiting-review
                    # item, but it is not evidence-backed resolution of a
                    # persisted block. Keep the exact item at the head of the
                    # chain and do not run the mapper after this decision.
                    summary["blocked_item"] = blocked_item
                    summary["errors"].append(
                        f"persisted blocked Stripe item {bt_id} was rejected; "
                        "evidence-backed resolution is required"
                    )
                    break
                result = await _map_or_skip_rejected(session, bt)
            except Exception as exc:
                error_text = f"{exc.__class__.__name__}: {exc}"
                if isinstance(bt_id, str) and bt_id:
                    failure_counts[bt_id] = min(
                        failure_counts.get(bt_id, 0) + 1, 3
                    )
                summary["errors"].append(
                    f"map_and_post {bt.get('id', '?')}: {error_text}"
                )
                if (
                    not isinstance(bt_id, str)
                    or not bt_id
                    or failure_counts.get(bt_id, 0) < 3
                ):
                    processing_error = True
                    break
                # The mapper savepoint has now rolled back. Keep the failed
                # item at the head of the chain and raise a visible alert.
                try:
                    async with session.begin_nested():
                        await enqueue_blocked_balance_transaction(
                            session,
                            workspace_id=str(workspace_id),
                            entity_id=str(entity_id),
                            txn=bt,
                            actor_uc_uid="agent:stripe_sync",
                            error=error_text,
                            revenue_account=revenue_account,
                        )
                except Exception as alert_exc:
                    summary["errors"].append(
                        "blocked alert "
                        f"{bt_id}: {alert_exc.__class__.__name__}: {alert_exc}"
                    )
                blocked_item = {
                    "id": bt_id,
                    "amount": bt.get("amount"),
                    "currency": bt.get("currency"),
                    "error": error_text,
                }
                summary["blocked_item"] = blocked_item
                _log.error(
                    "stripe_sync_blocked_item workspace=%s entity=%s id=%s "
                    "amount=%s currency=%s error=%s",
                    workspace_id,
                    entity_id,
                    bt_id,
                    bt.get("amount"),
                    bt.get("currency"),
                    error_text,
                )
                break
            result_outcome = result.outcome
            result_reason = getattr(result, "reason", None) or "no reason supplied"
            idempotency_confirmed = False
            if result_outcome in _REVIEW_MAPPER_OUTCOMES:
                # The approval row is deliberately not enough to advance the
                # chain. The approval-posting path must create the journal row;
                # confirm it on this same database transaction before moving
                # past the Stripe item.
                if isinstance(bt_id, str) and bt_id:
                    try:
                        idempotency_confirmed = await _journal_entry_exists(
                            session,
                            entity_id=entity_id,
                            source_external_id=bt_id,
                        )
                    except Exception as exc:
                        result_reason = (
                            "could not confirm review journal entry: "
                            f"{exc.__class__.__name__}: {exc}"
                        )
                if not idempotency_confirmed:
                    summary["awaiting_review_item"] = {
                        "id": bt_id,
                        "amount": bt.get("amount"),
                        "currency": bt.get("currency"),
                    }
                    summary["errors"].append(
                        f"awaiting review {bt.get('id', '?')}: {result_reason}"
                    )
                    awaiting_review = True
                    break
                result_outcome = "blocked_idempotent"
            if result.outcome == "blocked_idempotent":
                try:
                    confirmed = idempotency_confirmed or await _journal_entry_exists(
                        session, entity_id=entity_id, source_external_id=bt_id
                    )
                except Exception as exc:
                    confirmed = False
                    result_reason = (
                        "could not confirm idempotency journal entry: "
                        f"{exc.__class__.__name__}: {exc}"
                    )
                if not confirmed:
                    result_outcome = "unconfirmed_idempotent"
                    if result_reason == "no reason supplied":
                        result_reason = (
                            "mapper returned blocked_idempotent without a "
                            "journal_entry for this Stripe transaction"
                        )
            if result_outcome not in _ADVANCE_MAPPER_OUTCOMES:
                error_text = (
                    f"mapper refusal {result_outcome}: {result_reason}"
                )
                if isinstance(bt_id, str) and bt_id:
                    failure_counts[bt_id] = min(
                        failure_counts.get(bt_id, 0) + 1, 3
                    )
                summary["errors"].append(
                    f"map_and_post {bt.get('id', '?')}: {error_text}"
                )
                if (
                    not isinstance(bt_id, str)
                    or not bt_id
                    or failure_counts.get(bt_id, 0) < 3
                ):
                    processing_error = True
                    break
                try:
                    async with session.begin_nested():
                        await enqueue_blocked_balance_transaction(
                            session,
                            workspace_id=str(workspace_id),
                            entity_id=str(entity_id),
                            txn=bt,
                            actor_uc_uid="agent:stripe_sync",
                            error=error_text,
                            revenue_account=revenue_account,
                        )
                except Exception as alert_exc:
                    summary["errors"].append(
                        "blocked alert "
                        f"{bt_id}: {alert_exc.__class__.__name__}: {alert_exc}"
                    )
                summary["blocked_item"] = {
                    "id": bt_id,
                    "amount": bt.get("amount"),
                    "currency": bt.get("currency"),
                    "error": error_text,
                }
                _log.error(
                    "stripe_sync_blocked_item workspace=%s entity=%s id=%s "
                    "amount=%s currency=%s error=%s",
                    workspace_id,
                    entity_id,
                    bt_id,
                    bt.get("amount"),
                    bt.get("currency"),
                    error_text,
                )
                break

            if retrying_blocked and result_outcome not in {
                "posted", "blocked_idempotent", "human_rejected"
            }:
                error_text = (
                    f"persisted blocked item returned non-posting outcome: "
                    f"{result_outcome}"
                )
                if isinstance(bt_id, str) and bt_id:
                    failure_counts[bt_id] = min(
                        failure_counts.get(bt_id, 0) + 1, 3
                    )
                summary["errors"].append(
                    f"map_and_post {bt.get('id', '?')}: {error_text}"
                )
                processing_error = True
                break

            last_processed = bt
            if isinstance(bt_id, str):
                # A failure counter is cleared only after this same item
                # reaches an advancing outcome.
                failure_counts.pop(bt_id, None)
            if result_outcome == "posted":
                summary["posted"] += 1
                if result.fee_journal_entry_id:
                    summary["posted"] += 1  # sidecar fee entry counts too
            elif result_outcome in ("queued_for_confirm", "queued_first_run"):
                summary["queued"] += 1
            elif result_outcome == "flagged_for_cpa":
                summary["flagged_for_cpa"] += 1
            elif result_outcome == "blocked_idempotent":
                pass
            else:
                summary["skipped"] += 1

    if awaiting_review:
        summary["last_outcome"] = "awaiting_review"
        summary["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
        _, next_cursor = _durable_position_after_item(
            last_processed,
            fallback_last_sync=last_sync,
            fallback_cursor=continuation_cursor,
        )
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=last_sync,
            continuation_cursor=next_cursor,
            window_gte=window_gte,
            window_lt=window_lt,
            failure_counts=failure_counts,
            carried_blocked_id=loaded_blocked_id,
            carried_revision=loaded_revision,
            blocked_item=blocked_item,
        )
        return summary

    if processing_error:
        summary["last_outcome"] = "processing_error"
        summary["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
        _, next_cursor = _durable_position_after_item(
            last_processed,
            fallback_last_sync=last_sync,
            fallback_cursor=continuation_cursor,
        )
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=last_sync,
            continuation_cursor=next_cursor,
            window_gte=window_gte,
            window_lt=window_lt,
            failure_counts=failure_counts,
            carried_blocked_id=loaded_blocked_id,
            carried_revision=loaded_revision,
            blocked_item=blocked_item,
        )
        return summary

    if "blocked_item" in summary:
        summary["last_outcome"] = "blocked_item"
        summary["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
        _, next_cursor = _durable_position_after_item(
            last_processed,
            fallback_last_sync=last_sync,
            fallback_cursor=continuation_cursor,
        )
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=last_sync,
            continuation_cursor=next_cursor,
            window_gte=window_gte,
            window_lt=window_lt,
            failure_counts=failure_counts,
            carried_blocked_id=loaded_blocked_id,
            carried_revision=loaded_revision,
            blocked_item=summary.get("blocked_item") or blocked_item,
        )
        return summary

    if retrying_blocked:
        # A persisted block owns the pass. Even after the exact item succeeds,
        # leave the fixed window and last_sync_at in place so the next pass
        # continues from this item and nothing after it is skipped.
        summary["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
        summary["last_outcome"] = "ok"
        _, next_cursor = _durable_position_after_item(
            last_processed,
            fallback_last_sync=last_sync,
            fallback_cursor=loaded_continuation_cursor,
        )
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=last_sync,
            continuation_cursor=next_cursor,
            window_gte=window_gte,
            window_lt=window_lt,
            failure_counts=failure_counts,
            carried_blocked_id=loaded_blocked_id,
            carried_revision=loaded_revision,
            clear_blocked_item=True,
        )
        return summary

    if bts.exhausted and window_lt is not None:
        next_sync_at = datetime.fromtimestamp(window_lt, tz=timezone.utc)
        next_cursor = None
        next_window_gte = None
        next_window_lt = None
    else:
        next_sync_at = last_sync
        next_cursor = bts.continuation_cursor
        next_window_gte = window_gte
        next_window_lt = window_lt

    summary["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
    summary["last_outcome"] = "ok"

    # Persist the cursor before reconciliation. Reconciliation is best effort,
    # and must not erase progress from a capped pass when it raises.
    loaded_revision = await _record_sync_outcome(
        workspace_id,
        entity_id,
        outcome_summary=summary,
        last_sync_at=next_sync_at,
        continuation_cursor=next_cursor,
        window_gte=next_window_gte,
        window_lt=next_window_lt,
        failure_counts=failure_counts,
        carried_blocked_id=loaded_blocked_id,
        carried_revision=loaded_revision,
        clear_blocked_item=False,
    )

    # ---- 3. Reconcile Stripe payouts ↔ BoA deposits ----
    try:
        async with workspace_session(SessionLocal, workspace_id) as session:
            recon = await reconcile_one_entity(
                session, workspace_id=workspace_id, entity_id=entity_id,
            )
            summary["reconciliation"] = {
                "checked": recon["checked_payouts"],
                "matched": recon["matched"],
                "unmatched": recon["unmatched"],
            }
    except Exception as exc:
        summary["errors"].append(f"reconcile: {exc.__class__.__name__}: {exc}")

    await _record_sync_outcome(
        workspace_id,
        entity_id,
        outcome_summary=summary,
        last_sync_at=next_sync_at,
        continuation_cursor=next_cursor,
        window_gte=next_window_gte,
        window_lt=next_window_lt,
        failure_counts=failure_counts,
        carried_blocked_id=loaded_blocked_id,
        carried_revision=loaded_revision,
        clear_blocked_item=False,
    )
    return summary


async def _sync_all_workspaces(
    workspace_filter: UUID | None = None,
) -> list[dict[str, Any]]:
    workspaces = await _workspaces_with_stripe()
    if not workspaces:
        # Loud on EVERY cycle, not just the one-shot path. An empty discovery
        # is indistinguishable from "nothing to do" in the logs, which is
        # exactly how this worker sat healthy and idle while a credential was
        # configured the whole time.
        _log.warning(
            "stripe_sync_no_workspaces_with_credential — discovery returned 0 "
            "workspaces. If a stripe credential IS configured, this is a fault, "
            "not an idle cycle."
        )
    out = []
    for w in workspaces:
        wid = UUID(w["workspace_id"])
        if workspace_filter is not None and wid != workspace_filter:
            continue
        try:
            s = await _sync_one_workspace(wid)
        except Exception as exc:
            _log.exception("stripe_sync_unhandled_failure workspace=%s", wid)
            s = {
                "workspace_id": str(wid),
                "last_outcome": "unhandled_exception",
                "errors": [f"{exc.__class__.__name__}: {exc}"],
            }
        out.append(s)
        _log.info(
            "stripe_sync_workspace_done workspace=%s outcome=%s posted=%d queued=%d flagged=%d skipped=%d errors=%d",
            wid,
            s.get("last_outcome"),
            s.get("posted", 0),
            s.get("queued", 0),
            s.get("flagged_for_cpa", 0),
            s.get("skipped", 0),
            len(s.get("errors") or []),
        )
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _main_async(args: argparse.Namespace) -> int:
    workspace_filter: UUID | None = None
    if args.workspace:
        try:
            workspace_filter = UUID(args.workspace)
        except ValueError:
            _log.error("--workspace must be a UUID: %s", args.workspace)
            return 2
    if not _ingestion_enabled():
        _log.warning(
            "stripe_sync_disabled — discovery works but ingestion is OFF. Set "
            "STRIPE_SYNC_ENABLED=1 to turn it on; see the module docstring for "
            "why it ships off."
        )
        if not args.loop:
            return 0
        # Under --loop the process is supervised by a restart policy, so
        # returning here would exit, get restarted, log, exit ... — a crash
        # loop that spams the log every couple of seconds and reads as a
        # broken container. Idle quietly instead, re-checking each cycle so
        # flipping the env var and restarting is all it takes to enable.
        while not _ingestion_enabled():
            await asyncio.sleep(max(60, args.interval_s))
    if not args.loop:
        results = await _sync_all_workspaces(workspace_filter)
        if not results:
            _log.info("stripe_sync_no_workspaces_with_credential")
        return 0
    interval = max(60, args.interval_s)
    _log.info("stripe_sync_loop_starting interval_s=%d", interval)
    while True:
        try:
            await _sync_all_workspaces(workspace_filter)
        except Exception:
            _log.exception("stripe_sync_loop_failure (will retry)")
        await asyncio.sleep(interval)



def _ingestion_enabled() -> bool:
    """Whether this worker may write to the ledger.

    Ships OFF. The discovery bug above meant this worker had never posted
    anything, so switching it on is a first-time ingestion into live books, not
    a resumption — and for eBay specifically there is a live conflict: the
    Listing-Ops app already syncs eBay sales into the
    magic-unicorn-inc-listing-ops entity, while _entity_for_credential here
    resolves to the OLDEST active entity (magic-unicorn-inc). Same economic
    activity, two entities, and different `source` labels so the
    (entity, source, source_external_id) idempotency key cannot dedupe them:
    consolidated revenue would be counted twice.

    Deciding which system owns eBay-to-ledger is an accounting call, not a
    code one. Until it is made, discovery being fixed must not imply posting.
    """
    return os.getenv("STRIPE_SYNC_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def main() -> None:
    p = argparse.ArgumentParser(description="Stripe API → journal sync worker")
    p.add_argument("--loop", action="store_true", help="Run forever with a sleep loop")
    p.add_argument(
        "--interval-s",
        type=int,
        default=DEFAULT_LOOP_INTERVAL_S,
        help=f"Sleep between passes in --loop mode (default {DEFAULT_LOOP_INTERVAL_S}s)",
    )
    p.add_argument(
        "--workspace",
        type=str,
        default=os.getenv("STRIPE_SYNC_WORKSPACE"),
        help="Restrict to one workspace UUID (default: env STRIPE_SYNC_WORKSPACE or all)",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
