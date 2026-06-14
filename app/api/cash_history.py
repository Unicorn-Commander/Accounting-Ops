"""Daily ending cash balance series, derived from the posted journal.

Powers the dashboard-v2 "cash ribbon": a real, gap-free 90-day series of
end-of-day cash balances. This replaces the frontend reconstruction in
`web/lib/cash-series.ts buildRealCashSeries`, which walked backward from the
current balance over a capped ≤1000-transaction fetch — for high-volume books
the earliest points drifted as transactions fell outside the cap. Here the
series is computed server-side over the FULL journal, uncapped.

Cash convention matches `app.tools.reporting._cash_position_from_journal`
(the same number the balance sheet and runway report): cash is the 10xx asset
block (account numbers 1000-1099), the daily delta is
(debits to cash) − (credits from cash) on posted entries, bucketed by
`effective_date`. Because we mirror that filter exactly, the final point of the
series reconciles to the canonical journal cash position as of today.

Live-data path:
  /dashboard → /api/workspaces/{wid}/cash/daily-balance → posted journal
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["cash-position"])

_CENTS = Decimal("0.01")


def _q2(value: Decimal) -> float:
    return float(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _build_daily_series(
    opening: Decimal,
    delta_by_day: dict[date, Decimal],
    start: date,
    days: int,
) -> list[float]:
    """Forward-cumulative end-of-day balances over ``days`` from ``start``.

    Ending balance of each day = ``opening`` + Σ daily deltas up to and
    including that day; days with no activity inherit the prior balance. The
    final element equals ``opening`` + Σ(all deltas) — so when ``delta_by_day``
    holds exactly the posted cash movement within the window, the last point
    reconciles to the journal cash position as of the last day.
    """
    series: list[float] = []
    running = opening
    cursor = start
    for _ in range(days):
        running += delta_by_day.get(cursor, Decimal("0"))
        series.append(_q2(running))
        cursor += timedelta(days=1)
    return series


@router.get("/api/workspaces/{workspace_id}/cash/daily-balance")
async def workspace_cash_daily_balance(
    workspace_id: UUID,
    days: int = Query(default=90, ge=1, le=366),
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Daily ending cash balance for a trailing window, from the posted journal.

    Returns `days` end-of-day balances (oldest → newest, last point = today),
    plus the opening balance carried into the window. Every value is real
    ledger movement — days with no cash activity carry the prior balance
    forward, and the series is uncapped (no transaction-fetch limit).

    Optional `entity_id`:
      - omitted / "consolidated" → all entities in scope
      - specific UUID            → only that entity

    Entity-scope (BOLA): a scoped member requesting a specific out-of-scope
    entity gets 403; a scoped member requesting consolidated is RESTRICTED to
    their allowed entities. Unscoped members are unaffected.

    The cash definition (10xx asset block, debit − credit, posted entries by
    effective_date) is identical to the balance sheet / runway cash line, so
    the final point reconciles to the canonical journal cash position.
    """
    require_role(workspace, "viewer")

    concrete = entity_id if entity_id and entity_id not in ("consolidated", "unmapped") else None
    scope_restriction = assert_entity_in_scope(workspace, concrete)

    as_of = date.today()
    start = as_of - timedelta(days=days - 1)

    # Entity filter fragment + params, shared by the opening and per-day queries.
    # journal_entry lives in the workspace schema (search_path is pinned by
    # workspace_session), so workspace isolation is already enforced; we only
    # scope by entity here.
    entity_sql = ""
    params: dict = {}
    if concrete is not None:
        entity_sql = " AND entity_id::text = :eid"
        params["eid"] = concrete
    elif scope_restriction is not None:
        entity_sql = " AND entity_id::text = ANY(:scope)"
        params["scope"] = [str(e) for e in scope_restriction]

    opening_sql = text(
        f"""
        WITH cash_sides AS (
            SELECT amount AS debit, 0::numeric AS credit
            FROM journal_entry
            WHERE status = 'posted'
              AND effective_date < :start
              AND debit_account_number BETWEEN 1000 AND 1099{entity_sql}
            UNION ALL
            SELECT 0::numeric AS debit, amount AS credit
            FROM journal_entry
            WHERE status = 'posted'
              AND effective_date < :start
              AND credit_account_number BETWEEN 1000 AND 1099{entity_sql}
        )
        SELECT COALESCE(SUM(debit) - SUM(credit), 0) AS opening
        FROM cash_sides
        """
    )

    daily_sql = text(
        f"""
        WITH cash_sides AS (
            SELECT effective_date AS d, amount AS debit, 0::numeric AS credit
            FROM journal_entry
            WHERE status = 'posted'
              AND effective_date BETWEEN :start AND :as_of
              AND debit_account_number BETWEEN 1000 AND 1099{entity_sql}
            UNION ALL
            SELECT effective_date AS d, 0::numeric AS debit, amount AS credit
            FROM journal_entry
            WHERE status = 'posted'
              AND effective_date BETWEEN :start AND :as_of
              AND credit_account_number BETWEEN 1000 AND 1099{entity_sql}
        )
        SELECT d, COALESCE(SUM(debit) - SUM(credit), 0) AS net
        FROM cash_sides
        GROUP BY d
        """
    )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        opening_row = (
            await session.execute(opening_sql, {**params, "start": start})
        ).mappings().first()
        daily_rows = (
            await session.execute(daily_sql, {**params, "start": start, "as_of": as_of})
        ).mappings().all()

    opening = Decimal(str(opening_row["opening"])) if opening_row and opening_row["opening"] is not None else Decimal("0")
    delta_by_day: dict[date, Decimal] = {
        r["d"]: Decimal(str(r["net"])) for r in daily_rows if r["net"] is not None
    }

    # Forward cumulative: ending balance of each day = opening + Σ deltas up to
    # and including that day. Days with no activity inherit the prior balance.
    series = _build_daily_series(opening, delta_by_day, start, days)

    return {
        "days": days,
        "start_date": start.isoformat(),
        "as_of": as_of.isoformat(),
        "currency": "USD",
        "opening": _q2(opening),
        "series": series,  # GL ending cash per day, oldest → newest
        "gl_current": series[-1],  # reconciles to journal cash as of today
        "has_history": len(delta_by_day) > 0,
        "active_days": len(delta_by_day),
        "source": "journal",
    }
