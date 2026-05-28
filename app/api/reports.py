"""Financial reports (P&L, balance sheet, cash flow, trial balance, 1099-NEC).

  GET /api/workspaces/{wid}/reports/pl              — Profit & Loss
  GET /api/workspaces/{wid}/reports/balance-sheet   — Balance Sheet (as-of)
  GET /api/workspaces/{wid}/reports/cash-flow       — Cash Flow
  GET /api/workspaces/{wid}/reports/trial-balance   — Trial Balance (as-of)
  GET /api/workspaces/{wid}/reports/1099-nec        — 1099-NEC contractors >= $600

pl / balance-sheet / cash-flow accept period and entity_id query params:
  period:    "YYYY", "YYYY-Q1..Q4", "YYYY-MM", "YYYY-MM-DD..YYYY-MM-DD",
             "mtd", "qtd", "ytd"
  entity_id: UUID or "consolidated" / "all" (default: consolidated)
1099-nec takes a calendar `year` ("YYYY") + entity_id instead of period.

Account-type classification uses the chart_of_account.account_type column
(asset / liability / equity / revenue / cogs / expense / other_income /
other_expense / tax). Joining via canonical_form lets us classify both
debit and credit sides of each entry.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
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


def _entity_filter(
    entity_id: str | None,
    scope_restriction: tuple[UUID, ...] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the SQL entity predicate for a report query.

    `scope_restriction` comes from assert_entity_in_scope: when a *scoped*
    member requests a consolidated/all view it is the tuple of entity UUIDs
    they may see, so we restrict the (otherwise unfiltered) query to that set.
    For a concrete in-scope entity_id the normal equality predicate is used and
    scope_restriction is None (nothing extra to add). Unscoped members always
    pass scope_restriction=None and get the original behavior.
    """
    if not entity_id or entity_id in ("consolidated", "all"):
        if scope_restriction is not None:
            return (
                " AND je.entity_id = ANY(:entity_scope)",
                {"entity_scope": [str(e) for e in scope_restriction]},
            )
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


async def compute_profit_and_loss(
    workspace: WorkspaceContext,
    period: str,
    entity_id: str | None = None,
    restrict_entity_ids: list[UUID] | None = None,
) -> dict:
    """Compute a Profit & Loss for the period over the posted journal.

    Single implementation shared by the HTTP route (``profit_and_loss``) and the
    MCP tools (``get_p_and_l`` / ``get_consolidated_p_and_l``) so the two
    surfaces can never produce different numbers. The caller is responsible for
    role enforcement (``require_role``); this function still applies the
    membership's entity_scope via ``assert_entity_in_scope`` as defense in depth.

    Revenue accounts (4xxx, 7xxx) net credits-minus-debits (normal balance =
    credit); expense accounts (5xxx, 6xxx, 8xxx, 9xxx) net debits-minus-credits
    (normal balance = debit). Net income = revenue - expenses.

    ``restrict_entity_ids`` (consolidated subset): when provided, the report is
    restricted to exactly those entity ids (``je.entity_id = ANY(...)``) and
    ``entity_id`` is treated as consolidated. The ids are intersected with the
    caller's entity_scope when the membership is scoped.
    """
    if restrict_entity_ids is not None:
        # Intersect the requested subset with the caller's allowed scope.
        scope_restriction = assert_entity_in_scope(workspace, None)
        if scope_restriction is not None:
            allowed = set(scope_restriction)
            restrict_entity_ids = [e for e in restrict_entity_ids if e in allowed]
        entity_clause = " AND je.entity_id = ANY(:entity_scope)"
        entity_params = {"entity_scope": [str(e) for e in restrict_entity_ids]}
    else:
        scope_restriction = assert_entity_in_scope(workspace, entity_id)
        entity_clause, entity_params = _entity_filter(entity_id, scope_restriction)
    start, end, label = _parse_period(period)

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


