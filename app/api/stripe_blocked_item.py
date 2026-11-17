"""Manual "resolved elsewhere" unblock for a blocked Stripe sync item.

  POST /api/workspaces/{wid}/stripe/blocked-item/resolve
  POST /api/workspaces/{wid}/stripe/blocked-item/reopen

Body: ``{bt_id, journal_entry_id}`` or ``{bt_id, not_books_relevant_reason}``
(exactly one of the two), with an optional ``entity_id`` when more than one
entity syncs Stripe. ``bt_id`` must equal the current
``connector_state.state.blocked_item.id`` or the call is refused with 409.

Who: an OWNER or ADMIN of the workspace, in an interactive human session.
Agents, service tokens, PATs and M2M callers are refused (Kit's rule makes
this a manual act). For that reason there is deliberately no MCP tool paired
with this route, the same call app/tools/connectors.py makes for deleting
credentials.

Reopen body: ``{bt_id, reason}`` (optional ``entity_id``). It undoes a
resolution: the id leaves ``resolved_blocked_items`` and goes back to the
head of the sync chain as the blocked item, so the worker retries it through
the mapper with normal materiality. Same callers as resolve; audited.

Read side: ``GET /api/workspaces/{wid}/stripe/blocked-item`` returns the
current blocked item per Stripe entity (id, amount, currency, error, failure
count, since when) and the resolutions with their audit fields, for the
connector settings panel. Read floor: viewer, the same as the Stripe status
route. It takes no lock and writes nothing.

Both POST calls take the shared Stripe state lock. While a sync pass holds it they
wait a few seconds and then answer 409 "retry shortly".

Nothing is posted. See app/connectors/stripe/blocked_resolution.py for what
is written, all inside the single workspace_session transaction below.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.approvals import _require_manual_proposal_human
from app.auth import AuthContext, validate_bearer_token
from app.authz import assert_entity_in_scope
from app.connectors.stripe.blocked_resolution import (
    MAX_REASON_CHARS,
    MIN_REASON_CHARS,
    RESOLVED_STATE_KEY,
    BlockedResolutionError,
    reopen_resolved_item,
    resolve_blocked_item,
)
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["stripe"])


class ResolveBlockedItemRequest(BaseModel):
    bt_id: str = Field(min_length=1, max_length=255)
    entity_id: UUID | None = None
    journal_entry_id: UUID | None = None
    not_books_relevant_reason: str | None = Field(default=None, max_length=MAX_REASON_CHARS)


class ReopenResolvedItemRequest(BaseModel):
    bt_id: str = Field(min_length=1, max_length=255)
    entity_id: UUID | None = None
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)


def require_owner_or_admin_human(auth: AuthContext, workspace: WorkspaceContext) -> None:
    """403 unless the caller is an owner/admin in an interactive human session."""
    if workspace.role not in {"owner", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolving or reopening a blocked Stripe item requires an owner or admin",
        )
    if workspace.agent_id is not None or workspace.pat_scopes is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Resolving or reopening a blocked Stripe item requires a human session",
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


@router.post("/api/workspaces/{workspace_id}/stripe/blocked-item/reopen")
async def reopen_stripe_resolved_item(
    workspace_id: UUID,
    body: ReopenResolvedItemRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict:
    """Undo a manual resolution and re-arm the item for normal processing."""
    require_owner_or_admin_human(auth, workspace)
    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            return await reopen_resolved_item(
                session,
                ctx=workspace,
                bt_id=body.bt_id.strip(),
                entity_id=body.entity_id,
                reason=body.reason,
                scope_check=assert_entity_in_scope,
            )
    except BlockedResolutionError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


# ---------------------------------------------------------------------------
# Read side for the connector settings panel
# ---------------------------------------------------------------------------


def _state_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _failure_count(state: dict[str, Any], bt_id: str) -> int:
    counts = state.get("_stripe_failure_counts")
    value = counts.get(bt_id) if isinstance(counts, dict) else None
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0) if value is not None else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def summarize_entity_state(
    entity_id: str,
    entity_display_name: str | None,
    state: dict[str, Any],
    blocked_approval: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The panel's view of one Stripe connector_state row: (blocked, resolved).

    Only the fields the panel shows leave the server: the sync window, cursor
    and revision stay internal.
    """
    blocked: dict[str, Any] | None = None
    item = state.get("blocked_item")
    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
        bt_id = item["id"]
        amount = item.get("amount")
        blocked = {
            "bt_id": bt_id,
            "entity_id": entity_id,
            "entity_display_name": entity_display_name,
            "amount": amount if isinstance(amount, int) and not isinstance(amount, bool) else None,
            "currency": _str_or_none(item.get("currency")),
            "error": _str_or_none(item.get("error")),
            "failure_count": _failure_count(state, bt_id),
            "since": _iso((blocked_approval or {}).get("created_at")),
            "approval_id": (blocked_approval or {}).get("id"),
            "approval_status": (blocked_approval or {}).get("status"),
        }

    resolved: list[dict[str, Any]] = []
    raw = state.get(RESOLVED_STATE_KEY)
    if isinstance(raw, dict):
        for bt_id, resolution in raw.items():
            if not isinstance(bt_id, str) or not bt_id or not isinstance(resolution, dict):
                continue
            original = resolution.get("blocked_item")
            original = original if isinstance(original, dict) else {}
            amount = original.get("amount")
            resolved.append({
                "bt_id": bt_id,
                "entity_id": entity_id,
                "entity_display_name": entity_display_name,
                "kind": resolution.get("kind"),
                "resolved_by_uc_uid": _str_or_none(resolution.get("resolved_by_uc_uid")),
                "resolved_at": _str_or_none(resolution.get("resolved_at")),
                "journal_entry_id": _str_or_none(resolution.get("journal_entry_id")),
                "not_books_relevant_reason": _str_or_none(
                    resolution.get("not_books_relevant_reason")
                ),
                "amount": amount if isinstance(amount, int) and not isinstance(amount, bool) else None,
                "currency": _str_or_none(original.get("currency")),
                "error": _str_or_none(original.get("error")),
                "approval_id": _str_or_none(resolution.get("approval_id")),
                "audit_id": _str_or_none(resolution.get("audit_id")),
            })
    resolved.sort(key=lambda r: r["resolved_at"] or "", reverse=True)
    return blocked, resolved


