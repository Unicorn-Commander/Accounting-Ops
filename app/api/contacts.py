"""Contacts (counterparty) HTTP routes — list / search / parse-preview / import.

  GET  /api/workspaces/{wid}/counterparties              — list + search (viewer)
  POST /api/workspaces/{wid}/counterparties              — create one (accountant)
  POST /api/workspaces/{wid}/counterparties/parse        — parse an uploaded
       vCard/CSV into preview rows; NO writes (accountant)
  POST /api/workspaces/{wid}/counterparties/import       — bulk upsert the
       confirmed rows (accountant)

Auth mirrors app/api/invoices.py: resolve_workspace + require_role +
workspace_session. Counterparties are workspace-shared (no entity scope), so
there's no assert_entity_in_scope here. Parsing is a pure transform (see
app/contacts/import_parse.py) so the preview never touches the DB — the user
tags customer/vendor/skip, then calls import.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import bearer
from app.contacts import (
    bulk_import_counterparties,
    list_counterparties,
    parse_contacts,
    upsert_counterparty,
)
from app.contacts.contact_ops_client import (
    ContactOpsError,
    ContactOpsNotConfigured,
    fetch_contact_rows,
    is_configured as contact_ops_configured,
)
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["contacts"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ContactRow(BaseModel):
    display_name: str | None = None
    name: str | None = None
    kind: str = "customer"
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    company: str | None = None
    person_name: str | None = None
    contact_id: str | None = None
    source: str | None = None


class ParseRequest(BaseModel):
    filename: str = Field(default="contacts.csv", max_length=256)
    content: str = Field(..., description="Raw text of the uploaded .vcf or .csv")


class ImportRequest(BaseModel):
    rows: list[ContactRow] = Field(..., max_length=10000)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/counterparties")
async def list_contacts(
    workspace_id: UUID,
    q: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        return await list_counterparties(
            session, q=q, kind=kind, limit=limit, offset=offset
        )


@router.post(
    "/api/workspaces/{workspace_id}/counterparties",
    status_code=status.HTTP_201_CREATED,
)
async def create_contact(
    workspace_id: UUID,
    body: ContactRow,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "accountant")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        res = await upsert_counterparty(
            session,
            workspace_id=str(workspace.workspace_id),
            name=(body.display_name or body.name or ""),
            kind=body.kind,
            contact_id=body.contact_id,
            email=body.email,
            phone=body.phone,
            address=body.address,
            company=body.company,
            person_name=body.person_name,
            source=body.source or "manual",
        )
        return res


@router.post("/api/workspaces/{workspace_id}/counterparties/parse")
async def parse_contacts_preview(
    workspace_id: UUID,
    body: ParseRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Parse an uploaded vCard/CSV into normalized preview rows. NO DB writes."""
    require_role(workspace, "accountant")
    parsed = parse_contacts(body.filename, body.content)
    return {
        "count": len(parsed),
        "rows": [p.as_dict() for p in parsed],
    }


@router.get("/api/workspaces/{workspace_id}/counterparties/sources")
async def contact_sources(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Which import sources are available. Lets the UI show/hide the
    'Sync from Contact-Ops' button without guessing."""
    require_role(workspace, "viewer")
    return {"file": True, "contact_ops": contact_ops_configured()}


@router.post("/api/workspaces/{workspace_id}/counterparties/sync-contact-ops")
async def sync_from_contact_ops(
    workspace_id: UUID,
    q: str | None = Query(default=None, description="Optional search query"),
    workspace: WorkspaceContext = Depends(resolve_workspace),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict[str, Any]:
    """Pull the caller's contacts from Contact-Ops as PREVIEW rows (no writes).

    The rows flow into the same review/tag screen as a file import, then the
    user confirms via /import. Returns 503 (with a clear message) when the
    federation isn't wired up yet, so the UI degrades gracefully.
    """
    require_role(workspace, "accountant")
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        rows = await fetch_contact_rows(credentials.credentials, query=q)
    except ContactOpsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ContactOpsError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"count": len(rows), "rows": rows, "source": "contact-ops"}


@router.post("/api/workspaces/{workspace_id}/counterparties/import")
async def import_contacts(
    workspace_id: UUID,
    body: ImportRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Bulk-upsert the confirmed preview rows into this workspace."""
    require_role(workspace, "accountant")
    rows = [r.model_dump() for r in body.rows]
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        return await bulk_import_counterparties(
            session, workspace_id=str(workspace.workspace_id), rows=rows
        )


__all__ = ("router",)
