"""Tax review queue: list, fetch, and resolve 1120 preparer specialist items.

The 1120 Preparer specialist agent inserts rows into ``tax_review_queue`` via
the draft_memo and flag_for_cpa_review MCP tools. These HTTP endpoints give the
CPA reviewer a surface to list pending items, inspect details, and record their
resolution (reviewed / resolved / rejected).

Routes:
  GET  /api/workspaces/{wid}/tax-review-queue
       ?entity_id=&tax_year=&status=pending&item_type=
       — list items, viewer floor, entity-scope enforced.

  GET  /api/workspaces/{wid}/tax-review-queue/{id}
       — fetch single item, viewer floor.

  POST /api/workspaces/{wid}/tax-review-queue/{id}/resolve
       — body {status, resolution_notes}, accountant floor.
       Sets reviewer_uc_uid + resolved_at on the row.

Role floors:
  * List / get: viewer — the CPA reviewer (accountant role in the workspace)
    can also read. Viewer is the minimum so workspace owners without an
    accountant assignment can still see items.
  * Resolve: accountant — the CPA resolving items is typically provisioned
    with accountant role. Admin/owner also pass (roles are cumulative).

Entity-scope (BOLA): mirrors app/api/approvals.py. A scoped member can only
see and resolve items for their entities.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["tax-review"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ResolveRequest(BaseModel):
    status: str  # "reviewed" | "resolved" | "rejected"
    resolution_notes: str | None = None


_VALID_RESOLVE_STATUSES = frozenset({"reviewed", "resolved", "rejected"})

_VALID_ITEM_TYPES = frozenset({
    "form_line_proposal", "flagged_position", "memo", "question"
})

_VALID_QUEUE_STATUSES = frozenset({"pending", "reviewed", "resolved", "rejected", "all"})


# ---------------------------------------------------------------------------
# List items
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/tax-review-queue")
async def list_tax_review_items(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    tax_year: int | None = None,
    # URL exposes ?status= (idiomatic + matches the web client).
    # Internal name stays status_filter to avoid collision with the
    # fastapi.status import used for HTTP codes.
    status_filter: str = Query("pending", alias="status"),
    item_type: str | None = None,
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """List tax review queue items for a workspace (optionally one entity).

    Read floor is viewer. Defaults to status='pending'; pass ?status=all
    to see the full history.

    Entity-scope (BOLA): mirrors approvals.py. A scoped member is restricted
    to their entities; an unscoped member sees all.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    if status_filter and status_filter.lower() not in _VALID_QUEUE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"status_filter must be one of: "
                f"{', '.join(sorted(_VALID_QUEUE_STATUSES))}"
            ),
        )
    if item_type and item_type.lower() not in _VALID_ITEM_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"item_type must be one of: "
                f"{', '.join(sorted(_VALID_ITEM_TYPES))}"
            ),
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {
            "limit": min(max(limit, 1), 500),
            "offset": max(offset, 0),
        }

        if entity_id:
            where.append("trq.entity_id = :eid")
            params["eid"] = str(entity_id)

        if scope_restriction is not None:
            where.append("trq.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]

        if tax_year:
            where.append("trq.tax_year = :tax_year")
            params["tax_year"] = tax_year

        if status_filter and status_filter.lower() != "all":
            where.append("trq.status = :status")
            params["status"] = status_filter

        if item_type:
            where.append("trq.item_type = :item_type")
            params["item_type"] = item_type

        where_sql = " AND ".join(where)

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        trq.id::text,
                        trq.entity_id::text,
                        e.display_name AS entity_display_name,
                        e.slug AS entity_slug,
                        trq.tax_year,
                        trq.item_type,
                        trq.form_reference,
                        trq.content,
                        trq.confidence,
                        trq.citations,
                        trq.status,
                        trq.submitter_kind,
                        trq.submitter_id,
                        trq.reviewer_uc_uid,
                        trq.resolution_notes,
                        trq.created_at,
                        trq.resolved_at
                    FROM tax_review_queue trq
                    LEFT JOIN entity e ON e.id = trq.entity_id
                    WHERE {where_sql}
                    ORDER BY trq.created_at DESC, trq.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

        total = (
            await session.execute(
                text(
                    f"SELECT count(*) FROM tax_review_queue trq WHERE {where_sql}"
                ),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

    return {"items": [dict(r) for r in rows], "total": total}


# ---------------------------------------------------------------------------
# Get single item
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/tax-review-queue/{item_id}")
async def get_tax_review_item(
    workspace_id: UUID,
    item_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Fetch a single tax review queue item.

    Read floor is viewer. Entity-scope enforced: the item's entity_id must be
    in the caller's scope.
    """
    require_role(workspace, "viewer")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        trq.id::text,
                        trq.entity_id::text,
                        e.display_name AS entity_display_name,
                        e.slug AS entity_slug,
                        trq.tax_year,
                        trq.item_type,
                        trq.form_reference,
                        trq.content,
                        trq.confidence,
                        trq.citations,
                        trq.status,
                        trq.submitter_kind,
                        trq.submitter_id,
                        trq.reviewer_uc_uid,
                        trq.resolution_notes,
                        trq.created_at,
                        trq.resolved_at
                    FROM tax_review_queue trq
                    LEFT JOIN entity e ON e.id = trq.entity_id
                    WHERE trq.id = :id
                    """
                ),
                {"id": str(item_id)},
            )
        ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="Tax review item not found")

    # Entity-scope check: the item was found in the workspace schema; now verify
    # the caller's entity_scope permits access to this entity.
    assert_entity_in_scope(workspace, UUID(row["entity_id"]))

    return dict(row)


# ---------------------------------------------------------------------------
# Resolve an item
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/tax-review-queue/{item_id}/resolve")
async def resolve_tax_review_item(
    workspace_id: UUID,
    item_id: UUID,
    body: ResolveRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Mark a tax review queue item as reviewed, resolved, or rejected.

    The CPA reviewer calls this endpoint after inspecting the specialist's
    output and reaching a conclusion on the flagged position or memo.

    Role floor: accountant (the CPA is typically provisioned as an accountant;
    admin/owner also pass). This is intentionally lower than the approvals
    ``/decide`` endpoint (which requires admin) because tax-review items are
    NOT applying changes to the ledger — they are recording a CPA's review
    outcome on a draft position.

    Body:
        status: 'reviewed' | 'resolved' | 'rejected'
            - 'reviewed': CPA has seen it; may note further work needed.
            - 'resolved': Issue identified and addressed; ready for return.
            - 'rejected': Item is not a valid issue; no action taken.
        resolution_notes: Free-text note from the CPA (optional but recommended).

    Sets reviewer_uc_uid and resolved_at on the row.
    """
    require_role(workspace, "accountant")

    new_status = (body.status or "").strip().lower()
    if new_status not in _VALID_RESOLVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"status must be one of: "
                f"{', '.join(sorted(_VALID_RESOLVE_STATUSES))}"
            ),
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Lock the row and validate it exists + is pending before updating.
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text, status
                    FROM tax_review_queue
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": str(item_id)},
            )
        ).mappings().first()

        if row is None:
            raise HTTPException(
                status_code=404, detail="Tax review item not found"
            )

        # Entity-scope check
        assert_entity_in_scope(workspace, UUID(row["entity_id"]))

        if row["status"] not in ("pending", "reviewed"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Item is already '{row['status']}'; "
                    "only 'pending' or 'reviewed' items can be resolved."
                ),
            )

        await session.execute(
            text(
                """
                UPDATE tax_review_queue
                SET status            = :new_status,
                    reviewer_uc_uid   = :reviewer,
                    resolution_notes  = :notes,
                    resolved_at       = now()
                WHERE id = :id
                """
            ),
            {
                "new_status": new_status,
                "reviewer": workspace.user_uc_uid,
                "notes": body.resolution_notes,
                "id": str(item_id),
            },
        )

    return {
        "ok": True,
        "item_id": str(item_id),
        "status": new_status,
        "reviewer_uc_uid": workspace.user_uc_uid,
        "resolved_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
    }