async def load_blocked_item_view(session: Any, workspace: WorkspaceContext) -> dict[str, Any]:
    """Read every Stripe connector_state row of the workspace the caller may see."""
    scope = assert_entity_in_scope(workspace, None)
    params: dict[str, Any] = {"wid": str(workspace.workspace_id)}
    scope_sql = ""
    if scope is not None:
        scope_sql = "AND cs.entity_id = ANY(:entity_scope)"
        params["entity_scope"] = [str(e) for e in scope]
    rows = (
        await session.execute(
            text(
                f"""
                SELECT cs.entity_id::text AS entity_id,
                       e.display_name AS entity_display_name,
                       cs.state
                FROM connector_state cs
                LEFT JOIN entity e ON e.id = cs.entity_id
                WHERE cs.workspace_id = :wid AND cs.connector_name = 'stripe_api'
                  {scope_sql}
                ORDER BY cs.entity_id
                """  # noqa: S608 - scope_sql is a fixed literal
            ),
            params,
        )
    ).mappings().all()

    blocked_items: list[dict[str, Any]] = []
    resolved_items: list[dict[str, Any]] = []
    for row in rows:
        state = _state_dict(row["state"])
        item = state.get("blocked_item")
        approval = None
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            approval = (
                await session.execute(
                    text(
                        """
                        SELECT id::text AS id, status::text AS status, created_at
                        FROM approval_queue
                        WHERE entity_id = :eid
                          AND subject_type = 'stripe_post_balance_transaction'
                          AND payload ->> 'stripe_balance_txn_id' = :stripe_id
                        ORDER BY (status = 'pending') DESC, created_at ASC
                        LIMIT 1
                        """
                    ),
                    {"eid": row["entity_id"], "stripe_id": item["id"]},
                )
            ).mappings().first()
        blocked, resolved = summarize_entity_state(
            row["entity_id"],
            row["entity_display_name"],
            state,
            dict(approval) if approval is not None else None,
        )
        if blocked is not None:
            blocked_items.append(blocked)
        resolved_items.extend(resolved)
    resolved_items.sort(key=lambda r: r["resolved_at"] or "", reverse=True)
    return {
        "blocked": blocked_items,
        "resolved": resolved_items,
        "min_reason_chars": MIN_REASON_CHARS,
        "max_reason_chars": MAX_REASON_CHARS,
    }


@router.get("/api/workspaces/{workspace_id}/stripe/blocked-item")
async def get_stripe_blocked_item(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Current blocked Stripe item(s) and manual resolutions. Read-only.

    Read floor: viewer, the same as GET .../connectors/stripe/status. A
    member with an entity scope sees only their entities' rows.
    """
    require_role(workspace, "viewer")
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        return await load_blocked_item_view(session, workspace)
