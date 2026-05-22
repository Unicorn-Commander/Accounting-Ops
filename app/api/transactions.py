"""Real-time transactions list from SimpleFIN.

Same interim Phase 1.5 wiring as cash_position: env var keyed by uc_uid.
Phase 1.6 moves to encrypted DB credentials.

Returns a flattened, sorted transaction list across all accounts visible
through the user's SimpleFIN connection. Entity-mapping (which account
belongs to which entity) is a future feature; for now we surface the
origin account name + org so the user can mentally map them.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext
from app.tenancy import WorkspaceContext, resolve_workspace

router = APIRouter(tags=["transactions"])


async def _simplefin_access_url(workspace: WorkspaceContext) -> str | None:
    """DB-first, env-var fallback (matches cash_position pattern)."""
    async with SessionLocal() as session:
        async with session.begin():
            try:
                plaintext = await get_credential_plaintext(
                    session,
                    workspace.workspace_id,
                    "simplefin",
                    requesting_service="transactions",
                    actor_uc_uid=workspace.user_uc_uid,
                    actor_agent_id=workspace.agent_id,
                )
                if plaintext:
                    return plaintext
            except Exception:
                pass
    return os.getenv(
        f"SIMPLEFIN_ACCESS_URL_{workspace.user_uc_uid.upper()}"
    ) or os.getenv("SIMPLEFIN_ACCESS_URL_AARON")


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
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Return the most recent transactions across all the user's SimpleFIN accounts.

    Sorted newest-first by posted timestamp. Surfaces both pending and posted.
    """
    access_url = await _simplefin_access_url(workspace)
    if not access_url:
        return {"configured": False, "transactions": [], "message": "No connection."}

    payload = await _fetch_simplefin_window(access_url, days=days, include_pending=True)
    transactions: list[dict] = []
    for account in payload.get("accounts", []):
        account_id = account.get("id")
        account_name = account.get("name", "")
        org = account.get("org") or {}
        org_name = org.get("name") or org.get("domain") or "—"
        currency = account.get("currency", "USD")
        for txn in account.get("transactions", []):
            posted = txn.get("posted")
            transacted_at = txn.get("transacted_at")
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
                    "amount": _money(txn.get("amount")),
                    "payee": txn.get("payee"),
                    "description": txn.get("description"),
                    "memo": txn.get("memo"),
                    "is_pending": bool(txn.get("pending", False)),
                }
            )
    transactions.sort(key=lambda t: t.get("posted_ts") or 0, reverse=True)
    return {
        "configured": True,
        "source": "simplefin",
        "days_window": days,
        "total_count": len(transactions),
        "transactions": transactions[:limit],
        "errors": payload.get("errors", []),
    }
