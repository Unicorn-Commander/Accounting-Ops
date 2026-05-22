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

from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext
from app.tenancy import WorkspaceContext, resolve_workspace

router = APIRouter(tags=["cash-position"])


async def _simplefin_access_url(workspace: WorkspaceContext) -> str | None:
    """Resolve SimpleFIN access URL for this workspace.

    Phase 1.6 path: encrypted DB credential via public.tenant_credentials.
    Phase 1.5 fallback: env var (removed in 1.7 once everyone migrates).
    """
    async with SessionLocal() as session:
        async with session.begin():
            try:
                plaintext = await get_credential_plaintext(
                    session,
                    workspace.workspace_id,
                    "simplefin",
                    requesting_service="cash_position",
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
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Return real-time account balances for this workspace.

    Aggregates accounts across all SimpleFIN connections wired into the user's
    env credentials. Each account has: id, name, org_name, currency, balance,
    available_balance, last_balance_as_of.

    Optional `entity_id` filter:
      - omitted / "consolidated" → all accounts
      - specific UUID            → only accounts mapped to that entity
      - "unmapped"               → only accounts with no mapping
    """
    from app.api.account_mappings import get_account_to_entity_map

    access_url = await _simplefin_access_url(workspace)
    if not access_url:
        return {
            "total_usd": "0.00",
            "currency": "USD",
            "accounts": [],
            "configured": False,
            "message": "No SimpleFIN connection for this user yet. Phase 1.6 wires the Connect UI.",
        }

    payload = await _fetch_simplefin_accounts(access_url)
    account_map = await get_account_to_entity_map(workspace.workspace_id)

    # Pull the linked COA account_type for each external account so we can
    # split assets (cash, savings) from liabilities (credit cards) cleanly.
    from sqlalchemy import text as _sa_text

    from app.workspace_session import workspace_session as _ws

    coa_link: dict[str, dict] = {}
    async with _ws(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                _sa_text(
                    """
                    SELECT linked_external_source AS source,
                           linked_external_account_id AS ext_id,
                           account_number, account_type, canonical_form
                    FROM chart_of_account
                    WHERE linked_external_account_id IS NOT NULL
                    """
                )
            )
        ).mappings().all()
    for r in rows:
        coa_link[f"{r['source']}:{r['ext_id']}"] = dict(r)

    accounts = []
    # Three running totals: assets (positive), liabilities (positive,
    # but represents debt owed), net (assets - liabilities).
    assets_total = 0.0
    liabilities_total = 0.0
    per_entity_assets: dict[str, float] = {}
    per_entity_liab: dict[str, float] = {}

    for a in payload.get("accounts", []):
        org_data = a.get("org") or {}
        org_name = org_data.get("name") or org_data.get("domain") or "—"
        external_account_id = a.get("id")
        mapping_key = f"simplefin:{external_account_id}"
        mapping = account_map.get(mapping_key)
        coa = coa_link.get(mapping_key)
        # Apply entity filter
        if entity_id == "unmapped" and mapping is not None:
            continue
        if entity_id and entity_id not in ("consolidated", "unmapped"):
            if mapping is None or mapping["entity_id"] != entity_id:
                continue
        try:
            balance_f = float(a.get("balance", 0))
        except (TypeError, ValueError):
            balance_f = 0.0

        # Classify: liability if COA account_type='liability', else asset.
        account_type = (coa["account_type"] if coa else None) or "asset"
        is_liability = account_type == "liability"

        if a.get("currency", "USD").upper() == "USD":
            if is_liability:
                # Credit card balances are stored as negative numbers in
                # SimpleFIN (e.g. -17293.65 = $17,293.65 of debt). Flip to
                # positive for the liability total.
                liabilities_total += abs(balance_f)
                if mapping is not None:
                    per_entity_liab[mapping["entity_id"]] = (
                        per_entity_liab.get(mapping["entity_id"], 0.0) + abs(balance_f)
                    )
            else:
                assets_total += balance_f
                if mapping is not None:
                    per_entity_assets[mapping["entity_id"]] = (
                        per_entity_assets.get(mapping["entity_id"], 0.0) + balance_f
                    )

        accounts.append(
            {
                "id": external_account_id,
                "name": a.get("name"),
                "org_name": org_name,
                "currency": a.get("currency", "USD"),
                "balance": str(a.get("balance", "0")),
                "available_balance": str(a.get("available-balance"))
                if a.get("available-balance") is not None
                else None,
                "balance_as_of": a.get("balance-date"),
                "entity_id": mapping["entity_id"] if mapping else None,
                "entity_display_name": mapping["entity_display_name"] if mapping else None,
                "entity_slug": mapping["entity_slug"] if mapping else None,
                "account_type": account_type,
                "coa_canonical": coa["canonical_form"] if coa else None,
                "last4": (a.get("name") or "").rsplit("(", 1)[-1].rstrip(")")
                if "(" in (a.get("name") or "")
                else None,
            }
        )

    net = assets_total - liabilities_total
    return {
        # Compatibility: total_usd retained as net of (assets - liabilities)
        "total_usd": f"{net:.2f}",
        "currency": "USD",
        "accounts": accounts,
        "configured": True,
        "source": "simplefin",
        "errors": payload.get("errors", []),
        # New: split assets vs liabilities for clearer dashboard rendering
        "assets_total": f"{assets_total:.2f}",
        "liabilities_total": f"{liabilities_total:.2f}",
        "net_total": f"{net:.2f}",
        "per_entity_assets": {k: f"{v:.2f}" for k, v in per_entity_assets.items()},
        "per_entity_liabilities": {k: f"{v:.2f}" for k, v in per_entity_liab.items()},
        "unmapped_count": sum(1 for a in accounts if a["entity_id"] is None),
    }
