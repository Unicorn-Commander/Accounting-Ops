"""Setup Assistant bootstrap endpoint.

The Setup Assistant lives at the /setup web route which embeds a chat. The
chat-session creation requires an ``entity_id`` FK, so for the very-first-run
case (workspace with 0 entities), we need to pre-create a placeholder entity.

This endpoint creates that placeholder ONLY if the workspace has zero active
entities. Returns the bound entity_id either way. Idempotent.

Role floor: accountant (the user provisioning their first entity will be
either owner or admin; the floor is set conservatively).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import bind_http_caller
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(
    tags=["setup"],
    dependencies=[Depends(bind_http_caller)],
)


@router.post("/api/workspaces/{workspace_id}/setup/bootstrap")
async def bootstrap_setup_entity(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Ensure at least one entity exists; create a placeholder if needed.

    If the workspace already has an entity, returns the first one. If not,
    creates a placeholder entity called "Setup Sandbox" + seeds a minimal
    chart of accounts, then returns the new entity_id.

    The Setup Assistant chat is bound to this entity_id. As the user walks
    through intake, the assistant calls ``create_entity_with_coa`` to add the
    real entity — at which point the placeholder can be renamed or left as-is
    (the user can delete it later via the entity admin surface).

    Returns:
        ``{"entity_id": "<uuid>", "display_name": ..., "kind": ...,
            "is_placeholder": <bool>}``

    Role floor: accountant.
    """
    require_role(workspace, "accountant")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Is there an existing entity we can bind to?
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id, display_name, kind::text AS kind "
                    "FROM entity WHERE status = 'active' ORDER BY created_at LIMIT 1"
                )
            )
        ).mappings().first()

        if existing:
            return {
                "entity_id": existing["id"],
                "display_name": existing["display_name"],
                "kind": existing["kind"],
                "is_placeholder": False,
            }

    # No entities — provision a placeholder via the same path
    # ``create_entity_with_coa`` uses. We import here to avoid a circular
    # import at module-load time.
    from app.tools.create_entity_with_coa import create_entity_with_coa

    result = await create_entity_with_coa(
        workspace_id=workspace.workspace_id,
        display_name="Setup Sandbox",
        legal_name="Setup Sandbox (Placeholder)",
        kind="other",
        country_code="US",
    )
    return {
        "entity_id": result["entity_id"],
        "display_name": result["display_name"],
        "kind": result["kind"],
        "is_placeholder": True,
    }
