"""eBay Finances → journal sync worker.

Run modes
~~~~~~~~~
* ``python scripts/ebay_sync.py``           — single pass; sync every workspace
                                              with an ebay_finances credential.
* ``python scripts/ebay_sync.py --loop``    — long-running loop with a short
                                              sleep between passes (default 3600s
                                              = hourly), used by the compose
                                              ``ebay-sync`` service entrypoint.
* ``python scripts/ebay_sync.py --workspace <uuid>`` — sync only one workspace.

Per pass, for each workspace:
  1. Load + decrypt the ebay_finances credential blob (via the same
     CredCrypto envelope the rest of the app uses).
  2. Open an eBay client (auto-refresh access token if expired).
  3. Pull payouts in a fixed window whose bounds come from eBay timestamps;
     the first window covers the client's 30-day backfill.
  4. For each payout: pull its associated /transaction rows.
  5. Pass every txn (+ the synthetic PAYOUT entry) through
     ``app.connectors.ebay.mapper.map_and_post_transaction`` — the first 30
     rows queue for human review, then auto-post by materiality.
  6. Merge the eBay-owned cursor and outcome keys into connector_state under
     its row lock.

Safety
~~~~~~
* Each page continuation and item failure is durable in connector_state.state;
  last_sync_at advances only after both pulls and all mappings are complete.
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
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.connectors.ebay import EbayApiError, EbayFinancesClient, load_ebay_credential
from app.connectors.ebay.decision_state import item_decision
from app.connectors.ebay.mapper import (
    map_and_post_transaction,
    normalize_payout_as_txn,
)
from app.connectors.ebay.reconcile import reconcile_one_entity
from app.db import SessionLocal
from app.workspace_session import workspace_session


_log = logging.getLogger("acctops.ebay_sync")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)

DEFAULT_LOOP_INTERVAL_S = int(os.getenv("EBAY_SYNC_INTERVAL_S", "3600"))
EBAY_WINDOW_OVERLAP = timedelta(days=1)

_ADVANCE_MAPPER_OUTCOMES = frozenset({
    "posted", "blocked_idempotent", "skipped_zero_amount", "human_rejected",
})
_REVIEW_MAPPER_OUTCOMES = frozenset({
    "queued_for_confirm", "queued_first_run", "blocked_materiality",
})


def _utcnow() -> datetime:
    """Return the run clock; tests replace this for deterministic cursors."""
    return datetime.now(tz=timezone.utc)


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
                "ebay_sync: neither ADMIN_DATABASE_URL nor DATABASE_URL is set"
            )
        _ADMIN_SESSIONMAKER = async_sessionmaker(
            create_async_engine(url, pool_pre_ping=True),
            expire_on_commit=False,
        )
    return _ADMIN_SESSIONMAKER


async def _workspaces_with_ebay() -> list[dict[str, Any]]:
    """Find every workspace that has at least one ebay_finances credential."""
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
                        WHERE tc.connector = 'ebay_finances'
                          AND w.status = 'active'
                        ORDER BY w.display_name
                        """
                    )
                )
            ).mappings().all()
    return [dict(r) for r in rows]


async def _entity_for_credential(workspace_id: UUID, entity_id_hint: str | None) -> UUID | None:
    """Pick the entity to attribute eBay revenue to.

    Priority:
      1. The entity_id encoded in the credential blob (set during OAuth callback).
      2. Connector_state row with connector_name='ebay_finances' (any entity).
      3. The single MU-Inc-like entity in the workspace (kind='c_corp').
      4. None — caller should skip.
    """
    if entity_id_hint:
        try:
            return UUID(entity_id_hint)
        except ValueError:
            pass
    async with workspace_session(SessionLocal, workspace_id) as session:
        # Try existing connector_state
        r = (
            await session.execute(
                text(
                    """
                    SELECT entity_id::text FROM connector_state
                    WHERE connector_name = 'ebay_finances'
                    ORDER BY last_sync_at DESC NULLS LAST
                    LIMIT 1
                    """
                )
            )
        ).first()
        if r:
            return UUID(r.entity_id)
        # Single c_corp entity heuristic
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