@router.get("/api/workspaces/{workspace_id}/reports/pl")
async def profit_and_loss(
    workspace_id: UUID,
    period: str = "ytd",
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Profit & Loss for the period. See ``compute_profit_and_loss``."""
    require_role(workspace, "viewer")
    return await compute_profit_and_loss(workspace, period, entity_id)


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
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    entity_clause, entity_params = _entity_filter(entity_id, scope_restriction)

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
        "balanced": drift == Decimal(0),
        "drift": str(drift),
    }


# ---------------------------------------------------------------------------
# Cash-flow v2 classifier helpers
# ---------------------------------------------------------------------------
#
# v2 classification rules (applied in order; first match wins):
#
#   investing:  counter account_type = 'asset'
#               AND account_number starts with '15' (Fixed Assets / Accum. Depr.)
#               This covers Fixed Assets (1500) and Accumulated Depreciation
#               (1550) today; as the COA adds more 15xx intangible-asset lines
#               they automatically fall into the investing bucket because the
#               type='asset' + '15' prefix check stays accurate without edits.
#
#   financing:  counter account_type = 'equity'  (3xxx accounts), OR
#               counter account_type = 'liability' AND account_number = '2400'
#               (Notes Payable — long-term debt indicator), OR
#               counter account_number IN ('6900', '7900')
#               (inter-entity transfer accounts).
#
#   operating:  everything else (accounts payable 2xxx, A/R 1xxx short-term,
#               revenue 4xxx, expense 6xxx, etc.).
#
# v1 fallback: if the COA JOIN yields no account_type for the counter (NULL),
# the v1 prefix logic is used so existing behaviour is preserved for any
# legacy entries whose counter canonical_form doesn't match a live COA row.


# Account-number prefixes / exact numbers used by the v2 rules.
_INVESTING_ACCOUNT_PREFIX = "15"  # Fixed Assets + Accumulated Depreciation
_FINANCING_DEBT_ACCOUNT = "2400"   # Notes Payable (long-term debt indicator)
_INTER_ENTITY_ACCOUNTS = frozenset({"6900", "7900"})  # inter-entity transfers


def _classify_cash_flow_v2(
    counter_number: str,
    counter_type: str | None,
) -> str:
    """v2 cash-flow bucket for a counter-account number + account_type.

    Returns 'investing', 'financing', or 'operating'.

    Rules (first match wins):
      investing  — type='asset' AND number starts with '15'
      financing  — type='equity', OR
                   type='liability' AND number='2400' (Notes Payable), OR
                   number in inter-entity set (6900/7900)
      operating  — everything else

    Falls back to v1 prefix logic when counter_type is None (COA lookup miss).
    """
    if counter_type is not None:
        # --- investing: fixed assets / accumulated depreciation ---
        if counter_type == "asset" and counter_number.startswith(_INVESTING_ACCOUNT_PREFIX):
            return "investing"
        # --- financing: equity, long-term debt, inter-entity transfers ---
        if counter_type == "equity":
            return "financing"
        if counter_type == "liability" and counter_number == _FINANCING_DEBT_ACCOUNT:
            return "financing"
        if counter_number in _INTER_ENTITY_ACCOUNTS:
            return "financing"
        # --- everything else: operating ---
        return "operating"

    # v1 fallback (COA row not found for counter)
    counter_kind = _account_type_prefix(counter_number)
    if counter_kind == "equity" or counter_number == _FINANCING_DEBT_ACCOUNT:
        return "financing"
    if counter_number in ("1500", "1550"):
        return "investing"
    return "operating"


@router.get("/api/workspaces/{workspace_id}/reports/cash-flow")
async def cash_flow(
    workspace_id: UUID,
    period: str = "ytd",
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Cash-flow statement: change in cash accounts (10xx) split into
    operating / investing / financing.

    v2 classifier (version: "v2") uses chart_of_account.account_type to
    classify the counter account accurately:

      investing:  counter type='asset' AND account_number starts with '15'
                  (Fixed Assets, Accumulated Depreciation, future 15xx intangibles)
      financing:  counter type='equity' (3xxx), OR
                  type='liability' AND account='2400' (Notes Payable), OR
                  inter-entity transfers (6900, 7900)
      operating:  everything else

    v1 prefix fallback is used when the COA JOIN yields no account_type for
    a counter account (preserves behaviour for legacy entries).
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)
    start, end, label = _parse_period(period)
    entity_clause, entity_params = _entity_filter(entity_id, scope_restriction)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entity_label = await _entity_label(session, entity_id)
        # v2: JOIN chart_of_account on the counter canonical_form's account_number
        # to get account_type for classification.  LEFT JOIN so entries whose
        # counter isn't in the live COA still appear (handled by v1 fallback).
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH cash_sides AS (
                        -- Cash inflows: entries where the debit side is a cash account
                        SELECT
                            debit_account AS canonical_form,
                            amount        AS in_flow,
                            0::numeric    AS out_flow,
                            credit_account AS counter
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date >= :start
                          AND effective_date <= :end
                          AND debit_account LIKE '10%'
                          {entity_clause}
                        UNION ALL
                        -- Cash outflows: entries where the credit side is a cash account
                        SELECT
                            credit_account AS canonical_form,
                            0::numeric     AS in_flow,
                            amount         AS out_flow,
                            debit_account  AS counter
                        FROM journal_entry je
                        WHERE status = 'posted'
                          AND effective_date >= :start
                          AND effective_date <= :end
                          AND credit_account LIKE '10%'
                          {entity_clause}
                    )
                    SELECT
                        cs.canonical_form,
                        cs.counter,
                        SUM(cs.in_flow)  AS sum_in,
                        SUM(cs.out_flow) AS sum_out,
                        -- v2: pull account_type from COA for the counter account
                        coa.account_type AS counter_type
                    FROM cash_sides cs
                    LEFT JOIN chart_of_account coa
                      ON coa.account_number = split_part(cs.counter, '·', 1)
                     AND coa.is_active = true
                    GROUP BY cs.canonical_form, cs.counter, coa.account_type
                    ORDER BY cs.canonical_form, cs.counter
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
        counter_number = counter.split("·", 1)[0] if "·" in counter else counter
        counter_type: str | None = r.get("counter_type")  # None when COA JOIN misses
        bucket = _classify_cash_flow_v2(counter_number, counter_type)
        line = {
            "cash_account": r["canonical_form"],
            "counter_account": counter,
            "amount": str(net),
        }
        if bucket == "investing":
            investing.append(line)
            inv_total += net
        elif bucket == "financing":
            financing.append(line)
            fin_total += net
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
        "version": "v2",
        "operating": {"lines": operating, "total": str(op_total)},
        "investing": {"lines": investing, "total": str(inv_total)},
        "financing": {"lines": financing, "total": str(fin_total)},
        "net_change_in_cash": str(op_total + inv_total + fin_total),
    }


# ---------------------------------------------------------------------------
# Trial Balance
# ---------------------------------------------------------------------------
#
# A trial balance is the double-entry integrity check: for every posted entry
# the debit side equals the credit side, so the grand total of all debits MUST
# equal the grand total of all credits.  It lists one row per active
# chart_of_account entry that has non-zero activity up to as_of.
#
# Sign convention (normal-balance notation):
#   asset/expense:            normal balance = debit  → positive net = normal
#   liability/equity/revenue: normal balance = credit → positive net = inverted
# net_balance = total_debit − total_credit for every row.
#
# Exclusions (same as the cash-recon):
#   source IN ('reversal', 'recategorization') — these are the accounting
#   correction appendages; they cancel the original entry when included, but
#   for a plain trial balance we follow the cash-recon convention and exclude
#   them so a reversal/restatement pair doesn't double-count.
#   NOTE: the spec says "excluding source IN ('reversal','recategorization')"
#   matches the cash-recon exclusion.  Discuss with lead if all-entries view
#   is desired for a future "include_corrections" flag.


@router.get("/api/workspaces/{workspace_id}/reports/trial-balance")
async def trial_balance(
    workspace_id: UUID,
    as_of: str | None = None,
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Trial balance as of a date.  Defaults to today.

    Returns one row per active chart_of_account with non-zero activity up to
    as_of.  Each row:
      account_number, canonical_form, account_type,
      total_debit, total_credit, net_balance (debit − credit).

    Grand totals (grand_total_debit, grand_total_credit) and a balanced bool
    (total debits == total credits, the fundamental double-entry check) are
    included.  Role floor: viewer.  Entity-scope honored.

    Exclusion: entries with source IN ('reversal', 'recategorization') are
    excluded — they are accounting-correction appendages that cancel in pairs;
    including them would double-count corrections.  This matches the cash-recon
    exclusion convention.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)
    as_of_date = date.fromisoformat(as_of) if as_of else date.today()
    entity_clause, entity_params = _entity_filter(entity_id, scope_restriction)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entity_label = await _entity_label(session, entity_id)

        # Aggregate debits and credits per COA account across all posted entries
        # up to as_of, excluding reversal/recategorization correction sources.
        # We join chart_of_account via account_number prefix extracted from
        # canonical_form (split_part on '·').  Only active COA rows with
        # non-zero activity are returned (HAVING filters zero-net rows).
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH activity AS (
                        -- Debit side of each posted entry
                        SELECT
                            split_part(je.debit_account, '·', 1) AS account_number,
                            je.amount AS debit_amount,
                            0::numeric               AS credit_amount
                        FROM journal_entry je
                        WHERE je.status = 'posted'
                          AND je.effective_date <= :as_of
                          AND je.source NOT IN ('reversal', 'recategorization')
                          {entity_clause}
                        UNION ALL
                        -- Credit side of each posted entry
                        SELECT
                            split_part(je.credit_account, '·', 1) AS account_number,
                            0::numeric               AS debit_amount,
                            je.amount                AS credit_amount
                        FROM journal_entry je
                        WHERE je.status = 'posted'
                          AND je.effective_date <= :as_of
                          AND je.source NOT IN ('reversal', 'recategorization')
                          {entity_clause}
                    ),
                    aggregated AS (
                        SELECT
                            a.account_number,
                            SUM(a.debit_amount)  AS total_debit,
                            SUM(a.credit_amount) AS total_credit
                        FROM activity a
                        GROUP BY a.account_number
                        HAVING SUM(a.debit_amount) != 0 OR SUM(a.credit_amount) != 0
                    )
                    SELECT
                        agg.account_number,
                        coa.canonical_form,
                        coa.account_type,
                        agg.total_debit,
                        agg.total_credit
                    FROM aggregated agg
                    JOIN chart_of_account coa
                      ON coa.account_number = agg.account_number
                     AND coa.is_active = true
                    ORDER BY agg.account_number
                    """
                ),
                {"as_of": as_of_date, **entity_params},
            )
        ).mappings().all()

    accounts: list[dict] = []
    grand_debit = Decimal("0")
    grand_credit = Decimal("0")

    for r in rows:
        total_debit = Decimal(str(r["total_debit"] or 0))
        total_credit = Decimal(str(r["total_credit"] or 0))
        net_balance = total_debit - total_credit
        grand_debit += total_debit
        grand_credit += total_credit
        accounts.append(
            {
                "account_number": r["account_number"],
                "canonical_form": r["canonical_form"],
                "account_type": r["account_type"],
                "total_debit": str(total_debit),
                "total_credit": str(total_credit),
                "net_balance": str(net_balance),
            }
        )

    balanced = grand_debit == grand_credit
    return {
        "as_of": as_of_date.isoformat(),
        "entity_label": entity_label,
        "entity_id": entity_id or "consolidated",
        "currency": "USD",
        "accounts": accounts,
        "grand_total_debit": str(grand_debit),
        "grand_total_credit": str(grand_credit),
        "balanced": balanced,
    }


