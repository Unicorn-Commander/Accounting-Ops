"""Real-time transactions list from SimpleFIN.

SimpleFIN credentials come ONLY from the per-workspace encrypted store
(public.tenant_credentials) — no environment-variable fallback (audit finding
C1). A workspace with no stored credential simply yields no SimpleFIN rows
(and `configured: false` if no other source is wired); it NEVER returns
another workspace's transactions.

Returns a flattened, sorted transaction list across all accounts visible
through the workspace's SimpleFIN connection. Each transaction is annotated
with the entity it maps to (account_entity_mapping) when one exists.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.category_overrides import load_overrides_for_workspace
from app.authz import assert_entity_in_scope
from app.categorization import categorize_transaction
from app.clients import ebay_finances
from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext
from app.tenancy import WorkspaceContext, require_role, resolve_workspace

_log = logging.getLogger(__name__)

router = APIRouter(tags=["transactions"])


async def _simplefin_access_url(workspace: WorkspaceContext) -> str | None:
    """Per-workspace encrypted SimpleFIN credential, or None if unconfigured.

    Credential comes ONLY from public.tenant_credentials — no environment-
    variable fallback (audit finding C1). Returns None when this workspace has
    no stored SimpleFIN credential; the endpoint then surfaces no SimpleFIN
    data (eBay, if configured, is additive and handled separately).
    """
    async with SessionLocal() as session:
        async with session.begin():
            return await get_credential_plaintext(
                session,
                workspace.workspace_id,
                "simplefin",
                requesting_service="transactions",
                actor_uc_uid=workspace.user_uc_uid,
                actor_agent_id=workspace.agent_id,
            )


async def _fetch_simplefin_window(
    access_url: str, days: int = 30, include_pending: bool = True
) -> dict:
    parts = urlsplit(access_url)
    username = parts.username or ""
    password = parts.password or ""
    host = parts.hostname
    if not host:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SimpleFIN access URL malformed",
        )
    path = parts.path.rstrip("/")
    if not path.endswith("/accounts"):
        path = f"{path}/accounts"
    start_ts = int(
        (datetime.now(tz=UTC) - timedelta(days=days)).timestamp()
    )
    target = (
        f"{parts.scheme}://{host}{path}"
        f"?start-date={start_ts}&pending={'1' if include_pending else '0'}"
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(target, auth=(username, password))
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"SimpleFIN returned HTTP {resp.status_code}",
            )
        return resp.json()


def _money(value: str | int | float | None) -> str:
    if value is None:
        return "0.00"
    return str(value)


@router.get("/api/workspaces/{workspace_id}/recent-transactions")
async def workspace_recent_transactions(
    workspace_id: UUID,
    days: int = 30,
    limit: int = 50,
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Return the most recent transactions across all the user's SimpleFIN accounts.

    Sorted newest-first by posted timestamp. Surfaces both pending and posted.
    Each transaction is annotated with `entity_id` and `entity_name` if an
    account_entity_mapping exists for its source account.

    If `entity_id` query param is provided, only transactions from accounts
    mapped to that entity are returned. Pass entity_id=unmapped to filter to
    only accounts with no mapping.

    Entity-scope (BOLA): a scoped member requesting a specific out-of-scope
    entity gets 403; on a consolidated/unmapped request a scoped member is
    RESTRICTED to transactions mapped to their allowed entities (unmapped
    transactions are excluded). Unscoped members are unaffected.
    """
    from app.api.account_mappings import get_account_to_entity_map

    require_role(workspace, "viewer")
    scope_probe = None if entity_id == "unmapped" else entity_id
    scope_restriction = assert_entity_in_scope(workspace, scope_probe)
    scope_ids = (
        {str(e) for e in scope_restriction} if scope_restriction is not None else None
    )

    access_url = await _simplefin_access_url(workspace)
    account_map = await get_account_to_entity_map(workspace.workspace_id)
    overrides = await load_overrides_for_workspace(workspace.workspace_id)

    # SimpleFIN (BofA, USAA, ...) — primary source
    payload: dict = {"accounts": [], "errors": []}
    if access_url:
        payload = await _fetch_simplefin_window(access_url, days=days, include_pending=True)
    elif not access_url:
        # No SimpleFIN, but eBay may still be configured — fall through.
        pass

    transactions: list[dict] = []
    for account in payload.get("accounts", []):
        account_id = account.get("id")
        account_name = account.get("name", "")
        org = account.get("org") or {}
        org_name = org.get("name") or org.get("domain") or "—"
        currency = account.get("currency", "USD")
        mapping_key = f"simplefin:{account_id}"
        mapping = account_map.get(mapping_key)
        for txn in account.get("transactions", []):
            posted = txn.get("posted")
            transacted_at = txn.get("transacted_at")
            amount_str = _money(txn.get("amount"))
            try:
                amount_f = float(amount_str)
            except (TypeError, ValueError):
                amount_f = 0.0
            suggestion = categorize_transaction(
                payee=txn.get("payee"),
                description=txn.get("description"),
                memo=txn.get("memo"),
                amount=amount_f,
                overrides=overrides,
            )
            transactions.append(
                {
                    "id": txn.get("id"),
                    "account_id": account_id,
                    "account_name": account_name,
                    "org_name": org_name,
                    "currency": currency,
                    "posted_at": datetime.fromtimestamp(posted, tz=UTC).isoformat()
                    if posted
                    else None,
                    "posted_ts": posted,
                    "transacted_at": datetime.fromtimestamp(transacted_at, tz=UTC).isoformat()
                    if transacted_at
                    else None,
                    "amount": amount_str,
                    "payee": txn.get("payee"),
                    "description": txn.get("description"),
                    "memo": txn.get("memo"),
                    "is_pending": bool(txn.get("pending", False)),
                    "entity_id": mapping["entity_id"] if mapping else None,
                    "entity_display_name": mapping["entity_display_name"] if mapping else None,
                    "entity_slug": mapping["entity_slug"] if mapping else None,
                    "category": suggestion.category,
                    "category_status": suggestion.status,
                    "category_source": suggestion.source,
                    "category_kind": suggestion.kind,
                    "category_sign_matched": suggestion.sign_matched,
                }
            )
    # eBay Finances (Listing-Ops revenue) — secondary source, additive.
    ebay_errors: list[str] = []
    ebay_auth = await ebay_finances.load_ebay_auth(
        workspace.workspace_id,
        requesting_service="transactions",
        actor_uc_uid=workspace.user_uc_uid,
    )
    if ebay_auth is not None:
        # Try Finances API first (gives net payouts); fall back to Fulfillment
        # (gross order revenue) if Finances is gated.
        try:
            ebay_txns = await ebay_finances.recent_transactions(
                ebay_auth, days=days, limit=min(limit * 4, 500)
            )
        except ebay_finances.EbayFinancesError as exc:
            ebay_errors.append(f"finances: {exc}")
            ebay_txns = []
        except Exception as exc:
            _log.warning("ebay_finances_fetch_failed: %s", exc)
            ebay_errors.append(f"finances-mcp unavailable: {exc.__class__.__name__}")
            ebay_txns = []
        # If Finances returned nothing (gated or empty), try Fulfillment for
        # order-level revenue.
        if not ebay_txns:
            try:
                ebay_txns = await ebay_finances.fulfillment_orders(
                    ebay_auth, days=days, limit=min(limit * 4, 200)
                )
            except ebay_finances.EbayFinancesError as exc:
                ebay_errors.append(f"fulfillment: {exc}")
                ebay_txns = []
            except Exception as exc:
                _log.warning("ebay_fulfillment_fetch_failed: %s", exc)
                ebay_errors.append(f"fulfillment unavailable: {exc.__class__.__name__}")
                ebay_txns = []
        for txn in ebay_txns:
            ext_id = txn.get("account_id") or ""
            mapping_key = f"ebay:{ext_id}"
            mapping = account_map.get(mapping_key)
            amount_str = txn.get("amount", "0")
            try:
                amount_f = float(amount_str)
            except (TypeError, ValueError):
                amount_f = 0.0
            suggestion = categorize_transaction(
                payee=txn.get("payee"),
                description=txn.get("description"),
                memo=txn.get("memo"),
                amount=amount_f,
                overrides=overrides,
            )
            transactions.append(
                {
                    "id": txn.get("id"),
                    "account_id": ext_id,
                    "account_name": txn.get("account_name", "eBay payout"),
                    "org_name": txn.get("org_name", "eBay Finances"),
                    "currency": txn.get("currency", "USD"),
                    "posted_at": txn.get("posted_at"),
                    "posted_ts": txn.get("posted_ts"),
                    "transacted_at": txn.get("transacted_at"),
                    "amount": amount_str,
                    "payee": txn.get("payee"),
                    "description": txn.get("description"),
                    "memo": txn.get("memo"),
                    "is_pending": bool(txn.get("is_pending", False)),
                    "entity_id": mapping["entity_id"] if mapping else None,
                    "entity_display_name": mapping["entity_display_name"] if mapping else None,
                    "entity_slug": mapping["entity_slug"] if mapping else None,
                    "category": suggestion.category,
                    "category_status": suggestion.status,
                    "category_source": suggestion.source,
                    "category_kind": suggestion.kind,
                    "category_sign_matched": suggestion.sign_matched,
                }
            )

    # Entity-scope restriction (scoped members only): keep only transactions
    # whose mapped entity is in the member's allowed set. Unmapped transactions
    # (entity_id is None) are hidden from scoped members.
    if scope_ids is not None:
        transactions = [
            t for t in transactions if t["entity_id"] in scope_ids
        ]
    # Entity filter
    if entity_id == "unmapped":
        transactions = [t for t in transactions if t["entity_id"] is None]
    elif entity_id and entity_id != "consolidated":
        transactions = [t for t in transactions if t["entity_id"] == entity_id]
    transactions.sort(key=lambda t: t.get("posted_ts") or 0, reverse=True)
    sources = []
    if access_url:
        sources.append("simplefin")
    if ebay_auth is not None:
        sources.append("ebay_finances")
    return {
        "configured": bool(sources),
        "source": "+".join(sources) or "none",
        "days_window": days,
        "total_count": len(transactions),
        "transactions": transactions[:limit],
        "errors": (payload.get("errors") or []) + ebay_errors,
    }
