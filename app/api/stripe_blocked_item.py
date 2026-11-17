"""Manual "resolved elsewhere" unblock for a blocked Stripe sync item.

  POST /api/workspaces/{wid}/stripe/blocked-item/resolve

Body: ``{bt_id, journal_entry_id}`` or ``{bt_id, not_books_relevant_reason}``
(exactly one of the two), with an optional ``entity_id`` when more than one
entity syncs Stripe. ``bt_id`` must equal the current
``connector_state.state.blocked_item.id`` or the call is refused with 409.

Who: an OWNER or ADMIN of the workspace, in an interactive human session.
Agents, service tokens, PATs and M2M callers are refused (Kit's rule makes
this a manual act). For that reason there is deliberately no MCP tool paired
with this route, the same call app/tools/connectors.py makes for deleting
credentials.

Nothing is posted. See app/connectors/stripe/blocked_resolution.py for what
is written, all inside the single workspace_session transaction below.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.approvals import _require_manual_proposal_human
from app.auth import AuthContext, validate_bearer_token
from app.authz import assert_entity_in_scope
from app.connectors.stripe.blocked_resolution import (
    MAX_REASON_CHARS,
    BlockedResolutionError,
    resolve_blocked_item,
)
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["stripe"])


class ResolveBlockedItemRequest(BaseModel):
    bt_id: str = Field(min_length=1, max_length=255)
    entity_id: UUID | None = None
    journal_entry_id: UUID | None = None
    not_books_relevant_reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


def require_owner_or_admin_human(auth: AuthContext, workspace: WorkspaceContext) -> None:
    """403 unless the caller is an owner/admin in an interactive human session."""
    if workspace.role not in {"owner", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolving a blocked Stripe item requires an owner or admin",
        )
    if workspace.agent_id is not None or workspace.pat_scopes is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolving a blocked Stripe item requires a human session",
        )
    _require_manual_proposal_human(auth, workspace)


@router.post("/api/workspaces/{workspace_id}/stripe/blocked-item/resolve")
async def resolve_stripe_blocked_item(
    workspace_id: UUID,
    body: ResolveBlockedItemRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict:
    """Clear the blocked Stripe item as resolved elsewhere. Posts nothing."""
    require_owner_or_admin_human(auth, workspace)
    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            return await resolve_blocked_item(
                session,
                ctx=workspace,
                bt_id=body.bt_id.strip(),
                entity_id=body.entity_id,
                journal_entry_id=body.journal_entry_id,
                not_books_relevant_reason=body.not_books_relevant_reason,
                scope_check=assert_entity_in_scope,
            )
    except BlockedResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