# ---------------------------------------------------------------------------
# 1099-NEC (Nonemployee Compensation)
# ---------------------------------------------------------------------------
#
# IRS rule (Instructions for Forms 1099-MISC and 1099-NEC, rev. 04/2025):
# File a 1099-NEC for each person to whom you paid, in the course of your
# trade or business, at least $600 during the calendar year for *services*
# performed by someone who is NOT your employee (nonemployee compensation).
#
# WHAT COUNTS — account-class decision:
#   NEC is compensation for SERVICES by a contractor — NOT general operating
#   expense. A $5,000 cloud-hosting bill or a SaaS subscription is an expense
#   but it is NOT nonemployee compensation, so summing every 6xxx expense
#   account (let alone every posting, as the prior WIP did) materially
#   over-reports NEC. The chart of accounts (app/journal/accounts.py) gives us
#   a purpose-built bucket: account 6300 "Contractor · 1099-NEC". We therefore
#   aggregate ONLY entries whose DEBIT side lands in a contract-labor account
#   (6300 today). This is the precise, materially-correct basis for the form.
#   CONTRACT_LABOR_ACCOUNTS is a tunable allow-list: if the COA later splits
#   contractor spend across more accounts (e.g. a 63xx range), add them here.
#   We deliberately do NOT fall back to "all expense-kind prefixes" — that
#   would put rent/SaaS/meals/travel on a NEC form, which is wrong.
#
# DIRECTION — debit side only, netted:
#   NEC is money paid OUT to the contractor, which posts to the DEBIT side of
#   the expense account (normal balance = debit). We sum debit-minus-credit on
#   6300 per counterparty so that contractor refunds / reversals (credits back
#   to 6300) correctly reduce the reportable total — mirroring how P&L nets the
#   expense side. Revenue (4xxx credits), transfers, and refunds therefore can
#   never inflate the number.
#
# AGGREGATION — by counterparty:
#   Payee == counterparty.display_name. We JOIN counterparty on counterparty_id
#   and GROUP BY the counterparty, then apply the $600 threshold via HAVING.
#
# WHAT WE DO **NOT** AUTO-EXCLUDE (left to human review, per mission):
#   - Payments to corporations are generally exempt (legal-services fees to a
#     corporation are the notable exception). We do not know payee tax status
#     from the ledger, so we surface everyone and flag for review rather than
#     silently dropping.
#   - Credit-card / third-party-network (PayPal, Stripe, Venmo, ...) payments
#     are reported by the settlement entity on 1099-K and are NOT reportable on
#     1099-NEC by the payer. We cannot reliably infer payment rail from the
#     ledger, so processor-named counterparties are FLAGGED (requires_human_
#     review) rather than auto-excluded. A human confirms before filing.
#
# THRESHOLD NOTE: $600 is the long-standing threshold and is still what the
# IRS 1099-NEC instructions (rev. 04/2025) state. The One Big Beautiful Bill
# Act raises it to $2,000 starting with TY2026 (inflation-indexed thereafter);
# if/when the IRS form instructions adopt that, make NEC_THRESHOLD year-aware.

