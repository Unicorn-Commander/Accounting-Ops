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
from app.connectors.stripe.client import paginate_balance_transactions
from app.connectors.stripe.mapper import (
    DEFAULT_REVENUE_ACCOUNT,
    enqueue_quarantined_balance_transaction,
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


async def _load_sync_position(
    workspace_id: UUID, entity_id: UUID
) -> tuple[
    datetime | None,
    str | None,
    int | None,
    int | None,
    dict[str, int],
    dict[str, dict[str, Any]],
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
        return None, None, None, None, {}, {}
    state = r.state if isinstance(r.state, dict) else {}
    cursor = state.get("_stripe_continuation_cursor")

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
    if window_gte is None or window_lt is None:
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
    quarantined_items = state.get("_stripe_quarantined_items")
    if not isinstance(quarantined_items, dict):
        quarantined_items = {}
    quarantined_items = {
        str(key): value
        for key, value in quarantined_items.items()
        if isinstance(key, str) and isinstance(value, dict)
    }
    return (
        r.last_sync_at,
        cursor if isinstance(cursor, str) else None,
        window_gte,
        window_lt,
        failure_counts,
        quarantined_items,
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
    quarantined_items: dict[str, dict[str, Any]] | None = None,
    instance_name: str = "default",
) -> None:
    """Upsert connector_state with the latest run's summary."""
    state = dict(outcome_summary)
    if continuation_cursor is None:
        state.pop("_stripe_continuation_cursor", None)
    else:
        state["_stripe_continuation_cursor"] = continuation_cursor
    if window_gte is None or window_lt is None:
        state.pop("window_gte", None)
        state.pop("window_lt", None)
    else:
        state["window_gte"] = window_gte
        state["window_lt"] = window_lt
    if failure_counts:
        state["_stripe_failure_counts"] = failure_counts
    else:
        state.pop("_stripe_failure_counts", None)
    if quarantined_items:
        state["_stripe_quarantined_items"] = quarantined_items
    else:
        state.pop("_stripe_quarantined_items", None)
    async with workspace_session(SessionLocal, workspace_id) as session:
        result = await session.execute(
            text(
                """
                UPDATE connector_state
                SET last_sync_at = :last_sync_at, state = CAST(:state AS jsonb)
                WHERE entity_id = :eid AND connector_name = 'stripe_api'
                  AND connector_instance_name = :instance
                """
            ),
            {
                "eid": str(entity_id),
                "instance": instance_name,
                "state": json.dumps(state),
                "last_sync_at": last_sync_at,
            },
        )
        if (result.rowcount or 0) > 0:
            return
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
                SET last_sync_at = EXCLUDED.last_sync_at, state = EXCLUDED.state
                """
            ),
            {
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "instance": instance_name,
                "state": json.dumps(state),
                "last_sync_at": last_sync_at,
            },
        )


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
        "quarantined": 0,
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

    (
        last_sync,
        continuation_cursor,
        window_gte,
        window_lt,
        failure_counts,
        quarantined_items,
    ) = await _load_sync_position(workspace_id, entity_id)
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
            await _record_sync_outcome(
                workspace_id,
                entity_id,
                outcome_summary=summary,
                last_sync_at=last_sync,
                continuation_cursor=continuation_cursor,
                window_gte=window_gte if window_lt is not None else None,
                window_lt=window_lt,
                failure_counts=failure_counts,
                quarantined_items=quarantined_items,
            )
            return summary
    window_lt = bts.window_lt or window_lt
    if loaded_window_lt is None and window_lt is not None:
        # Establish the chain window before mapping. If the worker dies while
        # mapping, the retry still uses this exact Stripe-derived upper bound.
        await _record_sync_outcome(
            workspace_id,
            entity_id,
            outcome_summary=summary,
            last_sync_at=last_sync,
            continuation_cursor=continuation_cursor,
            window_gte=window_gte,
            window_lt=window_lt,
            failure_counts=failure_counts,
            quarantined_items=quarantined_items,
        )
    summary["balance_transactions_seen"] = len(bts)
    _log.info(
        "stripe_sync workspace=%s entity=%s pulled %d balance_transactions since %s",
        workspace_id, entity_id, len(bts), start_date.isoformat(),
    )

    # ---- 2. Map + post each ----
    revenue_account = cred.default_revenue_account or DEFAULT_REVENUE_ACCOUNT
    last_processed: dict[str, Any] | None = None
    processing_error = False
    async with workspace_session(SessionLocal, workspace_id) as session:
        for bt in bts:
            bt_id = bt.get("id")
            if isinstance(bt_id, str) and bt_id in quarantined_items:
                summary["quarantined"] += 1
                last_processed = bt
                continue
            try:
                # Keep every statement for one balance transaction inside its
                # own savepoint. A DBAPI error aborts the current PostgreSQL
                # transaction until that savepoint is rolled back; without
                # this boundary, earlier posts are lost when the outer
                # workspace transaction exits.
                async with session.begin_nested():
                    try:
                        result = await map_and_post_balance_transaction(
                            session,
                            workspace_id=str(workspace_id),
                            entity_id=str(entity_id),
                            txn=bt,
                            actor_uc_uid="agent:stripe_sync",
                            actor_agent_id="stripe_sync_worker",
                            revenue_account=revenue_account,
                        )
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
                            raise
                        try:
                            approval_id = await enqueue_quarantined_balance_transaction(
                                session,
                                workspace_id=str(workspace_id),
                                entity_id=str(entity_id),
                                txn=bt,
                                actor_uc_uid="agent:stripe_sync",
                                error=error_text,
                            )
                        except Exception as quarantine_exc:
                            summary["errors"].append(
                                "quarantine "
                                f"{bt_id}: {quarantine_exc.__class__.__name__}: "
                                f"{quarantine_exc}"
                            )
                            raise
                        quarantined_items[bt_id] = {
                            "approval_id": str(approval_id),
                            "error": error_text,
                        }
                        failure_counts.pop(bt_id, None)
                        summary["quarantined"] += 1
                        result = None
                        _log.warning(
                            "stripe_sync_quarantined workspace=%s entity=%s id=%s "
                            "after three consecutive failures",
                            workspace_id,
                            entity_id,
                            bt_id,
                        )
            except Exception:
                processing_error = True
                break
            if result is None:
                last_processed = bt
                continue
            last_processed = bt
            if isinstance(bt_id, str):
                failure_counts.pop(bt_id, None)
            if result.outcome == "posted":
                summary["posted"] += 1
                if result.fee_journal_entry_id:
                    summary["posted"] += 1  # sidecar fee entry counts too
            elif result.outcome in ("queued_for_confirm", "queued_first_run"):
                summary["queued"] += 1
            elif result.outcome == "flagged_for_cpa":
                summary["flagged_for_cpa"] += 1
            elif result.outcome == "blocked_idempotent":
                pass
            else:
                summary["skipped"] += 1

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
            quarantined_items=quarantined_items,
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
    await _record_sync_outcome(
        workspace_id,
        entity_id,
        outcome_summary=summary,
        last_sync_at=next_sync_at,
        continuation_cursor=next_cursor,
        window_gte=next_window_gte,
        window_lt=next_window_lt,
        failure_counts=failure_counts,
        quarantined_items=quarantined_items,
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
        quarantined_items=quarantined_items,
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
