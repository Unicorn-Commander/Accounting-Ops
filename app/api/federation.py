"""Cross-app federation reads (machine-to-machine).

Read-only endpoints that trusted suite apps call over the shared uchub Keycloak
(client-credentials M2M). Today: Tax-Planning-Ops pulls an entity's structured
tax-document extractions (the ``document_extraction`` table) by
(entity_id, tax_year) so it can consume AO's OCR output instead of re-running
the vision model — **AO owns the OCR; TPO is the consumer.**

These routes use ``resolve_workspace_for_federation`` (NOT ``resolve_workspace``):
a caller whose ``azp`` is in ``ACCTOPS_TRUSTED_M2M_CALLERS`` is floored to
``viewer`` with no membership row; HIPAA workspaces are refused. This is the same
rail the P&L federation read (``reports.py :: profit_and_loss``) already rides.
No document bytes are ever served — only the classification + verbatim
transcribed labeled values.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.db import SessionLocal
from app.tenancy import (
    WorkspaceContext,
    require_role,
    resolve_workspace_for_federation,
)
from app.workspace_session import workspace_session

router = APIRouter(tags=["federation"])


@router.get("/api/workspaces/{workspace_id}/tax-documents")
async def federated_tax_documents(
    workspace_id: UUID,
    entity_id: str | None = None,
    tax_year: int | None = None,
    doc_type: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace_for_federation),
) -> dict[str, Any]:
    """Structured tax-document extractions for a federation consumer.

    AO owns the OCR; consumers (Tax-Planning-Ops) read these rows over the
    shared-Keycloak M2M federation by (entity_id, tax_year) rather than
    re-running the vision model. Accepts a normal workspace-member JWT OR a
    trusted M2M client-credentials JWT (``azp`` in ``ACCTOPS_TRUSTED_M2M_CALLERS``)
    via ``resolve_workspace_for_federation``, which floors the caller to viewer
    and refuses HIPAA workspaces.

    Filters (all optional, ANDed): ``entity_id`` (UUID), ``tax_year`` (int),
    ``doc_type`` (str). Returns the verbatim transcribed ``fields`` plus
    classification metadata. Rows of every status are returned (a ``failed`` row
    still tells the consumer "a document exists here we couldn't read"); the
    consumer decides what to trust. Never serves the document bytes.
    """
    require_role(workspace, "viewer")

    # Build the WHERE incrementally; "TRUE" base keeps the f-string valid when no
    # filter is supplied. Every value is a bound parameter (never interpolated).
    filters = ["TRUE"]
    params: dict[str, Any] = {}
    if entity_id:
        try:
            UUID(entity_id)
        except ValueError as exc:
            raise HTTPException(400, "entity_id must be a UUID") from exc
        filters.append("de.entity_id = :eid")
        params["eid"] = entity_id
    if tax_year is not None:
        filters.append("de.tax_year = :ty")
        params["ty"] = tax_year
    if doc_type:
        filters.append("de.doc_type = :dt")
        params["dt"] = doc_type
    where_sql = " AND ".join(filters)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT de.id::text          AS id,
                           de.entity_id::text   AS entity_id,
                           de.receipt_id::text  AS receipt_id,
                           de.doc_type,
                           de.tax_year,
                           de.payer_name,
                           de.recipient_name,
                           de.fields,
                           de.confidence,
                           de.status::text      AS status,
                           de.model,
                           de.extracted_at
                    FROM document_extraction de
                    WHERE {where_sql}
                    ORDER BY de.tax_year DESC NULLS LAST, de.doc_type, de.payer_name
                    """
                ),
                params,
            )
        ).mappings().all()

    return {
        "workspace_id": str(workspace_id),
        "entity_id": entity_id,
        "tax_year": tax_year,
        "extractions": [
            {
                "id": r["id"],
                "entity_id": r["entity_id"],
                "receipt_id": r["receipt_id"],
                "doc_type": r["doc_type"],
                "tax_year": r["tax_year"],
                "payer_name": r["payer_name"],
                "recipient_name": r["recipient_name"],
                "fields": r["fields"] or {},
                "confidence": (
                    str(r["confidence"]) if r["confidence"] is not None else None
                ),
                "status": r["status"],
                "model": r["model"],
                "extracted_at": (
                    r["extracted_at"].isoformat() if r["extracted_at"] else None
                ),
            }
            for r in rows
        ],
        "total": len(rows),
    }
