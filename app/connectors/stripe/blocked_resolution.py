"""Manual "resolved elsewhere" unblock for a BLOCKED Stripe sync item.

Kit's rule (KIT-41-39): a blocked Stripe item leaves the head of the sync
chain in exactly two ways.

  1. Fix the config and let the worker retry it (scripts/stripe_sync.py).
  2. A human OWNER/ADMIN says it was resolved elsewhere. That claim needs
     either a manual journal entry id that exists in THIS workspace's books,
     or an explicit "not books-relevant" reason.

Path 2 lives here. It never posts to the books, never touches materiality and
never runs on its own. In ONE transaction it:

  * appends a ``decision_audit`` row (who, when, why, journal entry or reason);
  * marks the "(blocked)" approval row decided (status ``rejected``: the held
    posting is refused, nothing was posted) and stores the resolution on it;
  * clears ``blocked_item`` and that id's failure count in
    ``connector_state.state``, the same merge ``_record_sync_outcome`` does with
    ``clear_blocked_item=True``, and parks the continuation cursor on the
    resolved item so the next pass continues after it;
  * remembers the id under ``resolved_blocked_items`` so a later re-read of the
    24h window overlap skips it instead of posting it a second time.

The approval row, the audit row and connector_state are all workspace tables,
so the customer export (app/data_rights/export.py, dynamic table discovery)
carries the resolution without any change there.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenancy import WorkspaceContext
from app.tools._audit import write_decision_audit

RESOLVED_STATE_KEY = "resolved_blocked_items"
AUDIT_TOOL_NAME = "stripe_blocked_item_resolve"
AUDIT_OUTCOME = "resolved_elsewhere"
MIN_REASON_CHARS = 10
MAX_REASON_CHARS = 2000


class BlockedResolutionError(Exception):
    """A refusal with the HTTP status the API layer should return."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def load_resolved_blocked_ids(session: AsyncSession, entity_id: UUID) -> frozenset[str]:
    """Stripe ids a human resolved elsewhere; the sync worker must skip them."""
    row = (
        await session.execute(
            text(
                """
                SELECT state -> 'resolved_blocked_items' AS resolved
                FROM connector_state
                WHERE entity_id = :eid AND connector_name = 'stripe_api'
                """
            ),
            {"eid": str(entity_id)},
        )
    ).first()
    resolved = getattr(row, "resolved", None) if row is not None else None
    if isinstance(resolved, str):
        try:
            resolved = json.loads(resolved)
        except ValueError:
            resolved = None
    if not isinstance(resolved, dict):
        return frozenset()
    return frozenset(key for key in resolved if isinstance(key, str) and key)


def _state_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def resolved_state(state: dict[str, Any], bt_id: str, resolution: dict[str, Any]) -> dict[str, Any]:
    """Return connector_state.state after a manual resolution of ``bt_id``.

    Mirrors ``_record_sync_outcome(clear_blocked_item=True)``: drop the block
    and that id's failure count. The pinned window and last_sync_at stay, and
    the cursor moves onto the resolved item, which is where the chain stood
    once everything before it had been handled.
    """
    new_state = dict(state)
    new_state.pop("blocked_item", None)
    counts = new_state.get("_stripe_failure_counts")
    if isinstance(counts, dict):
        counts = {key: value for key, value in counts.items() if key != bt_id}
        if counts:
            new_state["_stripe_failure_counts"] = counts
        else:
            new_state.pop("_stripe_failure_counts", None)
    new_state["_stripe_continuation_cursor"] = bt_id
    resolved = new_state.get(RESOLVED_STATE_KEY)
    resolved = dict(resolved) if isinstance(resolved, dict) else {}
    resolved[bt_id] = resolution
    new_state[RESOLVED_STATE_KEY] = resolved
    new_state["last_outcome"] = AUDIT_OUTCOME
    return new_state


