"""Listing endpoints for workspaces + entities.

Powers the WorkspaceSwitcher + EntitySelector in the web UI:
  GET /api/me/workspaces                       — workspaces the caller belongs to
  GET /api/workspaces/{workspace_id}/entities  — entities within one workspace
  GET /api/workspaces/{workspace_id}           — single workspace detail
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["workspaces"])


async def _ensure_user_account(uc_uid: str) -> UUID:
    """Auto-provision a user_account row on first sight of a uc_uid."""
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text("SELECT id FROM public.user_account WHERE uc_uid = :uc"),
                    {"uc": uc_uid},
                )
            ).first()
            if row:
                return row.id
            result = await session.execute(
                text(
                    """
                    INSERT INTO public.user_account (id, uc_uid, is_active, created_at)
                    VALUES (gen_random_uuid(), :uc, true, now())
                    RETURNING id
                    """
                ),
                {"uc": uc_uid},
            )
            return result.scalar_one()


@router.get("/api/me/workspaces")
async def list_my_workspaces(
    auth: AuthContext = Depends(validate_bearer_token),
) -> list[dict[str, Any]]:
    """Return all workspaces the authenticated user has a membership in.

    Each row: `id`, `slug`, `display_name`, `role`, `isolation_tier`,
    `hipaa_protected`, `entity_count`, `last_active_at`.
    """
    user_id = await _ensure_user_account(auth.uc_uid)
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            w.id::text          AS id,
                            w.slug              AS slug,
                            w.display_name      AS display_name,
                            m.role::text        AS role,
                            w.isolation_tier::text AS isolation_tier,
                            w.hipaa_protected   AS hipaa_protected,
                            w.postgres_schema   AS postgres_schema,
                            m.last_active_at    AS last_active_at
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :user_id
                          AND w.status = 'active'
                        ORDER BY w.display_name
                        """
                    ),
                    {"user_id": user_id},
                )
            ).mappings().all()

    workspaces: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        # Count entities (per-workspace schema)
        try:
            async with workspace_session(SessionLocal, entry["id"]) as ws:
                count_row = (
                    await ws.execute(
                        text(
                            "SELECT count(*) AS c FROM entity WHERE status = 'active'"
                        )
                    )
                ).first()
                entry["entity_count"] = count_row.c if count_row else 0
        except Exception:
            entry["entity_count"] = 0
        entry.pop("postgres_schema", None)
        workspaces.append(entry)
    return workspaces


@router.get("/api/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Return one workspace's metadata + caller's role."""
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id::text AS id, slug, display_name,
                               isolation_tier::text AS isolation_tier,
                               hipaa_protected, status::text AS status,
                               created_at
                        FROM public.workspace
                        WHERE id = :id
                        """
                    ),
                    {"id": str(workspace.workspace_id)},
                )
            ).mappings().first()
            if row is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
            return {**dict(row), "role": workspace.role}


@router.get("/api/workspaces/{workspace_id}/entities")
async def list_workspace_entities(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> list[dict[str, Any]]:
    """Return all active entities in the workspace, filtered by entity_scope if set."""
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text            AS id,
                        slug                AS slug,
                        display_name        AS display_name,
                        legal_name          AS legal_name,
                        kind::text          AS kind,
                        country_code        AS country_code,
                        functional_currency AS functional_currency,
                        hipaa_protected     AS hipaa_protected,
                        status::text        AS status,
                        created_at
                    FROM entity
                    WHERE status = 'active'
                    ORDER BY display_name
                    """
                )
            )
        ).mappings().all()

    entities = [dict(row) for row in rows]
    # Apply entity_scope restriction from membership if set
    if workspace.entity_scope is not None:
        allowed = {str(eid) for eid in workspace.entity_scope}
        entities = [e for e in entities if e["id"] in allowed]
    return entities
