"""MCP reporting tools (read-only) over the posted journal.

Every tool authorizes the CURRENT MCP caller against the requested workspace
(and entity) via ``app.mcp_auth.authorize_workspace`` before touching data — the
caller's token decides what is reachable, never the tool arguments. The P&L math
is the single shared implementation in ``app.api.reports.compute_profit_and_loss``
so the MCP surface and the HTTP report surface can never produce different
numbers.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session


async def get_p_and_l(
    workspace_id: UUID,
    entity_id: UUID,
    period: str,
) -> dict[str, Any]:
    """Generate an entity-scoped P&L view from append-only journal projections.

    `period` is ISO 8601 month/quarter/year (e.g., '2026-05', '2026-Q2', '2026')
    or a relative token ('mtd', 'qtd', 'ytd') or a 'YYYY-MM-DD..YYYY-MM-DD' range.
    Currency is the entity's functional currency.
    """
    from app.api.reports import compute_profit_and_loss

    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    return await compute_profit_and_loss(ctx, period, str(entity_id))


async def _cash_position_from_journal(
    workspace_id: UUID, entity_id: str, as_of: date
) -> Decimal:
    """Cash on hand for one entity as of a date, derived from the posted journal.

    Sums (debits - credits) on cash accounts (asset accounts numbered 10xx —
    the same cash-account convention app.api.reports.cash_flow uses) for posted
    entries with effective_date <= as_of. Journal-derived (not the live bank
    feed) so it is deterministic and test-stable, and consistent with the
    balance-sheet's cash line. Runs inside workspace_session (search_path + RLS).
    """
    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    WITH cash_sides AS (
                        SELECT amount AS debit, 0::numeric AS credit
                        FROM journal_entry
                        WHERE status = 'posted'
                          AND entity_id = :eid
                          AND effective_date <= :as_of
                          AND debit_account LIKE '10%'
                        UNION ALL
                        SELECT 0::numeric AS debit, amount AS credit
                        FROM journal_entry
                        WHERE status = 'posted'
                          AND entity_id = :eid
                          AND effective_date <= :as_of
                          AND credit_account LIKE '10%'
                    )
                    SELECT COALESCE(SUM(debit) - SUM(credit), 0) AS cash
                    FROM cash_sides
                    """
                ),
                {"eid": entity_id, "as_of": as_of},
            )
        ).mappings().first()
    return Decimal(str(row["cash"])) if row and row["cash"] is not None else Decimal("0")


async def get_runway(
    workspace_id: UUID,
    entity_id: UUID,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project entity-scoped cash runway: months of cash left at the current burn.

    Simple, explicitly-documented model (no forecasting, no recurring detection):

        runway_months = current_cash / avg_monthly_net_burn

    where:
      * current_cash       = cash on hand as of `as_of`, summed from the posted
                             journal's cash accounts (10xx), i.e. the same number
                             the balance sheet reports — NOT the live bank feed,
                             so it's deterministic.
      * avg_monthly_net_burn = -(trailing net income) / window_months, using the
                             SHARED P&L computation over the trailing window.
                             Positive when the entity spends more than it earns
                             (burning cash); <= 0 means cash-flow positive.

    `scenario` (all optional):
      * window_months: trailing months to average burn over (default 3, 1..36)
      * as_of:         ISO date the projection is anchored to (default today)

    Returns months_of_runway (null when not burning — runway is effectively
    infinite) plus every input used, so the number is fully auditable.
    """
    from app.api.reports import compute_profit_and_loss

    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")

    s = scenario or {}
    try:
        window_months = int(s.get("window_months", 3))
    except (TypeError, ValueError):
        window_months = 3
    window_months = max(1, min(window_months, 36))
    as_of = date.today()
    if s.get("as_of"):
        try:
            as_of = date.fromisoformat(str(s["as_of"]))
        except ValueError as exc:
            raise ValueError(f"as_of must be an ISO date (YYYY-MM-DD): {s['as_of']!r}") from exc

    # Trailing window [start, as_of]. Approximate a month as 30 days for the
    # window span; the divisor below is the integer month count (exact).
    start = as_of - timedelta(days=30 * window_months)
    period = f"{start.isoformat()}..{as_of.isoformat()}"
    pl = await compute_profit_and_loss(ctx, period, str(entity_id))

    revenue = Decimal(pl["revenue"]["total"])
    expenses = Decimal(pl["expenses"]["total"])
    net_income = revenue - expenses  # negative => burning cash
    avg_monthly_net_income = net_income / Decimal(window_months)
    avg_monthly_net_burn = -avg_monthly_net_income  # positive => burning

    cash = await _cash_position_from_journal(workspace_id, str(entity_id), as_of)

    if avg_monthly_net_burn > 0:
        months_of_runway: str | None = str(
            (cash / avg_monthly_net_burn).quantize(Decimal("0.01"))
        )
        status = "burning"
    else:
        months_of_runway = None  # cash-flow positive: runway not bounded by burn
        status = "cash_flow_positive"

    return {
        "entity_id": str(entity_id),
        "as_of": as_of.isoformat(),
        "currency": "USD",
        "months_of_runway": months_of_runway,
        "status": status,
        "inputs": {
            "current_cash": str(cash),
            "window_months": window_months,
            "window_start": start.isoformat(),
            "window_end": as_of.isoformat(),
            "trailing_revenue": str(revenue),
            "trailing_expenses": str(expenses),
            "trailing_net_income": str(net_income),
            "avg_monthly_net_income": str(avg_monthly_net_income.quantize(Decimal("0.01"))),
            "avg_monthly_net_burn": str(avg_monthly_net_burn.quantize(Decimal("0.01"))),
        },
        "model": (
            "months_of_runway = current_cash / avg_monthly_net_burn; "
            "avg_monthly_net_burn = -(trailing_net_income) / window_months; "
            "current_cash summed from posted journal cash accounts (10xx) as of as_of."
        ),
    }


async def get_consolidated_p_and_l(
    workspace_id: UUID,
    period: str,
    entity_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    """Workspace-level consolidated P&L across multiple entities.

    Requires an admin/owner membership with NO entity_scope restriction
    (enforced by authorize_workspace(require_unscoped=True)). `entity_ids = None`
    consolidates ALL entities in the workspace; a provided list restricts the
    report to exactly those entities.
    """
    from app.api.reports import compute_profit_and_loss

    ctx = await authorize_workspace(
        workspace_id, min_role="admin", require_unscoped=True
    )
    return await compute_profit_and_loss(
        ctx, period, entity_id=None, restrict_entity_ids=entity_ids
    )