async def _load_sync_position(
    workspace_id: UUID, entity_id: UUID
) -> tuple[datetime | None, dict[str, Any] | None]:
    """Load the durable eBay chain position and its failure state."""
    async with workspace_session(SessionLocal, workspace_id) as session:
        r = (
            await session.execute(
                text(
                    """
                    SELECT last_sync_at, state FROM connector_state
                    WHERE entity_id = :eid AND connector_name = 'ebay_finances'
                    LIMIT 1
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).first()
    if r is None:
        return None, None
    state = r.state if isinstance(r.state, dict) else {}
    continuation = state.get("_ebay_continuation_cursor")
    if not isinstance(continuation, dict):
        continuation = None
    counts = state.get("_ebay_failure_counts")
    if not isinstance(counts, dict):
        counts = {}
    failure_counts = {
        str(key): int(value)
        for key, value in counts.items()
        if isinstance(key, str)
        and isinstance(value, (int, str))
        and not isinstance(value, bool)
        and str(value).isdigit()
        and int(value) > 0
    }
    blocked_item = state.get("ebay_blocked_item")
    if not isinstance(blocked_item, dict) or not isinstance(blocked_item.get("id"), str):
        blocked_item = None
    return r.last_sync_at if r.last_sync_at else None, {
        "cursor": continuation,
        "failure_counts": failure_counts,
        "blocked_item": blocked_item,
    }


async def _record_sync_outcome(
    workspace_id: UUID,
    entity_id: UUID,
    *,
    outcome_summary: dict[str, Any],
    last_sync_at: datetime | None,
    continuation_cursor: dict[str, Any] | None,
    failure_counts: dict[str, int] | None = None,
    blocked_item: dict[str, Any] | None = None,
    clear_blocked_item: bool = False,
    instance_name: str = "default",
) -> None:
    """Merge this worker's keys under the shared eBay state-row lock."""
    patch = dict(outcome_summary)
    drop: set[str] = set()
    if continuation_cursor is None:
        drop.add("_ebay_continuation_cursor")
    else:
        patch["_ebay_continuation_cursor"] = continuation_cursor
    if failure_counts:
        patch["_ebay_failure_counts"] = failure_counts
    else:
        drop.add("_ebay_failure_counts")
    if blocked_item is not None:
        patch["ebay_blocked_item"] = blocked_item
    elif clear_blocked_item:
        drop.add("ebay_blocked_item")
    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, state FROM connector_state
                    WHERE entity_id = :eid AND connector_name = 'ebay_finances'
                      AND connector_instance_name = :instance
                    FOR UPDATE
                    """
                ),
                {"eid": str(entity_id), "instance": instance_name},
            )
        ).first()
        if row is not None:
            await session.execute(
                text(
                    """
                    UPDATE connector_state
                    SET last_sync_at = :last_sync_at,
                        state = (COALESCE(state, '{}'::jsonb)
                                 - CAST(:drop_keys AS text[]))
                                || CAST(:patch AS jsonb)
                    WHERE id = :id
                    """
                ),
                {
                    "id": row.id,
                    "last_sync_at": last_sync_at,
                    "drop_keys": sorted(drop - set(patch)),
                    "patch": json.dumps(patch),
                },
            )
            return
        await session.execute(
            text(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                ) VALUES (
                    :id, :wid, :eid, 'ebay_finances', :instance,
                    CAST(:patch AS jsonb), :last_sync_at
                )
                ON CONFLICT (entity_id, connector_name, connector_instance_name)
                DO UPDATE SET
                    last_sync_at = EXCLUDED.last_sync_at,
                    state = (COALESCE(connector_state.state, '{}'::jsonb)
                             - CAST(:drop_keys AS text[]))
                            || EXCLUDED.state
                """
            ),
            {
                "id": str(uuid4()),
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "instance": instance_name,
                "patch": json.dumps(patch),
                "drop_keys": sorted(drop - set(patch)),
                "last_sync_at": last_sync_at,
            },
        )


# ---------------------------------------------------------------------------
# Per-workspace sync
# ---------------------------------------------------------------------------


def _iso_datetime(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _continuation_for_window(
    existing: dict[str, Any] | None,
    *,
    start_date: datetime | None,
    end_date: datetime | None,
) -> dict[str, Any]:
    """Keep a continuation only when it belongs to this exact date window."""
    if (
        isinstance(existing, dict)
        and existing.get("start_date") == _iso_datetime(start_date)
        and existing.get("end_date") == _iso_datetime(end_date)
    ):
        cursor = dict(existing)
        cursor["payouts"] = dict(cursor.get("payouts") or {"offset": 0})
        cursor["transactions"] = dict(cursor.get("transactions") or {})
        return cursor
    return {
        "start_date": _iso_datetime(start_date),
        "end_date": _iso_datetime(end_date),
        "payouts": {"offset": 0},
        "transactions": {},
    }


def _position_parts(
    loaded: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, int], dict[str, Any] | None]:
    """Accept both the current loaded-state shape and old test doubles."""
    if isinstance(loaded, dict) and {
        "cursor", "failure_counts", "blocked_item"
    } & set(loaded):
        cursor = loaded.get("cursor")
        return (
            cursor if isinstance(cursor, dict) else None,
            dict(loaded.get("failure_counts") or {}),
            loaded.get("blocked_item")
            if isinstance(loaded.get("blocked_item"), dict)
            else None,
        )
    return loaded, {}, None


def _offset(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _txn_id(txn: dict[str, Any]) -> str:
    return str(txn.get("transactionId") or txn.get("payoutId") or "")


async def _journal_entry_exists(
    session: Any, *, entity_id: UUID, external_id: str
) -> bool:
    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM journal_entry
                WHERE entity_id = :eid AND source = 'ebay_finances'
                  AND source_external_id = :ext
                LIMIT 1
                """
            ),
            {"eid": str(entity_id), "ext": external_id},
        )
    ).first()
    return row is not None


async def _enqueue_blocked_transaction(
    session: Any,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    txn: dict[str, Any],
    error: str,
) -> None:
    """Create one visible approval for a poison eBay item."""
    external_id = _txn_id(txn)
    existing = (
        await session.execute(
            text(
                """
                SELECT 1 FROM approval_queue
                WHERE entity_id = :eid AND status = 'pending'
                  AND subject_type = 'ebay_post_transaction'
                  AND payload ->> 'ebay_transaction_id' = :ext
                LIMIT 1
                """
            ),
            {"eid": str(entity_id), "ext": external_id},
        )
    ).first()
    if existing is not None:
        return
    amount = txn.get("amount") or txn.get("totalAmount") or {}
    amount_value = amount.get("value", "0") if isinstance(amount, dict) else amount
    payload = {
        "action": "ebay_post_transaction",
        "ebay_transaction_id": external_id,
        "ebay_type": txn.get("transactionType") or txn.get("_type") or "?",
        "raw_txn_keys": sorted(key for key in txn if key != "buyer"),
        "blocked": True,
    }
    await session.execute(
        text(
            """
            INSERT INTO approval_queue (
                id, workspace_id, entity_id, subject_type, subject_id,
                amount, currency_code, usd_equivalent_at_event_time,
                status, reasoning, requested_by_identity, payload
            ) VALUES (
                :id, :wid, :eid, 'ebay_post_transaction', :subject,
                :amount, 'USD', :amount, 'pending', :reasoning,
                'agent:ebay_sync', CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": str(uuid4()),
            "wid": str(workspace_id),
            "eid": str(entity_id),
            "subject": str(uuid4()),
            "amount": amount_value,
            "reasoning": f"(blocked) eBay {external_id}: {error}",
            "payload": json.dumps(payload),
        },
    )


async def _sync_one_workspace(workspace_id: UUID) -> dict[str, Any]:
    """Pull and post one fixed eBay window without skipping a refusal."""
    summary: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "started_at": _utcnow().isoformat(),
        "payouts_seen": 0,
        "transactions_seen": 0,
        "posted": 0,
        "queued": 0,
        "skipped": 0,
        "rejected": 0,
        "rejected_items": [],
        "errors": [],
    }

    cred = await load_ebay_credential(
        workspace_id,
        requesting_service="ebay_sync",
        actor_uc_uid="agent:ebay_sync",
    )
    if cred is None:
        summary["last_outcome"] = "no_credential"
        return summary
    entity_id = await _entity_for_credential(workspace_id, cred.entity_id)
    if entity_id is None:
        summary["last_outcome"] = "no_entity"
        summary["errors"].append("No active entity to attribute eBay revenue to.")
        return summary

    loaded_last_sync, loaded = await _load_sync_position(workspace_id, entity_id)
    stored_cursor, failure_counts, blocked_item = _position_parts(loaded)
    stored_start = _parse_datetime((stored_cursor or {}).get("start_date"))
    stored_end = _parse_datetime((stored_cursor or {}).get("end_date"))
    if stored_cursor and stored_start is not None:
        start_date = stored_start
        end_date = stored_end
    else:
        # The first request is intentionally unbounded. The client derives
        # both bounds from eBay timestamps and pins them in this chain.
        start_date = (
            loaded_last_sync - EBAY_WINDOW_OVERLAP
            if loaded_last_sync
            else None
        )
        end_date = None
    cursor = _continuation_for_window(
        stored_cursor,
        start_date=start_date,
        end_date=end_date,
    )
    payout_offset = _offset((cursor.get("payouts") or {}).get("offset"))
    transaction_cursors = cursor.setdefault("transactions", {})
    client = EbayFinancesClient.from_credential(cred)

    def set_window(result: Any) -> None:
        nonlocal start_date, end_date
        if start_date is None and result.window_start is not None:
            start_date = result.window_start
            cursor["start_date"] = _iso_datetime(start_date)
        if end_date is None and result.window_end is not None:
            end_date = result.window_end
            cursor["end_date"] = _iso_datetime(end_date)

    try:
        payouts = await client.list_payouts(
            start_date=start_date,
            end_date=end_date,
            limit=200,
            offset=payout_offset,
        )
        if payouts.continuation_invalid:
            cursor["payouts"]["offset"] = 0
            payout_offset = 0
            payouts = await client.list_payouts(
                start_date=start_date,
                end_date=end_date,
                limit=200,
                offset=0,
            )
    except EbayApiError as exc:
        summary["last_outcome"] = "api_error_payouts"
        summary["errors"].append(f"list_payouts: {exc}")
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=loaded_last_sync,
            continuation_cursor=cursor,
            failure_counts=failure_counts,
            blocked_item=blocked_item,
        )
        return summary
    set_window(payouts)
    summary["payouts_seen"] = len(payouts)
    if start_date is not None and end_date is not None:
        cursor["start_date"] = _iso_datetime(start_date)
        cursor["end_date"] = _iso_datetime(end_date)
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=loaded_last_sync,
            continuation_cursor=cursor,
            failure_counts=failure_counts,
            blocked_item=blocked_item,
        )

    pending_payouts = {
        payout_id: state.get("payout")
        for payout_id, state in transaction_cursors.items()
        if payout_id != "__loose__"
        and isinstance(state, dict)
        and isinstance(state.get("payout"), dict)
    }
    payouts_to_process = list(pending_payouts.values())
    seen_payout_ids = {payout.get("payoutId") for payout in payouts_to_process}
    payouts_to_process.extend(
        payout for payout in payouts
        if payout.get("payoutId") not in seen_payout_ids
    )

    processing_error = False
    awaiting_review = False
    blocked_now: dict[str, Any] | None = None
    blocked_resolved = False

    async def process_item(
        session: Any,
        txn: dict[str, Any],
        *,
        decision: str | None = None,
    ) -> str:
        """Return advance, await, fail, or block for one mapper outcome."""
        nonlocal blocked_now
        nonlocal blocked_resolved
        external_id = _txn_id(txn)
        if decision == "rejected":
            # A human rejection is an explicit, durable decision. It advances
            # this one item without posting and without creating another
            # pending approval on the next overlap read.
            failure_counts.pop(external_id, None)
            summary["rejected"] += 1
            summary["rejected_items"].append({
                "id": external_id,
                "decision": "rejected",
            })
            if blocked_item is not None and external_id == blocked_item.get("id"):
                blocked_resolved = True
            return "advance"
        try:
            async with session.begin_nested():
                result = await map_and_post_transaction(
                    session,
                    workspace_id=str(workspace_id),
                    entity_id=str(entity_id),
                    txn=txn,
                    actor_uc_uid="agent:ebay_sync",
                    actor_agent_id="ebay_sync_worker",
                    # This flag is derived from the locked connector state,
                    # never from approval payload fields. The mapper still
                    # derives accounts, dates, descriptions, and amounts from
                    # the raw eBay item returned by the API.
                    first_run_confirmed=decision == "approved",
                )
        except Exception as exc:
            error_text = f"{exc.__class__.__name__}: {exc}"
            failure_counts[external_id] = min(
                failure_counts.get(external_id, 0) + 1, 3
            )
            summary["errors"].append(f"map_and_post {external_id or '?'}: {error_text}")
            if failure_counts[external_id] < 3:
                return "fail"
            try:
                async with session.begin_nested():
                    await _enqueue_blocked_transaction(
                        session,
                        workspace_id=workspace_id,
                        entity_id=entity_id,
                        txn=txn,
                        error=error_text,
                    )
            except Exception as alert_exc:
                summary["errors"].append(
                    f"blocked alert {external_id}: "
                    f"{alert_exc.__class__.__name__}: {alert_exc}"
                )
            blocked_now = {
                "id": external_id,
                "amount": txn.get("amount"),
                "currency": (txn.get("amount") or {}).get("currency")
                if isinstance(txn.get("amount"), dict)
                else None,
                "error": error_text,
            }
            summary["blocked_item"] = blocked_now
            return "block"

        outcome = getattr(result, "outcome", "")
        reason = getattr(result, "reason", None) or "no reason supplied"
        if outcome == "blocked_idempotent":
            try:
                confirmed = await _journal_entry_exists(
                    session, entity_id=entity_id, external_id=external_id
                )
            except Exception as exc:
                confirmed = False
                reason = f"could not confirm duplicate: {exc.__class__.__name__}: {exc}"
            if not confirmed:
                outcome = "unconfirmed_idempotent"
        if outcome in _REVIEW_MAPPER_OUTCOMES:
            summary["queued"] += 1
            summary["awaiting_review_item"] = {
                "id": external_id,
                "amount": txn.get("amount"),
                "currency": (txn.get("amount") or {}).get("currency")
                if isinstance(txn.get("amount"), dict)
                else None,
            }
            summary["errors"].append(
                f"awaiting review {external_id or '?'}: {reason}"
            )
            return "await"
        if outcome not in _ADVANCE_MAPPER_OUTCOMES:
            failure_counts[external_id] = min(
                failure_counts.get(external_id, 0) + 1, 3
            )
            error_text = f"mapper refusal {outcome}: {reason}"
            summary["errors"].append(f"map_and_post {external_id or '?'}: {error_text}")
            if failure_counts[external_id] < 3:
                return "fail"
            try:
                async with session.begin_nested():
                    await _enqueue_blocked_transaction(
                        session,
                        workspace_id=workspace_id,
                        entity_id=entity_id,
                        txn=txn,
                        error=error_text,
                    )
            except Exception as alert_exc:
                summary["errors"].append(
                    f"blocked alert {external_id}: "
                    f"{alert_exc.__class__.__name__}: {alert_exc}"
                )
            blocked_now = {
                "id": external_id,
                "amount": txn.get("amount"),
                "currency": (txn.get("amount") or {}).get("currency")
                if isinstance(txn.get("amount"), dict)
                else None,
                "error": error_text,
            }
            summary["blocked_item"] = blocked_now
            return "block"
        failure_counts.pop(external_id, None)
        if blocked_item is not None and external_id == blocked_item.get("id"):
            blocked_resolved = True
        if outcome == "posted":
            summary["posted"] += 1
            if getattr(result, "fee_journal_entry_id", None):
                summary["posted"] += 1
        elif outcome == "skipped_zero_amount":
            summary["skipped"] += 1
        return "advance"

    async def process_item_in_transaction(txn: dict[str, Any]) -> str:
        """Map one item and commit its journal work before the next item.

        The mapper receives the same session that owns this item's transaction
        and savepoint. Keeping the transaction per item means a PostgreSQL
        statement error can abort only this item; a later rollback cannot erase
        journal rows already committed for earlier items.
        """
        async with workspace_session(SessionLocal, workspace_id) as item_session:
            row = (
                await item_session.execute(
                    text(
                        """
                        SELECT id, state FROM connector_state
                        WHERE entity_id = :eid AND connector_name = 'ebay_finances'
                          AND connector_instance_name = 'default'
                        FOR UPDATE
                        """
                    ),
                    {"eid": str(entity_id)},
                )
            ).first()
            state = getattr(row, "state", None) if row is not None else None
            decision = item_decision(
                state if isinstance(state, dict) else {}, _txn_id(txn)
            )
            return await process_item(item_session, txn, decision=decision)

    payout_page_offset = payout_offset
    for index, payout in enumerate(payouts_to_process):
        payout_id = payout.get("payoutId") or ""
        state = transaction_cursors.get(payout_id) or {}
        transaction_offset = _offset(state.get("offset"))
        payout_page_offset_for_state = _offset(
            state.get("payout_page_offset"), payout_page_offset
        )
        payout_index = _offset(state.get("payout_index"), index)
        payout_posted = bool(state.get("payout_posted"))
        try:
            txns = await client.list_transactions(
                payout_id=payout_id,
                limit=500,
                offset=transaction_offset,
            )
            if txns.continuation_invalid:
                transaction_offset = 0
                txns = await client.list_transactions(
                    payout_id=payout_id,
                    limit=500,
                    offset=0,
                )
        except EbayApiError as exc:
            transaction_cursors[payout_id] = {
                "offset": transaction_offset,
                "payout": payout,
                "payout_page_offset": payout_page_offset_for_state,
                "payout_index": payout_index,
                "payout_posted": payout_posted,
            }
            summary["errors"].append(
                f"list_transactions payout={payout_id[:8] or '?'}: {exc}"
            )
            processing_error = True
            break
        summary["transactions_seen"] += len(txns)
        for txn_index, txn in enumerate(txns):
            transaction_cursors[payout_id] = {
                "offset": transaction_offset + txn_index,
                "payout": payout,
                "payout_page_offset": payout_page_offset_for_state,
                "payout_index": payout_index,
                "payout_posted": payout_posted,
            }
            status = await process_item_in_transaction(txn)
            if status != "advance":
                processing_error = status == "fail" or status == "block"
                awaiting_review = status == "await"
                break
        if processing_error or awaiting_review:
            break
        if txns.exhausted:
            transaction_cursors.pop(payout_id, None)
        else:
            transaction_cursors[payout_id] = {
                "offset": txns.continuation_offset,
                "payout": payout,
                "payout_page_offset": payout_page_offset_for_state,
                "payout_index": payout_index,
                "payout_posted": payout_posted,
            }
            continue
        if payout_posted:
            cursor["payouts"]["offset"] = max(
                _offset(cursor["payouts"].get("offset")),
                payout_page_offset_for_state + payout_index + 1,
            )
            continue
        payout_txn = normalize_payout_as_txn(payout)
        transaction_cursors[payout_id] = {
            "offset": 0,
            "payout": payout,
            "payout_page_offset": payout_page_offset_for_state,
            "payout_index": payout_index,
            "payout_posted": False,
        }
        status = await process_item_in_transaction(payout_txn)
        if status != "advance":
            processing_error = status in {"fail", "block"}
            awaiting_review = status == "await"
            break
        transaction_cursors.pop(payout_id, None)
        cursor["payouts"]["offset"] = max(
            _offset(cursor["payouts"].get("offset")),
            payout_page_offset_for_state + payout_index + 1,
        )

    if not processing_error and not awaiting_review:
        if payouts.exhausted:
            cursor["payouts"].pop("offset", None)
        else:
            cursor["payouts"]["offset"] = max(
                _offset(cursor["payouts"].get("offset")),
                _offset(payouts.continuation_offset),
            )

    if not processing_error and not awaiting_review:
        loose_state = transaction_cursors.get("__loose__") or {}
        loose_offset = _offset(loose_state.get("offset"))
        try:
            loose_txns = await client.list_transactions(
                start_date=start_date,
                end_date=end_date,
                limit=500,
                offset=loose_offset,
            )
            if loose_txns.continuation_invalid:
                loose_offset = 0
                loose_txns = await client.list_transactions(
                    start_date=start_date,
                    end_date=end_date,
                    limit=500,
                    offset=0,
                )
        except EbayApiError as exc:
            summary["errors"].append(f"list_transactions(loose): {exc}")
            transaction_cursors["__loose__"] = {"offset": loose_offset}
            processing_error = True
            loose_txns = None
        if loose_txns is not None:
            set_window(loose_txns)
            if start_date is not None and end_date is not None:
                cursor["start_date"] = _iso_datetime(start_date)
                cursor["end_date"] = _iso_datetime(end_date)
            summary["transactions_seen"] += len(loose_txns)
            for txn_index, txn in enumerate(loose_txns):
                transaction_cursors["__loose__"] = {
                    "offset": loose_offset + txn_index
                }
                status = await process_item_in_transaction(txn)
                if status != "advance":
                    processing_error = status in {"fail", "block"}
                    awaiting_review = status == "await"
                    break
            if not processing_error and not awaiting_review:
                if loose_txns.exhausted:
                    transaction_cursors.pop("__loose__", None)
                else:
                    transaction_cursors["__loose__"] = {
                        "offset": loose_txns.continuation_offset
                    }

    if blocked_now is not None:
        blocked_item = blocked_now
    if awaiting_review:
        summary["last_outcome"] = "awaiting_review"
    elif processing_error:
        summary["last_outcome"] = "blocked_item" if blocked_now else "processing_error"
    else:
        summary["last_outcome"] = "ok"

    complete = (
        not processing_error
        and not awaiting_review
        and not cursor.get("payouts")
        and not transaction_cursors
    )
    next_cursor = None if complete else cursor
    next_sync_at = end_date if complete and end_date is not None else loaded_last_sync
    blocked_to_write = None if blocked_resolved else blocked_item

    # Make the durable cursor visible before reconciliation. A reconciliation
    # outage must never erase a completed page or re-open a failed item.
    if not processing_error and not awaiting_review:
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=next_sync_at,
            continuation_cursor=next_cursor,
            failure_counts=failure_counts,
            blocked_item=blocked_to_write,
            clear_blocked_item=blocked_resolved,
        )
        try:
            async with workspace_session(SessionLocal, workspace_id) as reconcile_session:
                recon = await reconcile_one_entity(
                    reconcile_session,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                )
                summary["reconciliation"] = {
                    "checked": recon["checked_payouts"],
                    "matched": recon["matched"],
                    "unmatched": recon["unmatched"],
                }
        except Exception as exc:
            summary["errors"].append(
                f"reconcile: {exc.__class__.__name__}: {exc}"
            )
    summary["finished_at"] = _utcnow().isoformat()
    await _record_sync_outcome(
        workspace_id,
        entity_id,
        outcome_summary=summary,
        last_sync_at=next_sync_at,
        continuation_cursor=next_cursor,
        failure_counts=failure_counts,
        blocked_item=blocked_to_write,
        clear_blocked_item=blocked_resolved,
    )
    return summary


