"""Approval recommendations: list pre-analyzer results.

  GET /api/workspaces/{wid}/approval-recommendations
        ?entity_id=   (required — entity-scoped)
        &cluster_id=  (optional — filter to one cluster)
        &status=pending|all  (optional — filter by whether target approval is
                               still pending; default 'pending')
        &limit=       (default 100, max 500)
        &offset=      (default 0)

Role floor: viewer (read-only; the analyzer that writes recommendations requires
accountant, but listing them is safe for any workspace member).

Returns:
  {
    "recommendations": [{...}, ...],
    "total": int,
    "clusters": [{"cluster_id": str, "count": int,
                  "confidence": str, "recommended_decision": str,
                  "total_amount_cents": int}, ...]
  }
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["approval-recommendations"])


@router.get("/api/workspaces/{workspace_id}/approval-recommendations")
async def list_approval_recommendations(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    cluster_id: str | None = None,
    status: str = "pending",
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """List approval recommendations for a workspace/entity.

    Joins ``approval_recommendation`` with ``approval_queue`` so each row
    carries the recommendation plus the live status of its target approval.

    Parameters:
        entity_id: required for entity-scoped access; an admin/owner may omit
            it to get cross-entity recommendations (rare).
        cluster_id: optional — filter to a single cluster.
        status: 'pending' (default) restricts to recommendations whose target
            approval is still pending; 'all' includes approved/rejected.
        limit: page size (max 500).
        offset: page offset.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where: list[str] = ["1=1"]
        params: dict[str, Any] = {
            "limit": min(limit, 500),
            "offset": offset,
        }

        if entity_id:
            where.append("ar.entity_id = :eid")
            params["eid"] = str(entity_id)

        if scope_restriction is not None:
            where.append("ar.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]

        if cluster_id:
            where.append("ar.cluster_id = :cluster_id")
            params["cluster_id"] = cluster_id

        if status and status.lower() != "all":
            where.append("aq.status = :approval_status")
            params["approval_status"] = status.lower()

        where_sql = " AND ".join(where)

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        ar.id::text AS recommendation_id,
                        ar.approval_id::text,
                        ar.cluster_id,
                        ar.recommended_decision,
                        ar.recommended_category,
                        ar.confidence,
                        ar.rationale,
                        ar.submitter_kind,
                        ar.submitter_id,
                        ar.created_at,
                        -- target approval fields
                        aq.entity_id::text AS entity_id,
                        aq.status AS approval_status,
                        aq.amount,
                        aq.currency_code,
                        aq.payload,
                        aq.created_at AS approval_created_at
                    FROM approval_recommendation ar
                    JOIN approval_queue aq ON aq.id = ar.approval_id
                    WHERE {where_sql}
                    ORDER BY ar.created_at DESC, ar.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

        total = (
            await session.execute(
                text(
                    f"""
                    SELECT count(*)
                    FROM approval_recommendation ar
                    JOIN approval_queue aq ON aq.id = ar.approval_id
                    WHERE {where_sql}
                    """
                ),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

        # Build cluster roll-up from recommendations that matched the filter
        # (separate query for cluster aggregates — avoids double-counting in the
        # paginated rows).
        cluster_rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        ar.cluster_id,
                        count(*)::int AS count,
                        ar.confidence,
                        ar.recommended_decision,
                        sum(CAST(ar.recommended_category IS NOT NULL AS int))::int
                            AS has_category_count,
                        -- representative category (most common in cluster)
                        (SELECT recommended_category
                         FROM approval_recommendation ar2
                         WHERE ar2.cluster_id = ar.cluster_id
                           AND ar2.workspace_id = ar.workspace_id
                         GROUP BY recommended_category
                         ORDER BY count(*) DESC LIMIT 1) AS representative_category
                    FROM approval_recommendation ar
                    JOIN approval_queue aq ON aq.id = ar.approval_id
                    WHERE {where_sql}
                    GROUP BY ar.cluster_id, ar.confidence, ar.recommended_decision,
                             ar.workspace_id
                    ORDER BY
                        CASE ar.confidence
                            WHEN 'high'   THEN 1
                            WHEN 'medium' THEN 2
                            WHEN 'low'    THEN 3
                            ELSE 9
                        END,
                        count(*) DESC
                    """
                ),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).mappings().all()

    return {
        "recommendations": [dict(r) for r in rows],
        "total": total,
        "clusters": [dict(r) for r in cluster_rows],
    }