CONTRACT_LABOR_ACCOUNTS: tuple[str, ...] = ("6300",)
NEC_THRESHOLD = Decimal("600")

# Substrings in a counterparty name that suggest a credit-card / third-party
# payment processor (1099-K territory) — used only to set requires_human_review,
# never to auto-exclude.
_PROCESSOR_KEYWORDS: tuple[str, ...] = (
    "stripe", "paypal", "square", "shopify", "squareup",
    "checkout", "braintree", "adyen", "worldpay", "zelle",
    "venmo", "cashapp", "cash app",
)


def _looks_like_processor(payee_name: str) -> bool:
    """True if the payee name hints at a payment processor (1099-K territory)."""
    low = (payee_name or "").lower()
    return any(kw in low for kw in _PROCESSOR_KEYWORDS)


def _build_1099_nec_payees(rows: list[dict]) -> list[dict]:
    """Turn netted (payee, net_amount, posting_count) rows into the report's
    payee list: enforce the >= $600 NEC threshold, flag processor-mediated
    payees for human review, and sort by amount descending.

    Pure function (no DB) so the threshold + exclusion logic is unit-testable.
    Rows whose netted contractor payment is below the threshold (or <= 0 after
    refunds) are dropped — only reportable NEC remains.
    """
    payees: list[dict] = []
    for r in rows:
        net = Decimal(str(r["net_amount"] or 0))
        if net < NEC_THRESHOLD:
            continue
        payee_name = r["payee"] or "Unknown Payee"
        payees.append(
            {
                "payee": payee_name,
                "total_amount": str(net),
                "posting_count": int(r["posting_count"] or 0),
                "requires_human_review": _looks_like_processor(payee_name),
            }
        )
    payees.sort(key=lambda p: Decimal(p["total_amount"]), reverse=True)
    return payees


