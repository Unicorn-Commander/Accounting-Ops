"""MCP read tools for the financial-report surface.

The HTTP handlers in ``app/api/reports.py`` are the source of truth for the
SQL + sign conventions. The MCP layer wraps them by:

  1. Calling ``authorize_workspace`` for token-bound auth + entity-scope check.
  2. Building a ``WorkspaceContext`` (returned by ``authorize_workspace``).
  3. Invoking the HTTP handler's coroutine directly with the resolved context.

This keeps ONE implementation of the report math but enforces auth the MCP way.

Tools:
  - get_balance_sheet    — assets/liabilities/equity as of a date
  - get_cash_flow        — operating/investing/financing for a period (v2 classifier)
  - get_trial_balance    — debits/credits by COA account for a date
  - get_1099_nec_report  — year-end NEC report, payees >= $600

All four are read-only with viewer floor (same as HTTP).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.mcp_auth import authorize_workspace


def _entity_probe(entity_id: str | None) -> UUID | str | None:
    """Translate the entity_id argument to a scope-probe value.

    The HTTP report handlers accept entity_id as either a UUID string,
    'consolidated', 'unmapped', or omitted. For MCP authorization we only need
    to enforce entity_scope when a specific UUID was provided; the consolidated
    case requires unscoped membership (admin/owner) — but the HTTP handler
    already enforces that, so we leave it alone here.
    """
    if not entity_id:
        return None
    if entity_id in ("consolidated", "unmapped"):
        return None
    try:
        return UUID(entity_id)
    except ValueError:
        return None


async def get_balance_sheet(
    workspace_id: UUID,
    as_of: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Balance sheet for an entity (or consolidated) as of a date.

    Mirrors GET /api/workspaces/{wid}/reports/balance-sheet.

    Assets = debits − credits on debit-normal accounts; Liabilities + Equity =
    credits − debits on credit-normal accounts. Retained-earnings (revenue −
    expenses) is computed and appended to Equity.

    Args:
        workspace_id: Workspace UUID.
        as_of: Optional ISO YYYY-MM-DD; defaults to today.
        entity_id: Optional — UUID string or omit/'consolidated' for workspace-wide.

    Returns:
        {"as_of": "...", "entity_id": "...", "entity_label": "...",
         "currency": "USD",
         "assets": {"lines": [...], "total": "..."},
         "liabilities": {"lines": [...], "total": "..."},
         "equity": {"lines": [...], "total": "..."},
         "balanced": bool, "drift": "..."}

    Role floor: viewer.
    """
    workspace = await authorize_workspace(
        workspace_id, _entity_probe(entity_id), min_role="viewer"
    )
    from app.api.reports import balance_sheet as _http_balance_sheet
    return await _http_balance_sheet(
        workspace_id=workspace_id,
        as_of=as_of,
        entity_id=entity_id,
        workspace=workspace,
    )


async def get_cash_flow(
    workspace_id: UUID,
    period: str = "ytd",
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Cash-flow statement (operating/investing/financing) for a period.

    Mirrors GET /api/workspaces/{wid}/reports/cash-flow. Uses the v2 classifier
    (chart_of_account.account_type for the counter account).

    Args:
        workspace_id: Workspace UUID.
        period: 'mtd' | 'qtd' | 'ytd' | 'YYYY-MM' | 'YYYY-Qn' | 'YYYY' |
            'YYYY-MM-DD..YYYY-MM-DD'.
        entity_id: Optional — UUID string or omit for consolidated.

    Returns:
        {"period": "...", "start_date": "...", "end_date": "...",
         "entity_id": "...", "entity_label": "...", "currency": "USD",
         "version": "v2",
         "operating": {"lines": [...], "total": "..."},
         "investing": {"lines": [...], "total": "..."},
         "financing": {"lines": [...], "total": "..."},
         "net_change_in_cash": "..."}

    Role floor: viewer.
    """
    workspace = await authorize_workspace(
        workspace_id, _entity_probe(entity_id), min_role="viewer"
    )
    from app.api.reports import cash_flow as _http_cash_flow
    return await _http_cash_flow(
        workspace_id=workspace_id,
        period=period,
        entity_id=entity_id,
        workspace=workspace,
    )


async def get_trial_balance(
    workspace_id: UUID,
    as_of: str | None = None,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Trial balance as of a date.

    Mirrors GET /api/workspaces/{wid}/reports/trial-balance. One row per active
    COA account with non-zero activity, total_debit + total_credit per account,
    plus grand totals + the balanced bool.

    Excludes source IN ('reversal', 'recategorization') so reversal/restatement
    pairs don't double-count.

    Args:
        workspace_id: Workspace UUID.
        as_of: Optional ISO YYYY-MM-DD; defaults to today.
        entity_id: Optional — UUID string or omit for consolidated.

    Returns:
        {"as_of": "...", "entity_id": "...", "entity_label": "...",
         "currency": "USD",
         "accounts": [
            {"account_number": "1010", "canonical_form": "...",
             "account_type": "asset",
             "total_debit": "...", "total_credit": "...",
             "net_balance": "..."}, ...],
         "grand_total_debit": "...", "grand_total_credit": "...",
         "balanced": bool}

    Role floor: viewer.
    """
    workspace = await authorize_workspace(
        workspace_id, _entity_probe(entity_id), min_role="viewer"
    )
    from app.api.reports import trial_balance as _http_trial_balance
    return await _http_trial_balance(
        workspace_id=workspace_id,
        as_of=as_of,
        entity_id=entity_id,
        workspace=workspace,
    )


async def get_1099_nec_report(
    workspace_id: UUID,
    year: str,
    entity_id: str | None = None,
) -> dict[str, Any]:
    """Year-end 1099-NEC report: payees paid >= $600 in nonemployee compensation.

    Mirrors GET /api/workspaces/{wid}/reports/1099-nec. Aggregates the DEBIT
    side of posted journal entries whose debit account is a contract-labor
    account (6300 today), grouped by counterparty. Counterparties whose name
    matches a known payment processor (Stripe, PayPal, etc.) are flagged
    ``requires_human_review`` (1099-K territory).

    DRAFT — for CPA review only. Auto-determination of corporate status and
    payment-method exclusions is NOT performed here.

    Args:
        workspace_id: Workspace UUID.
        year: 4-digit calendar year (e.g. "2024").
        entity_id: Optional — UUID string or omit for consolidated.

    Returns: list of payee aggregates plus a draft notice.

    Role floor: viewer.
    """
    workspace = await authorize_workspace(
        workspace_id, _entity_probe(entity_id), min_role="viewer"
    )
    from app.api.reports import form_1099_nec as _http_1099_nec
    result = await _http_1099_nec(
        workspace_id=workspace_id,
        year=year,
        entity_id=entity_id,
        workspace=workspace,
    )
    if isinstance(result, dict):
        result["_draft_notice"] = (
            "DRAFT — for CPA review only. Payment-method exclusions and "
            "corporate-status exemptions are NOT applied. Flagged payees "
            "(requires_human_review) need human confirmation before filing."
        )
    return result