async def _sync_all_workspaces(workspace_filter: UUID | None = None) -> list[dict[str, Any]]:
    workspaces = await _workspaces_with_ebay()
    if not workspaces:
        # Loud on EVERY cycle, not just the one-shot path. An empty discovery
        # is indistinguishable from "nothing to do" in the logs, which is
        # exactly how this worker sat healthy and idle while a credential was
        # configured the whole time.
        _log.warning(
            "ebay_sync_no_workspaces_with_credential — discovery returned 0 "
            "workspaces. If a ebay credential IS configured, this is a fault, "
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
            _log.exception("ebay_sync_unhandled_failure workspace=%s", wid)
            s = {
                "workspace_id": str(wid),
                "last_outcome": "unhandled_exception",
                "errors": [f"{exc.__class__.__name__}: {exc}"],
            }
        out.append(s)
        _log.info(
            "ebay_sync_workspace_done workspace=%s outcome=%s posted=%d queued=%d skipped=%d errors=%d",
            wid,
            s.get("last_outcome"),
            s.get("posted", 0),
            s.get("queued", 0),
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
            "ebay_sync_disabled — discovery works but ingestion is OFF. Set "
            "EBAY_SYNC_ENABLED=1 to turn it on; see the module docstring for "
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
            _log.info("ebay_sync_no_workspaces_with_credential")
        return 0
    interval = max(60, args.interval_s)
    _log.info("ebay_sync_loop_starting interval_s=%d", interval)
    while True:
        try:
            await _sync_all_workspaces(workspace_filter)
        except Exception:
            _log.exception("ebay_sync_loop_failure (will retry)")
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
    return os.getenv("EBAY_SYNC_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def main() -> None:
    p = argparse.ArgumentParser(description="eBay Finances → journal sync worker")
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
        default=os.getenv("EBAY_SYNC_WORKSPACE"),
        help="Restrict to one workspace UUID (default: env EBAY_SYNC_WORKSPACE or all)",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(_main_async(args)))


if __name__ == "__main__":
    main()