@router.get("/api/workspaces/{workspace_id}/reports/1099-nec")
async def form_1099_nec(
    workspace_id: UUID,
    year: str,
    entity_id: str | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """1099-NEC year-end report: counterparties paid >= $600 in nonemployee
    compensation for a given calendar year + entity.

    Aggregates the DEBIT side of posted journal entries whose debit account is
    a contract-labor account (6300 "Contractor · 1099-NEC"), grouped by
    counterparty (= payee). Returns rows of
    {payee, total_amount, posting_count, requires_human_review} where the
    netted total >= $600, sorted descending. See the module-level comment above
    for the full account-class + IRS reasoning.

    NOTE: This endpoint does NOT auto-determine corporate status or
    payment-method exclusions. Payees paid via credit-card / payment processors
    may instead belong on 1099-K; those whose name matches a known processor
    are flagged ``requires_human_review`` for a human to confirm before filing.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)
    # Validate year
    if len(year) != 4 or not year.isdigit():
        raise HTTPException(400, "year must be a 4-digit string like '2025'")
    y = int(year)
    start = date(y, 1, 1)
    end = date(y, 12, 31)
    entity_clause, entity_params = _entity_filter(entity_id, scope_restriction)

    # Allow-list of contract-labor account NUMBERS, expanded into a SQL IN-list
    # of bound params so the query stays injection-safe.
    acct_params = {
        f"nec_acct_{i}": num for i, num in enumerate(CONTRACT_LABOR_ACCOUNTS)
    }
    acct_in = ", ".join(f":{k}" for k in acct_params)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        entity_label = await _entity_label(session, entity_id)
        # Sum debit-minus-credit on the contractor account(s), per counterparty.
        # Both sides are filtered to the contract-labor account so a contractor
        # refund (credit back to 6300) nets against the payments. Counterparty
        # join is the canonical payee.
        rows = (
            await session.execute(
                text(
                    f"""
                    WITH nec_sides AS (
                        SELECT
                            je.counterparty_id AS counterparty_id,
                            je.amount          AS debit,
                            0::numeric         AS credit
                        FROM journal_entry je
                        WHERE je.status = 'posted'
                          AND je.effective_date >= :start
                          AND je.effective_date <= :end
                          AND split_part(je.debit_account, '·', 1) IN ({acct_in})
                          {entity_clause}
                        UNION ALL
                        SELECT
                            je.counterparty_id AS counterparty_id,
                            0::numeric         AS debit,
                            je.amount          AS credit
                        FROM journal_entry je
                        WHERE je.status = 'posted'
                          AND je.effective_date >= :start
                          AND je.effective_date <= :end
                          AND split_part(je.credit_account, '·', 1) IN ({acct_in})
                          {entity_clause}
                    )
                    SELECT
                        coalesce(c.display_name, 'Unknown Payee') AS payee,
                        SUM(s.debit) - SUM(s.credit)              AS net_amount,
                        COUNT(*)                                  AS posting_count
                    FROM nec_sides s
                    LEFT JOIN counterparty c ON c.id = s.counterparty_id
                    GROUP BY c.id, c.display_name
                    HAVING SUM(s.debit) - SUM(s.credit) >= :threshold
                    ORDER BY net_amount DESC
                    """
                ),
                {
                    "start": start,
                    "end": end,
                    "threshold": NEC_THRESHOLD,
                    **acct_params,
                    **entity_params,
                },
            )
        ).mappings().all()

    payees = _build_1099_nec_payees([dict(r) for r in rows])

    return {
        "year": year,
        "entity_label": entity_label,
        "entity_id": entity_id or "consolidated",
        "currency": "USD",
        "threshold": f"{NEC_THRESHOLD:.2f}",
        "basis_accounts": list(CONTRACT_LABOR_ACCOUNTS),
        "payees": payees,
        "total_payees": len(payees),
        "total_paid": str(sum((Decimal(p["total_amount"]) for p in payees), Decimal("0"))),
        "note": "Nonemployee compensation = payments for contractor services "
                "(GL account 6300). This report does not auto-determine "
                "corporate status or payment-method exclusions: payments to "
                "corporations are generally exempt (legal fees excepted), and "
                "credit-card / third-party-network payments belong on 1099-K, "
                "not 1099-NEC. Payees flagged requires_human_review match a "
                "known payment processor — verify the payment rail and the "
                "payee's tax status before filing.",
    }
