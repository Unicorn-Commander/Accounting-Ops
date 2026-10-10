"""MCP tool: get_period_journal — raw journal entries for a date range.

Used by the 1120 Preparer specialist for line-level tax reasoning: given a
fiscal year period the specialist can inspect the individual journal entries
that underlie each form-line aggregate.

Returns the journal rows in posted order (newest effective_date first), with
the same per-row fields the list_transactions tool surfaces plus category, so
the specialist can reason about the 1120 line placement for individual items.

DRAFT LABEL: Results carry a ``_draft_notice`` in the wrapper so the specialist
always knows this raw data is for analysis, not a final position.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.tools._date_filters import parse_iso_date_filter
from app.workspace_session import entity_session

_MAX_LIMIT = 500

# Columns returned per journal row. Kept to fields the specialist needs for
# tax reasoning: amounts, accounts, category, payee/description, dates.
_COLUMNS = (
    "id, effective_date, posted_at, amount, currency_code, "
    "usd_equivalent_at_event_time, debit_account, credit_account, "
    "category, payee, description, memo, status, materiality_tier"
)


def _b(value: Any) -> Any:
    """JSON-safe scalar: stringify Decimals/dates/UUIDs, pass through the rest."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


async def get_period_journal(
    workspace_id: UUID,
    entity_id: UUID,
    start_date: str,
    end_date: str,
    limit: int = 500,
) -> dict[str, Any]:
    """Return raw posted journal entries for an entity within a date range.

    Designed for the 1120 Preparer specialist to inspect individual transactions
    that underlie a form-line aggregate. Returns up to `limit` rows (max 500)
    ordered by effective_date DESC, posted_at DESC.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID (entity-scoped; must be in caller's scope).
        start_date: ISO 8601 date or datetime (inclusive), e.g. '2023-01-01'.
        end_date: ISO 8601 date or datetime (inclusive), e.g. '2023-12-31'.
        limit: Maximum rows to return (default 500, max 500).

    Returns a dict with ``entries`` (list of journal row projections) and
    ``_draft_notice``.

    DRAFT — for CPA review only.

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")

    clamped_limit = max(1, min(int(limit), _MAX_LIMIT))

    # asyncpg requires datetime.date for the effective_date DATE column; the
    # tool accepts ISO date or datetime strings, so normalize before binding.
    _start = parse_iso_date_filter(start_date, "start_date")
    _end = parse_iso_date_filter(end_date, "end_date")

    params: dict[str, Any] = {
        "entity_id": str(entity_id),
        "start_date": _start,
        "end_date": _end,
        "limit": clamped_limit,
    }

    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, _eid):
        rows = (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM journal_entry "
                    "WHERE entity_id = :entity_id "
                    "  AND status = 'posted' "
                    "  AND effective_date >= :start_date "
                    "  AND effective_date <= :end_date "
                    "ORDER BY effective_date DESC, posted_at DESC "
                    "LIMIT :limit"
                ),
                params,
            )
        ).mappings().all()

    entries = [{k: _b(v) for k, v in dict(r).items()} for r in rows]

    return {
        "entity_id": str(entity_id),
        "start_date": start_date,
        "end_date": end_date,
        "entry_count": len(entries),
        "limit_applied": clamped_limit,
        "entries": entries,
        "_draft_notice": (
            "DRAFT — for CPA review only. "
            "Raw journal entries for specialist analysis. "
            "Not a substitute for professional tax advice."
        ),
    }
