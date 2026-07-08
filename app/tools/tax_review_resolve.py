"""MCP tool: resolve_tax_review_item — close a tax_review_queue row.

Used by the CPA Resolver agent (and any agent acting on the CPA's behalf) to
mark a queue item as reviewed / resolved / rejected. Mirrors POST
/api/workspaces/{wid}/tax-review-queue/{id}/resolve.

This is NOT a ledger write — it records a CPA's review outcome on a draft tax
position. Role floor is accountant (same as the HTTP endpoint), intentionally
lower than the approvals decide endpoint (which requires admin).

DRAFT — for CPA review only. Resolutions captured by an agent must still be
approved by a licensed CPA before any return is filed.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session


_VALID_RESOLVE_STATUSES = frozenset({"reviewed", "resolved", "rejected"})


async def resolve_tax_review_item(
    workspace_id: UUID,
    item_id: UUID,
    status: str,
    resolution_notes: str | None = None,
) -> dict[str, Any]:
    """Mark a tax_review_queue row as reviewed / resolved / rejected.

    Mirrors POST /tax-review-queue/{id}/resolve. Sets reviewer_uc_uid (the
    calling identity) and resolved_at on the row. The item must currently be
    in status='pending' or 'reviewed'; other states are a 409.

    Args:
        workspace_id: Workspace UUID.
        item_id: tax_review_queue row id.
        status: 'reviewed' | 'resolved' | 'rejected'.
            - reviewed: CPA has seen it; may note further work needed.
            - resolved: Issue identified and addressed; ready for return.
            - rejected: Item is not a valid issue; no action taken.
        resolution_notes: Optional free-text note recorded on the row.

    Returns:
        {"ok": True, "item_id": "...", "status": "...",
         "reviewer_uc_uid": "...", "resolved_at": "...",
         "_draft_notice": "..."}

    Role floor: accountant.

    DRAFT — for CPA review only. Resolutions captured by an agent need final
    CPA sign-off before any return is filed.
    """
    new_status = (status or "").strip().lower()
    if new_status not in _VALID_RESOLVE_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_RESOLVE_STATUSES)}; got {status!r}"
        )

    ctx = await authorize_workspace(workspace_id, None, min_role="accountant")

    async with workspace_session(SessionLocal, workspace_id) as session:
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
            raise LookupError(f"Tax review item {item_id} not found")

        # Entity scope: the item's entity_id must be in the caller's scope.
        assert_entity_in_scope(ctx, UUID(row["entity_id"]))

        if row["status"] not in ("pending", "reviewed"):
            raise ValueError(
                f"Item is already '{row['status']}'; "
                "only 'pending' or 'reviewed' items can be resolved."
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
                "reviewer": ctx.user_uc_uid,
                "notes": resolution_notes,
                "id": str(item_id),
            },
        )

    return {
        "ok": True,
        "item_id": str(item_id),
        "status": new_status,
        "reviewer_uc_uid": ctx.user_uc_uid,
        "resolved_at": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "_draft_notice": (
            "DRAFT — for CPA review only. This resolution was recorded by an "
            "agent on the CPA's behalf and is NOT a substitute for licensed "
            "CPA sign-off before any return is filed."
        ),
    }
