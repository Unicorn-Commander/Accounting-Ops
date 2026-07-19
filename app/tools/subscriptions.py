"""MCP tool (read-only): detect_subscriptions — recurring subscription charges.

The model-agnostic detector lives in ``app.subscriptions.detect`` (pure, no DB).
This wrapper is the thin DB layer: it authorizes the caller against the
workspace/entity, pulls the posted journal for the entity over a lookback
window (by ``effective_date``), hands the rows to the pure detector, and shapes
the result for the accountant agent.

READ-ONLY: this surface analyzes the journal so the accountant can flag what to
cancel. It writes nothing and cancels nothing — cancellation is a separate
browser agent.

Follows the ``app.tools.reporting`` structure: authorize_workspace(min_role=
"viewer") for token-bound entity-scope auth, then ``workspace_session`` for the
search_path/RLS-pinned SELECT.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.subscriptions.detect import detect_subscriptions
from app.workspace_session import workspace_session

#: Lookback bounds (months). Need >= ~3 cadence periods of history to detect a
#: subscription; an annual sub needs multiple years, hence a generous default.
_MIN_LOOKBACK_MONTHS = 1
_MAX_LOOKBACK_MONTHS = 120  # 10 years

#: Columns the detector consumes (plus a few for the merchant-key fallback).
_COLUMNS = (
    "amount, currency_code, effective_date, posted_at, counterparty_id, "
    "category, source, description, payee, memo"
)


def _b(value: Any) -> Any:
    """JSON-safe scalar: stringify Decimals/dates/UUIDs, pass the rest through."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


async def detect_subscriptions_tool(
    workspace_id: UUID,
    entity_id: UUID,
    lookback_months: int = 24,
) -> dict[str, Any]:
    """Detect recurring subscription charges for an entity from the journal.

    Scans the entity's posted journal over the trailing ``lookback_months`` and
    returns the recurring charges that look like cancellable subscriptions
    (>= 3 similar-amount charges at a regular monthly/annual/weekly cadence).
    Owner contributions / distributions / transfers / loans and other internal
    movements are excluded. READ-ONLY — nothing is written or cancelled.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID (entity-scoped; must be in the caller's scope).
        lookback_months: Trailing window to scan, in months (default 24,
            clamped 1..120). Annual subscriptions need a multi-year window.

    Returns:
        ``{"entity_id", "as_of", "lookback_months", "currency",
            "items": [<subscription>, ...],
            "summary": {"count", "total_monthly_estimate", "active_count",
                        "stale_count"}}``

        where each ``items`` entry carries ``merchant, cadence, typical_amount,
        charge_count, first_seen, last_seen, next_expected, funding_source,
        is_active, months_since_last, total_paid`` (see
        ``app.subscriptions.detect.detect_subscriptions``).

        ``total_monthly_estimate`` normalizes each ACTIVE subscription's typical
        charge to a per-month figure (annual / 12, weekly * ~4.33, etc.) so the
        accountant sees the recurring monthly burn at a glance.

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")

    months = max(_MIN_LOOKBACK_MONTHS, min(int(lookback_months), _MAX_LOOKBACK_MONTHS))
    as_of = date.today()
    start = as_of - timedelta(days=30 * months)

    params = {
        "entity_id": str(entity_id),
        "start": start,
        "as_of": as_of,
    }

    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM journal_entry "
                    "WHERE entity_id = :entity_id "
                    "  AND status = 'posted' "
                    "  AND effective_date >= :start "
                    "  AND effective_date <= :as_of "
                    "ORDER BY effective_date ASC"
                ),
                params,
            )
        ).mappings().all()

    entries = [{k: _b(v) for k, v in dict(r).items()} for r in rows]

    items = detect_subscriptions(entries, as_of=as_of)

    # --- summary ------------------------------------------------------- #
    active = [it for it in items if it["is_active"]]
    stale = [it for it in items if not it["is_active"]]

    monthly_factor = {
        "weekly": Decimal("4.333"),     # ~52/12 weeks per month
        "biweekly": Decimal("2.1667"),  # ~26/12
        "monthly": Decimal("1"),
        "quarterly": Decimal(1) / Decimal(3),
        "annual": Decimal(1) / Decimal(12),
    }
    total_monthly = Decimal("0")
    for it in active:
        factor = monthly_factor.get(it["cadence"], Decimal("1"))
        total_monthly += Decimal(it["typical_amount"]) * factor

    return {
        "entity_id": str(entity_id),
        "as_of": as_of.isoformat(),
        "lookback_months": months,
        "currency": "USD",
        "items": items,
        "summary": {
            "count": len(items),
            "total_monthly_estimate": str(total_monthly.quantize(Decimal("0.01"))),
            "active_count": len(active),
            "stale_count": len(stale),
        },
    }
