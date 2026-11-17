"""Durable human decisions for eBay review items.

The eBay worker holds the connector_state row while it maps one item.  Human
decisions use the same row as a small, per-transaction decision ledger so an
overlapping one-day read cannot recreate a rejected approval or lose an
approval before the worker sees it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


REJECTED_STATE_KEY = "rejected_ebay_items"
APPROVED_STATE_KEY = "approved_ebay_items"


def _state_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def rejected_items(state: dict[str, Any] | None) -> dict[str, Any]:
    state = _state_dict(state)
    value = _state_dict(state.get(REJECTED_STATE_KEY))
    return {key: entry for key, entry in value.items() if isinstance(key, str) and key}


def approved_items(state: dict[str, Any] | None) -> dict[str, Any]:
    state = _state_dict(state)
    value = _state_dict(state.get(APPROVED_STATE_KEY))
    return {key: entry for key, entry in value.items() if isinstance(key, str) and key}


def item_decision(state: dict[str, Any] | None, transaction_id: str) -> str | None:
    """Return ``rejected`` or ``approved`` for one durable decision, if any."""
    if not transaction_id:
        return None
    if transaction_id in rejected_items(state):
        return "rejected"
    if transaction_id in approved_items(state):
        return "approved"
    return None


async def lock_ebay_state_row(
    session: AsyncSession,
    entity_id: UUID,
    instance: str = "default",
) -> tuple[str, dict[str, Any]] | None:
    """Lock and return the eBay connector state row used by the worker."""
    row = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, state
                FROM connector_state
                WHERE entity_id = :eid
                  AND connector_name = 'ebay_finances'
                  AND connector_instance_name = :instance
                FOR UPDATE
                """
            ),
            {"eid": str(entity_id), "instance": instance},
        )
    ).first()
    if row is None:
        return None
    return str(row.id), _state_dict(getattr(row, "state", None))


async def record_ebay_decision(
    session: AsyncSession,
    *,
    entity_id: UUID,
    transaction_id: str,
    approval_id: str,
    decision: str,
    decided_by: str | None,
    instance: str = "default",
) -> None:
    """Merge one decision under the connector state row lock.

    Rejects are durable holds. Approves remove the hold and arm exactly this
    external id for the worker's first-run review bypass. The mapper still gets
    the raw eBay item from the API; no accounting fields are replayed from the
    approval payload.
    """
    if not transaction_id:
        raise ValueError("eBay approval is missing ebay_transaction_id")
    locked = await lock_ebay_state_row(session, entity_id, instance)
    if locked is None:
        raise ValueError("eBay sync state is missing; decision cannot be durable")
    row_id, state = locked
    rejected = rejected_items(state)
    approved = approved_items(state)
    entry = {
        "approval_id": approval_id,
        "decided_by_uc_uid": decided_by,
        "decided_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": "approval_decision",
    }
    if decision == "reject":
        rejected[transaction_id] = entry
        approved.pop(transaction_id, None)
    elif decision == "approve":
        approved[transaction_id] = entry
        rejected.pop(transaction_id, None)
    else:
        raise ValueError(f"unknown eBay decision: {decision}")

    await session.execute(
        text(
            """
            UPDATE connector_state
            SET state = (COALESCE(state, '{}'::jsonb)
                         || CAST(:patch AS jsonb))
            WHERE id = :id
            """
        ),
        {
            "id": row_id,
            "patch": json.dumps({
                REJECTED_STATE_KEY: rejected,
                APPROVED_STATE_KEY: approved,
            }),
        },
    )


__all__ = (
    "APPROVED_STATE_KEY",
    "REJECTED_STATE_KEY",
    "approved_items",
    "item_decision",
    "lock_ebay_state_row",
    "record_ebay_decision",
    "rejected_items",
)
