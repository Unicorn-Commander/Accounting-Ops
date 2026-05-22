"""Financial reports (P&L, balance sheet, cash flow) over journal_entry.

  GET /api/workspaces/{wid}/reports/pl              — Profit & Loss
  GET /api/workspaces/{wid}/reports/balance-sheet   — Balance Sheet (as-of)
  GET /api/workspaces/{wid}/reports/cash-flow       — Cash Flow

All three accept period and entity_id query params:
  period:    "YYYY", "YYYY-Q1..Q4", "YYYY-MM", "YYYY-MM-DD..YYYY-MM-DD",
             "mtd", "qtd", "ytd"
  entity_id: UUID or "consolidated" / "all" (default: consolidated)

Account-type classification uses the chart_of_account.account_type column
(asset / liability / equity / revenue / cogs / expense / other_income /
other_expense / tax). Joining via canonical_form lets us classify both
debit and credit sides of each entry.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text

from app.db import SessionLocal
from app.tenancy import WorkspaceContext, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["reports"])


def _parse_period(period: str, today: date | None = None) -> tuple[date, date, str]:
    """Return (start_date, end_date, display_label) for the period string."""
    today = today or date.today()
    p = period.lower().strip()
    if p == "ytd":
        return date(today.year, 1, 1), today, f"{today.year} YTD"
    if p == "mtd":
        return date(today.year, today.month, 1), today, today.strftime("%Y-%m MTD")
    if p == "qtd":
        q = (today.month - 1) // 3
        return (
            date(today.year, q * 3 + 1, 1),
            today,
            f"{today.year} Q{q + 1} TD",
        )
    # Custom range "YYYY-MM-DD..YYYY-MM-DD"
    if ".." in p:
        a, b = p.split("..", 1)
        try:
            return date.fromisoformat(a), date.fromisoformat(b), f"{a} to {b}"
        except ValueError as e:
            raise HTTPException(400, f"Bad date range '{period}': {e}")
    # Full year "YYYY"
    if len(p) == 4 and p.isdigit():
        y = int(p)
        return date(y, 1, 1), date(y, 12, 31), str(y)
    # Quarter "YYYY-Q1..Q4"
    if "-q" in p and len(p) == 7:
        y, q = p.split("-q")
        y = int(y)
        q = int(q)
        if q < 1 or q > 4:
            raise HTTPException(400, "Quarter must be 1-4")
        start_month = (q - 1) * 3 + 1
        end_month = start_month + 2
        last_day = calendar.monthrange(y, end_month)[1]
        return (
            date(y, start_month, 1),
            date(y, end_month, last_day),
            f"{y} Q{q}",
        )
    # Month "YYYY-MM"
    if len(p) == 7 and "-" in p:
        y, m = p.split("-")
        y, m = int(y), int(m)
        last_day = calendar.monthrange(y, m)[1]
        return (
            date(y, m, 1),
            date(y, m, last_day),
            f"{y}-{m:02d}",
        )
    raise HTTPException(400, f"Unknown period format: '{period}'")


def _account_type_prefix(number: str) -> str:
    """Fallback account-type classification by number prefix."""
    if number.startswith("1"):
        return "asset"
    if number.startswith("2"):
        return "liability"
    if number.startswith("3"):
        return "equity"
    if number.startswith("4"):
        return "revenue"
    if number.startswith("5"):
        return "cogs"
    if number.startswith("6"):
        return "expense"
    if number.startswith("7"):
        return "other_income"
    if number.startswith("8"):
        return "other_expense"
    if number.startswith("9"):
        return "tax"
    return "unknown"


def _entity_filter(entity_id: str | None) -> tuple[str, dict[str, Any]]:
    if not entity_id or entity_id in ("consolidated", "all"):
        return "", {}
    try:
        UUID(entity_id)
    except ValueError as exc:
        raise HTTPException(400, "entity_id must be UUID or 'consolidated'/'all'") from exc
    return " AND je.entity_id = :entity_id", {"entity_id": entity_id}


async def _entity_label(session, entity_id: str | None) -> str:
    if not entity_id or entity_id in ("consolidated", "all"):
        return "Consolidated (all entities)"
    row = (
        await session.execute(
            text("SELECT display_name FROM entity WHERE id = :id"),
            {"id": entity_id},
        )
    ).first()
    return row.display_name if row else entity_id


@router.get("/api/workspaces/{workspace_id}/reports/pl")
async def profit_and_loss(
    workspace_id: UUID,
    period: str = "ytd",
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Profit & Loss for the period.

    Revenue accounts (4xxx, 7xxx) are summed as credits-minus-debits
    (normal balance = credit); expense accounts (5xxx, 6xxx, 8xxx, 9xxx)
    are summed as debits-minus-credits (normal balance = debit).
    Net income = revenue - expenses.
    """
    start, end, label = _parse_period(period)
    entity_clause, entity_params = _entity_filter(entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entity_label = await _entity_label(session, entity_id)
        # Build aggregation by account-number prefix derived from canonical_form
        # (canonical_form starts with the number, e.g. "6210·SaaS · AI").
        # We split on the '·' separator to extract the number.
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH all_sides AS (
                        SELECT
                            split_part(debit_account, '·', 1) AS account_number,
                            debit_account AS canonical_form,
                            amount AS debit,
                            0::numeric AS credit
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date >= :start
                          AND effective_date <= :end
                          {entity_clause}
                        UNION ALL
                        SELECT
                            split_part(credit_account, '·', 1) AS account_number,
                            credit_account AS canonical_form,
                            0::numeric AS debit,
                            amount AS credit
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date >= :start
                          AND effective_date <= :end
                          {entity_clause}
                    )
                    SELECT
                        account_number,
                        canonical_form,
                        SUM(debit) AS sum_debit,
                        SUM(credit) AS sum_credit
                    FROM all_sides
                    GROUP BY account_number, canonical_form
                    ORDER BY account_number
                    """
                ),
                {"start": start, "end": end, **entity_params},
            )
        ).mappings().all()

    revenue_total = Decimal("0")
    expense_total = Decimal("0")
    revenue_lines: list[dict] = []
    expense_lines: list[dict] = []
    for r in rows:
        kind = _account_type_prefix(r["account_number"] or "")
        debit = Decimal(str(r["sum_debit"] or 0))
        credit = Decimal(str(r["sum_credit"] or 0))
        if kind in ("revenue", "other_income"):
            net = credit - debit
            if net == 0:
                continue
            revenue_total += net
            revenue_lines.append(
                {
                    "account_number": r["account_number"],
                    "canonical_form": r["canonical_form"],
                    "amount": str(net),
                    "kind": kind,
                }
            )
        elif kind in ("expense", "cogs", "other_expense", "tax"):
            net = debit - credit
            if net == 0:
                continue
            expense_total += net
            expense_lines.append(
                {
                    "account_number": r["account_number"],
                    "canonical_form": r["canonical_form"],
                    "amount": str(net),
                    "kind": kind,
                }
            )

    return {
        "period": label,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "entity_label": entity_label,
        "entity_id": entity_id or "consolidated",
        "currency": "USD",
        "revenue": {
            "lines": revenue_lines,
            "total": str(revenue_total),
        },
        "expenses": {
            "lines": expense_lines,
            "total": str(expense_total),
        },
        "net_income": str(revenue_total - expense_total),
    }


@router.get("/api/workspaces/{workspace_id}/reports/balance-sheet")
async def balance_sheet(
    workspace_id: UUID,
    as_of: str | None = None,
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Balance sheet as of a date. Defaults to today.

    Assets sum = debits - credits (normal balance = debit).
    Liabilities + Equity sum = credits - debits (normal balance = credit).
    Includes computed retained earnings = sum(revenue) - sum(expenses)
    from beginning-of-time to as_of.
    """
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    entity_clause, entity_params = _entity_filter(entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entity_label = await _entity_label(session, entity_id)
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH all_sides AS (
                        SELECT
                            split_part(debit_account, '·', 1) AS account_number,
                            debit_account AS canonical_form,
                            amount AS debit,
                            0::numeric AS credit
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date <= :as_of
                          {entity_clause}
                        UNION ALL
                        SELECT
                            split_part(credit_account, '·', 1) AS account_number,
                            credit_account AS canonical_form,
                            0::numeric AS debit,
                            amount AS credit
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date <= :as_of
                          {entity_clause}
                    )
                    SELECT
                        account_number,
                        canonical_form,
                        SUM(debit) AS sum_debit,
                        SUM(credit) AS sum_credit
                    FROM all_sides
                    GROUP BY account_number, canonical_form
                    ORDER BY account_number
                    """
                ),
                {"as_of": as_of_date, **entity_params},
            )
        ).mappings().all()

    assets: list[dict] = []
    liabilities: list[dict] = []
    equity: list[dict] = []
    assets_total = Decimal("0")
    liab_total = Decimal("0")
    equity_total = Decimal("0")
    retained_earnings = Decimal("0")

    for r in rows:
        kind = _account_type_prefix(r["account_number"] or "")
        debit = Decimal(str(r["sum_debit"] or 0))
        credit = Decimal(str(r["sum_credit"] or 0))
        if kind == "asset":
            net = debit - credit
            if net == 0:
                continue
            assets_total += net
            assets.append(
                {"canonical_form": r["canonical_form"], "amount": str(net)}
            )
        elif kind == "liability":
            net = credit - debit
            if net == 0:
                continue
            liab_total += net
            liabilities.append(
                {"canonical_form": r["canonical_form"], "amount": str(net)}
            )
        elif kind == "equity":
            net = credit - debit
            if net == 0:
                continue
            equity_total += net
            equity.append(
                {"canonical_form": r["canonical_form"], "amount": str(net)}
            )
        elif kind in ("revenue", "other_income"):
            retained_earnings += credit - debit
        elif kind in ("expense", "cogs", "other_expense", "tax"):
            retained_earnings -= debit - credit

    if retained_earnings != 0:
        equity_total += retained_earnings
        equity.append(
            {
                "canonical_form": "3099·Retained Earnings (computed)",
                "amount": str(retained_earnings),
            }
        )

    drift = assets_total - (liab_total + equity_total)
    return {
        "as_of": as_of_date.isoformat(),
        "entity_label": entity_label,
        "entity_id": entity_id or "consolidated",
        "currency": "USD",
        "assets": {"lines": assets, "total": str(assets_total)},
        "liabilities": {"lines": liabilities, "total": str(liab_total)},
        "equity": {"lines": equity, "total": str(equity_total)},
        "balanced": str(drift) == "0",
        "drift": str(drift),
    }


@router.get("/api/workspaces/{workspace_id}/reports/cash-flow")
async def cash_flow(
    workspace_id: UUID,
    period: str = "ytd",
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Simple cash-flow statement: change in cash accounts (1010, 1011,
    1020) split by operating / investing / financing.

    v1 classification: every non-cash account counter-side maps to
    operating by default. (Investing = changes in 1500/1550 / financing
    = changes in 3xxx / 2400. We'll refine the classifier as the COA
    grows.)
    """
    start, end, label = _parse_period(period)
    entity_clause, entity_params = _entity_filter(entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entity_label = await _entity_label(session, entity_id)
        # Net change in each cash account (10xx) over the period.
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH cash_sides AS (
                        SELECT
                            debit_account AS canonical_form,
                            amount AS in_flow,
                            0::numeric AS out_flow,
                            credit_account AS counter
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date >= :start
                          AND effective_date <= :end
                          AND debit_account LIKE '10%'
                          {entity_clause}
                        UNION ALL
                        SELECT
                            credit_account AS canonical_form,
                            0::numeric AS in_flow,
                            amount AS out_flow,
                            debit_account AS counter
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date >= :start
                          AND effective_date <= :end
                          AND credit_account LIKE '10%'
                          {entity_clause}
                    )
                    SELECT
                        canonical_form,
                        counter,
                        SUM(in_flow) AS sum_in,
                        SUM(out_flow) AS sum_out
                    FROM cash_sides
                    GROUP BY canonical_form, counter
                    ORDER BY canonical_form, counter
                    """
                ),
                {"start": start, "end": end, **entity_params},
            )
        ).mappings().all()

    operating: list[dict] = []
    investing: list[dict] = []
    financing: list[dict] = []
    op_total = Decimal("0")
    inv_total = Decimal("0")
    fin_total = Decimal("0")

    for r in rows:
        in_flow = Decimal(str(r["sum_in"] or 0))
        out_flow = Decimal(str(r["sum_out"] or 0))
        net = in_flow - out_flow
        counter = r["counter"] or ""
        counter_number = counter.split("·", 1)[0] if "·" in counter else ""
        counter_kind = _account_type_prefix(counter_number)
        line = {
            "cash_account": r["canonical_form"],
            "counter_account": counter,
            "amount": str(net),
        }
        if counter_kind in ("equity",) or counter_number == "2400":
            financing.append(line)
            fin_total += net
        elif counter_number == "1500" or counter_number == "1550":
            investing.append(line)
            inv_total += net
        else:
            operating.append(line)
            op_total += net

    return {
        "period": label,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "entity_label": entity_label,
        "entity_id": entity_id or "consolidated",
        "currency": "USD",
        "operating": {"lines": operating, "total": str(op_total)},
        "investing": {"lines": investing, "total": str(inv_total)},
        "financing": {"lines": financing, "total": str(fin_total)},
        "net_change_in_cash": str(op_total + inv_total + fin_total),
    }