async def resolve_blocked_item(
    session: AsyncSession,
    *,
    ctx: WorkspaceContext,
    bt_id: str,
    entity_id: UUID | None,
    journal_entry_id: UUID | None,
    not_books_relevant_reason: str | None,
    scope_check: Callable[[WorkspaceContext, UUID], Any],
) -> dict[str, Any]:
    """Record a manual resolution. Caller owns the transaction and authz floor."""
    reason = (not_books_relevant_reason or "").strip() or None
    if (journal_entry_id is None) == (reason is None):
        raise BlockedResolutionError(
            422, "Provide exactly one of journal_entry_id or not_books_relevant_reason"
        )
    if reason is not None and not MIN_REASON_CHARS <= len(reason) <= MAX_REASON_CHARS:
        raise BlockedResolutionError(
            422,
            f"not_books_relevant_reason must be {MIN_REASON_CHARS} to "
            f"{MAX_REASON_CHARS} characters",
        )
    workspace_id = ctx.workspace_id

    params: dict[str, Any] = {"wid": str(workspace_id)}
    entity_filter = ""
    if entity_id is not None:
        entity_filter = "AND entity_id = :eid"
        params["eid"] = str(entity_id)
    rows = (
        await session.execute(
            text(
                f"""
                SELECT id::text AS id, entity_id::text AS entity_id, state
                FROM connector_state
                WHERE workspace_id = :wid AND connector_name = 'stripe_api'
                  {entity_filter}
                FOR UPDATE
                """  # noqa: S608 - entity_filter is a fixed literal
            ),
            params,
        )
    ).mappings().all()
    blocked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        state = _state_dict(row["state"])
        item = state.get("blocked_item")
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            blocked.append((dict(row), state))
    matches = [(row, state) for row, state in blocked if state["blocked_item"]["id"] == bt_id]
    if not matches:
        if not blocked:
            raise BlockedResolutionError(409, "No Stripe item is currently blocked")
        raise BlockedResolutionError(409, "bt_id is not the current blocked Stripe item")
    if len(matches) > 1:
        raise BlockedResolutionError(409, "bt_id is blocked on more than one entity; pass entity_id")
    row, state = matches[0]
    row_entity = UUID(row["entity_id"])
    scope_check(ctx, row_entity)
    blocked_item = dict(state["blocked_item"])

    journal_ref: dict[str, Any] | None = None
    if journal_entry_id is not None:
        je = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, entity_id::text AS entity_id
                    FROM journal_entry
                    WHERE id = :jid AND workspace_id = :wid
                    """
                ),
                {"jid": str(journal_entry_id), "wid": str(workspace_id)},
            )
        ).mappings().first()
        if je is None:
            raise BlockedResolutionError(
                422, "journal_entry_id does not exist in this workspace's books"
            )
        journal_ref = {"journal_entry_id": je["id"], "journal_entry_entity_id": je["entity_id"]}

    approval = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, reasoning
                FROM approval_queue
                WHERE entity_id = :eid
                  AND subject_type = 'stripe_post_balance_transaction'
                  AND status = 'pending'
                  AND payload ->> 'stripe_balance_txn_id' = :stripe_id
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"eid": str(row_entity), "stripe_id": bt_id},
        )
    ).mappings().first()
    approval_id = approval["id"] if approval else None

    resolved_by = ctx.user_uc_uid
    resolution: dict[str, Any] = {
        "kind": "journal_entry" if journal_ref else "not_books_relevant",
        "resolved_by_uc_uid": resolved_by,
        "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
        "journal_entry_id": journal_ref["journal_entry_id"] if journal_ref else None,
        "not_books_relevant_reason": reason,
        "blocked_item": blocked_item,
        "approval_id": approval_id,
    }
    note = (
        f"Resolved elsewhere by {resolved_by}: manual journal entry "
        f"{resolution['journal_entry_id']}"
        if journal_ref
        else f"Resolved elsewhere by {resolved_by}: not books-relevant: {reason}"
    )
    response = {
        "ok": True,
        "bt_id": bt_id,
        "entity_id": str(row_entity),
        "approval_id": approval_id,
        "posted": False,
        "resolution": resolution,
    }
    audit_id = await write_decision_audit(
        session,
        workspace_id=workspace_id,
        entity_id=row_entity,
        ctx=ctx,
        tool_name=AUDIT_TOOL_NAME,
        request_payload={
            "bt_id": bt_id,
            "entity_id": str(entity_id) if entity_id else None,
            "journal_entry_id": str(journal_entry_id) if journal_entry_id else None,
            "not_books_relevant_reason": reason,
        },
        response_payload=response,
        reasoning=note,
        outcome=AUDIT_OUTCOME,
    )
    resolution["audit_id"] = str(audit_id)

    if approval_id is not None:
        await session.execute(
            text(
                """
                UPDATE approval_queue
                SET status = CAST('rejected' AS approval_status),
                    decided_by_uc_uid = :uid,
                    decided_at = now(),
                    reasoning = reasoning || ' | ' || :note,
                    payload = payload || CAST(:payload AS jsonb)
                WHERE id = :id AND status = 'pending'
                """
            ),
            {
                "id": approval_id,
                "uid": resolved_by,
                "note": note,
                "payload": json.dumps({"blocked_resolution": resolution}),
            },
        )

    await session.execute(
        text(
            """
            UPDATE connector_state
            SET state = CAST(:state AS jsonb)
            WHERE id = :id
            """
        ),
        {"id": row["id"], "state": json.dumps(resolved_state(state, bt_id, resolution))},
    )
    response["audit_id"] = str(audit_id)
    return response
