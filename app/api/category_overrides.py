"""User-trained category overrides.

When a user rejects a categorization on /reconciliation-queue, they pick
the correct category. That decision is stored as a category_override row
so future transactions matching the same payee pattern get the right
category automatically, never going through the LLM tier or asking the
user again.

  GET    /api/workspaces/{wid}/category-overrides
  POST   /api/workspaces/{wid}/category-overrides
  DELETE /api/workspaces/{wid}/category-overrides/{id}

The categorization engine (app.categorization.engine) consults this table
BEFORE applying vendor rules.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["category-overrides"])


class CategoryOverrideRequest(BaseModel):
    pattern: str = Field(..., min_length=2, max_length=256)
    category: str = Field(..., min_length=1, max_length=64)
    debit_account: str | None = None
    credit_account: str | None = None
    entity_id: UUID | None = None


@router.get("/api/workspaces/{workspace_id}/category-overrides")
async def list_overrides(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text, pattern, category,
                           debit_account, credit_account, created_by_uc_uid,
                           created_at, match_count
                    FROM category_override
                    ORDER BY created_at DESC
                    """
                )
            )
        ).mappings().all()
    return {"overrides": [dict(r) for r in rows]}


@router.post("/api/workspaces/{workspace_id}/category-overrides")
async def create_override(
    workspace_id: UUID,
    body: CategoryOverrideRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Record a user's category correction. Idempotent on (workspace_id,
    entity_id, pattern) — second post with same pattern updates the
    category instead of failing."""
    require_role(workspace, "accountant")

    pattern_lower = body.pattern.lower().strip()
    if not pattern_lower:
        raise HTTPException(400, "pattern cannot be empty after trim/lower")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        result = await session.execute(
            text(
                """
                INSERT INTO category_override (
                    workspace_id, entity_id, pattern, category,
                    debit_account, credit_account, created_by_uc_uid
                ) VALUES (
                    :workspace_id, :entity_id, :pattern, :category,
                    :debit_account, :credit_account, :created_by_uc_uid
                )
                ON CONFLICT (workspace_id, entity_id, pattern) DO UPDATE
                SET category = EXCLUDED.category,
                    debit_account = EXCLUDED.debit_account,
                    credit_account = EXCLUDED.credit_account
                RETURNING id::text, pattern, category
                """
            ),
            {
                "workspace_id": str(workspace.workspace_id),
                "entity_id": str(body.entity_id) if body.entity_id else None,
                "pattern": pattern_lower,
                "category": body.category,
                "debit_account": body.debit_account,
                "credit_account": body.credit_account,
                "created_by_uc_uid": workspace.user_uc_uid,
            },
        )
        row = result.first()

    # ---- FalkorDB LEARN (best-effort, AFTER the workspace_session commit) ----
    # A human just confirmed pattern -> category. Teach the agent-memory graph so
    # get_learned_category_for_vendor can suggest it next time. The graph's
    # learned pattern is ENTITY-scoped, so we only project when the override is
    # bound to a specific entity (an unscoped/workspace-wide override has no
    # entity to attach the Vendor node to). Authoritative store is Postgres'
    # category_override row; this is its non-authoritative mirror.
    if body.entity_id is not None:
        from app import falkordb

        falkordb.project(
            falkordb.record_learned_pattern(
                workspace.workspace_id,
                body.entity_id,
                pattern_lower,
                body.category,
                rule_id=str(row.id),
                confirmed_by=workspace.user_uc_uid,
            ),
            label=f"learned_pattern:{row.id}",
        )

    return {"id": row.id, "pattern": row.pattern, "category": row.category}


@router.delete("/api/workspaces/{workspace_id}/category-overrides/{override_id}")
async def delete_override(
    workspace_id: UUID,
    override_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    require_role(workspace, "accountant")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        result = await session.execute(
            text("DELETE FROM category_override WHERE id = :id"),
            {"id": str(override_id)},
        )
    if result.rowcount == 0:
        raise HTTPException(404, "Override not found")
    return {"ok": True}


async def load_overrides_for_workspace(
    workspace_id: UUID,
) -> list[dict[str, Any]]:
    """Helper used by the categorization engine to consult overrides
    before applying vendor rules.
    """
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT pattern, category, debit_account, credit_account,
                           entity_id::text AS entity_id
                    FROM category_override
                    """
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]
