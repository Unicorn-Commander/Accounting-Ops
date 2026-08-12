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


async def _record_sync_outcome(
    workspace_id: UUID,
    entity_id: UUID,
    *,
    outcome_summary: dict[str, Any],
    instance_name: str = "default",
) -> None:
    """Upsert connector_state with the latest run's summary."""
    async with workspace_session(SessionLocal, workspace_id) as session:
        result = await session.execute(
            text(
                """
                UPDATE connector_state
                SET last_sync_at = now(), state = CAST(:state AS jsonb)
                WHERE entity_id = :eid AND connector_name = 'stripe_api'
                  AND connector_instance_name = :instance
                """
            ),
            {
                "eid": str(entity_id),
                "instance": instance_name,
                "state": json.dumps(outcome_summary),
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
                    :instance, CAST(:state AS jsonb), now()
                )
                ON CONFLICT (entity_id, connector_name, connector_instance_name) DO UPDATE
                SET last_sync_at = now(), state = EXCLUDED.state
                """
            ),
            {
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "instance": instance_name,
                "state": json.dumps(outcome_summary),
            },
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

    last_sync = await _load_last_sync(workspace_id, entity_id)
    start_date = (
        last_sync - timedelta(days=1)
        if last_sync
        else datetime.now(tz=timezone.utc) - timedelta(days=DEFAULT_BACKFILL_DAYS)
    )
    # Stripe filter is unix epoch seconds.
    created_gte = int(start_date.timestamp())

    client = StripeClient.from_credential(cred)

    # ---- 1. Pull balance_transactions ----
    try:
        bts = await paginate_balance_transactions(
            client, created_gte=created_gte, max_pages=50
        )
    except StripeApiError as exc:
        summary["last_outcome"] = "api_error_balance_transactions"
        summary["errors"].append(f"list_balance_transactions: {exc}")
        await _record_sync_outcome(workspace_id, entity_id, outcome_summary=summary)
        return summary
    summary["balance_transactions_seen"] = len(bts)
    _log.info(
        "stripe_sync workspace=%s entity=%s pulled %d balance_transactions since %s",
        workspace_id, entity_id, len(bts), start_date.isoformat(),
    )

    # ---- 2. Map + post each ----
    revenue_account = cred.default_revenue_account or DEFAULT_REVENUE_ACCOUNT
    async with workspace_session(SessionLocal, workspace_id) as session:
        for bt in bts:
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
                summary["errors"].append(
                    f"map_and_post {bt.get('id', '?')}: {exc.__class__.__name__}: {exc}"
                )
                continue
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

    summary["finished_at"] = datetime.now(tz=timezone.utc).isoformat()
    summary["last_outcome"] = "ok"
    await _record_sync_outcome(workspace_id, entity_id, outcome_summary=summary)
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
