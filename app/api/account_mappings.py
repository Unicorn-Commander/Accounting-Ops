"""Account-to-entity mapping CRUD.

Maps live bank/payment-processor accounts (discovered through connectors) to
specific entities within the workspace. The EntitySelector filters live
transactions and cash position using these mappings.

  GET  /api/workspaces/{wid}/account-mappings   — list discovered accounts + current mappings
  PUT  /api/workspaces/{wid}/account-mappings   — bulk upsert mappings

Discovery happens by querying the active SimpleFIN connection (and eventually
eBay/Plaid/etc.). Unmapped accounts are surfaced so the user can assign them.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["account-mappings"])


class AccountMappingRow(BaseModel):
    entity_id: UUID
    source: str = Field(default="simplefin")
    external_account_id: str
    external_org: str | None = None
    external_account_name: str | None = None
    label: str | None = None
    notes: str | None = None
    auto_categorize: bool = True


class BulkMappingRequest(BaseModel):
    mappings: list[AccountMappingRow]


async def _simplefin_access_url(workspace: WorkspaceContext) -> str | None:
    """Same DB-first lookup as cash_position / transactions."""
    async with SessionLocal() as session:
        async with session.begin():
            try:
                pt = await get_credential_plaintext(
                    session,
                    workspace.workspace_id,
                    "simplefin",
                    requesting_service="account_mappings",
                    actor_uc_uid=workspace.user_uc_uid,
                    actor_agent_id=workspace.agent_id,
                )
                if pt:
                    return pt
            except Exception:
                pass
    return os.getenv(
        f"SIMPLEFIN_ACCESS_URL_{workspace.user_uc_uid.upper()}"
    ) or os.getenv("SIMPLEFIN_ACCESS_URL_AARON")


async def _discover_simplefin_accounts(access_url: str) -> list[dict[str, Any]]:
    parts = urlsplit(access_url)
    user, pw = parts.username or "", parts.password or ""
    host = parts.hostname or ""
    path = parts.path.rstrip("/")
    if not path.endswith("/accounts"):
        path = f"{path}/accounts"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{parts.scheme}://{host}{path}?balances-only=1", auth=(user, pw)
        )
        if resp.status_code >= 400:
            return []
        payload = resp.json()
    accounts: list[dict[str, Any]] = []
    for a in payload.get("accounts", []):
        org = a.get("org") or {}
        accounts.append(
            {
                "source": "simplefin",
                "external_account_id": a.get("id"),
                "external_org": org.get("name") or org.get("domain") or "—",
                "external_account_name": a.get("name"),
                "currency": a.get("currency", "USD"),
                "balance": str(a.get("balance", "0")),
            }
        )
    return accounts


@router.get("/api/workspaces/{workspace_id}/account-mappings")
async def list_account_mappings(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Discover live accounts + return current mappings + entities for selection."""
    # 1. Discover live accounts from connectors (SimpleFIN for now).
    accounts: list[dict[str, Any]] = []
    access_url = await _simplefin_access_url(workspace)
    if access_url:
        accounts = await _discover_simplefin_accounts(access_url)

    # 2. Fetch existing mappings + entity list from the workspace schema.
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        mapping_rows = (
            await session.execute(
                text(
                    """
                    SELECT m.id::text, m.entity_id::text, m.source, m.external_account_id,
                           m.external_org, m.external_account_name, m.label, m.notes,
                           m.auto_categorize, m.updated_at,
                           e.slug AS entity_slug, e.display_name AS entity_display_name,
                           e.kind::text AS entity_kind
                    FROM account_entity_mapping m
                    JOIN entity e ON e.id = m.entity_id
                    ORDER BY m.external_org, m.external_account_name
                    """
                )
            )
        ).mappings().all()
        entity_rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, slug, display_name, kind::text AS kind
                    FROM entity
                    WHERE status = 'active'
                    ORDER BY display_name
                    """
                )
            )
        ).mappings().all()

    mappings = [dict(r) for r in mapping_rows]
    entities = [dict(r) for r in entity_rows]

    # 3. Mark which live accounts are unmapped.
    mapped_keys = {(m["source"], m["external_account_id"]) for m in mappings}
    for acct in accounts:
        key = (acct["source"], acct["external_account_id"])
        acct["mapped"] = key in mapped_keys
        match = next(
            (m for m in mappings if (m["source"], m["external_account_id"]) == key),
            None,
        )
        if match:
            acct["mapping_id"] = match["id"]
            acct["entity_id"] = match["entity_id"]
            acct["entity_display_name"] = match["entity_display_name"]
            acct["entity_slug"] = match["entity_slug"]
            acct["auto_categorize"] = match["auto_categorize"]
            acct["label"] = match["label"]

    return {
        "live_accounts": accounts,
        "mappings": mappings,
        "entities": entities,
        "unmapped_count": sum(1 for a in accounts if not a["mapped"]),
    }


@router.put("/api/workspaces/{workspace_id}/account-mappings")
async def upsert_account_mappings(
    workspace_id: UUID,
    body: BulkMappingRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Upsert a bulk set of mappings. Idempotent by (source, external_account_id)."""
    require_role(workspace, "accountant")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Validate that entity_ids exist in this workspace
        entity_rows = (
            await session.execute(
                text("SELECT id::text FROM entity WHERE status = 'active'")
            )
        ).all()
        valid_entity_ids = {r[0] for r in entity_rows}
        for m in body.mappings:
            if str(m.entity_id) not in valid_entity_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"entity_id {m.entity_id} not found in this workspace",
                )

        upserted = 0
        for m in body.mappings:
            await session.execute(
                text(
                    """
                    INSERT INTO account_entity_mapping (
                        workspace_id, entity_id, source, external_account_id,
                        external_org, external_account_name, label, notes,
                        auto_categorize
                    )
                    VALUES (
                        :workspace_id, :entity_id, :source, :external_account_id,
                        :external_org, :external_account_name, :label, :notes,
                        :auto_categorize
                    )
                    ON CONFLICT (source, external_account_id) DO UPDATE
                    SET entity_id = EXCLUDED.entity_id,
                        external_org = EXCLUDED.external_org,
                        external_account_name = EXCLUDED.external_account_name,
                        label = EXCLUDED.label,
                        notes = EXCLUDED.notes,
                        auto_categorize = EXCLUDED.auto_categorize
                    """
                ),
                {
                    "workspace_id": str(workspace.workspace_id),
                    "entity_id": str(m.entity_id),
                    "source": m.source,
                    "external_account_id": m.external_account_id,
                    "external_org": m.external_org,
                    "external_account_name": m.external_account_name,
                    "label": m.label,
                    "notes": m.notes,
                    "auto_categorize": m.auto_categorize,
                },
            )
            upserted += 1

    return {"upserted": upserted}


async def get_account_to_entity_map(
    workspace_id: UUID,
) -> dict[str, dict[str, Any]]:
    """Internal helper used by transactions + cash_position endpoints.

    Returns {(source, external_account_id): {entity_id, entity_display_name, ...}}
    """
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT m.source, m.external_account_id, m.entity_id::text AS entity_id,
                           m.auto_categorize, e.display_name AS entity_display_name,
                           e.slug AS entity_slug, e.kind::text AS entity_kind
                    FROM account_entity_mapping m
                    JOIN entity e ON e.id = m.entity_id
                    """
                )
            )
        ).mappings().all()
    return {
        f"{r['source']}:{r['external_account_id']}": dict(r) for r in rows
    }
