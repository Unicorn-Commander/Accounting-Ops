"""Real-time cash position from SimpleFIN.

Interim Phase 1.5 wiring: credentials come from environment variables, scoped
per-user (e.g., SIMPLEFIN_ACCESS_URL_AARON). Phase 1.6 will move credentials
into encrypted-at-rest storage in public.connector_credentials with a
KMS-derived per-tenant key, and lookups will join through connector_state.

For now this proves the live-data path works end-to-end:
  /dashboard → /api/workspaces/{wid}/cash-position → SimpleFIN bridge → bank
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.tenancy import WorkspaceContext, resolve_workspace

router = APIRouter(tags=["cash-position"])


def _access_url_for_user(uc_uid: str) -> str | None:
    """Look up the SimpleFIN access URL for this user.

    Interim: env var keyed by uc_uid (uppercased). Phase 1.6 moves to DB.
    """
    return os.getenv(f"SIMPLEFIN_ACCESS_URL_{uc_uid.upper()}") or os.getenv(
        "SIMPLEFIN_ACCESS_URL_AARON"  # session-default fallback for the demo
    )


async def _fetch_simplefin_accounts(access_url: str) -> dict:
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
    target = f"{parts.scheme}://{host}{path}?balances-only=1"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(target, auth=(username, password))
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"SimpleFIN returned HTTP {resp.status_code}",
            )
        return resp.json()


@router.get("/api/workspaces/{workspace_id}/cash-position")
async def workspace_cash_position(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Return real-time account balances for this workspace.

    Aggregates accounts across all SimpleFIN connections wired into the user's
    env credentials. Each account has: id, name, org_name, currency, balance,
    available_balance, last_balance_as_of.
    """
    access_url = _access_url_for_user(workspace.user_uc_uid)
    if not access_url:
        return {
            "total_usd": "0.00",
            "currency": "USD",
            "accounts": [],
            "configured": False,
            "message": "No SimpleFIN connection for this user yet. Phase 1.6 wires the Connect UI.",
        }

    payload = await _fetch_simplefin_accounts(access_url)
    accounts = []
    total = 0.0
    for a in payload.get("accounts", []):
        org_data = a.get("org") or {}
        org_name = org_data.get("name") or org_data.get("domain") or "—"
        try:
            balance_f = float(a.get("balance", 0))
        except (TypeError, ValueError):
            balance_f = 0.0
        if a.get("currency", "USD").upper() == "USD":
            total += balance_f
        accounts.append(
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "org_name": org_name,
                "currency": a.get("currency", "USD"),
                "balance": str(a.get("balance", "0")),
                "available_balance": str(a.get("available-balance"))
                if a.get("available-balance") is not None
                else None,
                "balance_as_of": a.get("balance-date"),
                "last4": (a.get("name") or "").rsplit("(", 1)[-1].rstrip(")")
                if "(" in (a.get("name") or "")
                else None,
            }
        )
    return {
        "total_usd": f"{total:.2f}",
        "currency": "USD",
        "accounts": accounts,
        "configured": True,
        "source": "simplefin",
        "errors": payload.get("errors", []),
    }
