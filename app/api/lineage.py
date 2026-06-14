"""Per-number lineage — "where did this number come from, and can I trust it?"

Given a displayed figure (the cash position, a P&L metric, or a single
account), return the posted journal entries that produced it, each with full
provenance (source feed, category, materiality, the agent's reasoning + verdict,
the source-document hash, reversal links) PLUS the per-entry hash-chain verify
status and a reconciling net that matches the displayed figure.

This powers the dashboard's lineage popover (click a number → trace it). The
reverse-filters mirror app/api/reports.py exactly (same account-number ranges,
same status='posted', same entity scope), so the lineage's net reconciles to
the figure the dashboard shows.

Read-only. Auth = viewer + entity-scope, identical to the report endpoints.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.api.reports import _entity_filter, _parse_period
from app.journal.hash_chain import chain_summary, verify_chain
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["lineage"])

# Account-number ranges per metric — must match app/api/reports.py.
_REVENUE_RANGES = [(4000, 4999), (7000, 7999)]
_EXPENSE_RANGES = [(5000, 6999), (8000, 9999)]
_CASH_RANGES = [(1000, 1099)]

# Tools whose decision_audit rows created/changed a journal entry. The link is
# loose (no FK): the entry id lives in the tool call's payload under varying
# keys. Best-effort enrichment on top of the entry's own reasoning_trace /
# model_version (which are reliable). These are the tool_names actually written
# to decision_audit (verified against the write sites): an approved bank import
# stamps response.journal_entry_id; (re)categorization stamps
# restated_entry_id / reversal_entry_id.
_POSTING_TOOLS = ("categorize", "approval_decision", "link_receipt")

_TENANT_TOOLS_SQL = ", ".join(f"'{t}'" for t in _POSTING_TOOLS)


def _range_or(col: str, ranges: list[tuple[int, int]]) -> str:
    """SQL: (col BETWEEN a AND b OR col BETWEEN c AND d). lo/hi are code-controlled ints."""
    return "(" + " OR ".join(f"{col} BETWEEN {lo} AND {hi}" for lo, hi in ranges) + ")"


def _q2(value: Any) -> str:
    d = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    return str(d.quantize(Decimal("0.01")))


def _as_of_or_today(as_of: str | None) -> date:
    if not as_of:
        return date.today()
    try:
        return date.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(400, f"as_of must be an ISO date: {as_of!r}") from exc


@router.get("/api/workspaces/{workspace_id}/lineage")
async def get_lineage(
    workspace_id: UUID,
    metric: str = Query(
        default="account",
        description="cash | revenue | expenses | net_income | account",
    ),
    account: str | None = Query(
        default=None, description="canonical_form, required when metric=account"
    ),
    period: str | None = Query(
        default=None, description="ytd|mtd|qtd|YYYY|YYYY-MM|YYYY-Qn|YYYY-MM-DD..YYYY-MM-DD"
    ),
    as_of: str | None = Query(default=None, description="ISO date for point-in-time metrics (cash)"),
    entity_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Trace a displayed number to its constituent posted journal entries.

    Identify the number with `metric` (+ `account` when metric=account), and a
    period (P&L metrics) or `as_of` (cash / balance). Returns the entries with
    provenance + agent-decision trail + reversal links, a reconciling net, and
    the entity's hash-chain verify status.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)
    entity_sql, entity_params = _entity_filter(entity_id, scope_restriction)

    metric = (metric or "account").strip().lower()

    # --- Resolve the entry predicate, the net expression, and the date window. ---
    point_in_time = metric == "cash"
    if point_in_time:
        as_of_d = _as_of_or_today(as_of)
        date_sql = " AND je.effective_date <= :as_of"
        date_params: dict[str, Any] = {"as_of": as_of_d}
        period_label = f"as of {as_of_d.isoformat()}"
    elif metric == "account" and not period:
        # A bare account drilldown with no period = balance as-of today.
        as_of_d = _as_of_or_today(as_of)
        date_sql = " AND je.effective_date <= :as_of"
        date_params = {"as_of": as_of_d}
        period_label = f"as of {as_of_d.isoformat()}"
    else:
        start_d, end_d, period_label = _parse_period(period or "ytd")
        date_sql = " AND je.effective_date >= :start AND je.effective_date <= :end"
        date_params = {"start": start_d, "end": end_d}

    params: dict[str, Any] = {**entity_params, **date_params}

    # predicate = which entries touch the metric's accounts; net_sql = the
    # reconciling figure (debit/credit netted by the account's normal balance).
    if metric == "cash":
        ranges = _CASH_RANGES
        predicate = f"({_range_or('je.debit_account_number', ranges)} OR {_range_or('je.credit_account_number', ranges)})"
        net_sql = (
            f"COALESCE(SUM(CASE WHEN {_range_or('je.debit_account_number', ranges)} THEN je.amount ELSE 0 END),0)"
            f" - COALESCE(SUM(CASE WHEN {_range_or('je.credit_account_number', ranges)} THEN je.amount ELSE 0 END),0)"
        )
        account_label = "Cash (1000–1099)"
    elif metric == "revenue":
        ranges = _REVENUE_RANGES
        predicate = f"({_range_or('je.debit_account_number', ranges)} OR {_range_or('je.credit_account_number', ranges)})"
        net_sql = (  # revenue is credit-normal
            f"COALESCE(SUM(CASE WHEN {_range_or('je.credit_account_number', ranges)} THEN je.amount ELSE 0 END),0)"
            f" - COALESCE(SUM(CASE WHEN {_range_or('je.debit_account_number', ranges)} THEN je.amount ELSE 0 END),0)"
        )
        account_label = "Revenue"
    elif metric == "expenses":
        ranges = _EXPENSE_RANGES
        predicate = f"({_range_or('je.debit_account_number', ranges)} OR {_range_or('je.credit_account_number', ranges)})"
        net_sql = (  # expenses are debit-normal
            f"COALESCE(SUM(CASE WHEN {_range_or('je.debit_account_number', ranges)} THEN je.amount ELSE 0 END),0)"
            f" - COALESCE(SUM(CASE WHEN {_range_or('je.credit_account_number', ranges)} THEN je.amount ELSE 0 END),0)"
        )
        account_label = "Expenses"
    elif metric == "net_income":
        rev_d = _range_or("je.debit_account_number", _REVENUE_RANGES)
        rev_c = _range_or("je.credit_account_number", _REVENUE_RANGES)
        exp_d = _range_or("je.debit_account_number", _EXPENSE_RANGES)
        exp_c = _range_or("je.credit_account_number", _EXPENSE_RANGES)
        predicate = f"({rev_d} OR {rev_c} OR {exp_d} OR {exp_c})"
        # net income = (revenue credits − debits) − (expense debits − credits)
        net_sql = (
            f"(COALESCE(SUM(CASE WHEN {rev_c} THEN je.amount ELSE 0 END),0)"
            f" - COALESCE(SUM(CASE WHEN {rev_d} THEN je.amount ELSE 0 END),0))"
            f" - (COALESCE(SUM(CASE WHEN {exp_d} THEN je.amount ELSE 0 END),0)"
            f" - COALESCE(SUM(CASE WHEN {exp_c} THEN je.amount ELSE 0 END),0))"
        )
        account_label = "Net income (revenue − expenses)"
    elif metric == "account":
        if not account:
            raise HTTPException(400, "account is required when metric=account")
        predicate = "(je.debit_account = :account OR je.credit_account = :account)"
        params["account"] = account
        # Normal balance from the account-number prefix (1/5/6/8/9 debit-normal).
        try:
            num = int(account.split("·", 1)[0])
        except (ValueError, AttributeError):
            num = 0
        debit_normal = str(num)[:1] in ("1", "5", "6", "8", "9")
        side_d = "COALESCE(SUM(CASE WHEN je.debit_account = :account THEN je.amount ELSE 0 END),0)"
        side_c = "COALESCE(SUM(CASE WHEN je.credit_account = :account THEN je.amount ELSE 0 END),0)"
        net_sql = f"{side_d} - {side_c}" if debit_normal else f"{side_c} - {side_d}"
        account_label = account
    else:
        raise HTTPException(400, f"Unknown metric '{metric}'")

    base_where = f"je.status = 'posted'{entity_sql} AND {predicate}{date_sql}"

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # 1) Reconciling net + total count (unpaginated).
        agg = (
            await session.execute(
                text(f"SELECT COUNT(*) AS n, ({net_sql}) AS net FROM journal_entry je WHERE {base_where}"),
                params,
            )
        ).mappings().first()
        entry_count = int(agg["n"]) if agg else 0
        net = _q2(agg["net"] if agg and agg["net"] is not None else 0)

        # 2) The contributing entries (paginated, newest first) + provenance.
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT je.id::text AS id,
                           je.entity_id::text AS entity_id,
                           e.display_name AS entity_display_name,
                           je.effective_date, je.posted_at,
                           je.amount, je.currency_code,
                           je.debit_account, je.credit_account,
                           je.category, je.materiality_tier, je.status,
                           je.source, je.source_external_id, je.description, je.payee, je.memo,
                           je.reasoning_trace, je.model_version, je.decision_verdict,
                           je.source_document_id::text AS source_document_id,
                           je.source_document_hash,
                           je.reverses_entry_id::text AS reverses_entry_id,
                           je.approving_human_uc_uid
                    FROM journal_entry je
                    LEFT JOIN entity e ON e.id = je.entity_id
                    WHERE {base_where}
                    ORDER BY je.effective_date DESC, je.posted_at DESC, je.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": limit, "offset": offset},
            )
        ).mappings().all()

        entries = [dict(r) for r in rows]
        je_ids = [e["id"] for e in entries]

        # 3) Best-effort agent-decision trail: link each entry to the agent tool
        #    call that created it (journal_entry_id lives in the tool payload —
        #    no FK, so this is enrichment on top of the entry's own columns).
        decisions: dict[str, dict[str, Any]] = {}
        if je_ids:
            da_rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT DISTINCT ON (je_id)
                               je_id, decision_audit_id, tool_name, model_version,
                               reasoning_trace, outcome, created_at
                        FROM (
                            SELECT id::text AS decision_audit_id, tool_name, model_version,
                                   reasoning_trace, outcome, created_at,
                                   COALESCE(response_payload->>'journal_entry_id',
                                            response_payload->>'restated_entry_id',
                                            response_payload->>'reversal_entry_id') AS je_id
                            FROM decision_audit
                            WHERE tool_name IN ({_TENANT_TOOLS_SQL})
                        ) da
                        WHERE da.je_id = ANY(:je_ids)
                        ORDER BY je_id, created_at DESC
                        """
                    ),
                    {"je_ids": je_ids},
                )
            ).mappings().all()
            for d in da_rows:
                decisions[d["je_id"]] = {
                    "decision_audit_id": d["decision_audit_id"],
                    "tool_name": d["tool_name"],
                    "model_version": d["model_version"],
                    "reasoning_trace": d["reasoning_trace"],
                    "outcome": d["outcome"],
                    "created_at": d["created_at"].isoformat() if d["created_at"] else None,
                }

        # 4) Hash-chain verification (per entity) — only for a single concrete
        #    entity (a per-entity chain). For consolidated we skip (no single chain).
        chain: dict[str, Any] = {"available": False}
        if entity_id and entity_id not in ("consolidated", "all"):
            try:
                tampered = await verify_chain(session, str(entity_id))
                summary = chain_summary(tampered)
                tampered_ids = {str(t.get("entry_id")) for t in summary.get("tampered", [])}
                chain = {"available": True, "ok": summary.get("ok"), "tampered_count": summary.get("tampered_count", 0)}
                # Mark any contributing entry that is itself tampered.
                for e in entries:
                    e["chain_ok"] = e["id"] not in tampered_ids
            except Exception:  # noqa: BLE001 — chain verify is a nice-to-have
                chain = {"available": False}

    # Assemble per-entry payload: provenance + decision trail + reversal.
    out_entries = []
    for e in entries:
        out_entries.append(
            {
                "id": e["id"],
                "entity_id": e["entity_id"],
                "entity_display_name": e["entity_display_name"],
                "effective_date": e["effective_date"].isoformat() if e["effective_date"] else None,
                "posted_at": e["posted_at"].isoformat() if e["posted_at"] else None,
                "amount": _q2(e["amount"]),
                "currency_code": e["currency_code"],
                "debit_account": e["debit_account"],
                "credit_account": e["credit_account"],
                "category": e["category"],
                "materiality_tier": e["materiality_tier"],
                "status": e["status"],
                "source": e["source"],
                "source_external_id": e["source_external_id"],
                "description": e["description"],
                "payee": e["payee"],
                "memo": e["memo"],
                # Decision trail — from the entry's own columns (reliable), plus
                # the linked agent tool call (best-effort).
                "reasoning_trace": e["reasoning_trace"],
                "model_version": e["model_version"],
                "decision_verdict": e["decision_verdict"],
                "approving_human_uc_uid": e["approving_human_uc_uid"],
                "agent_decision": decisions.get(e["id"]),
                # Source-document evidence.
                "source_document_id": e["source_document_id"],
                "source_document_hash": e["source_document_hash"],
                # Reversal link (this entry reverses another).
                "reverses_entry_id": e["reverses_entry_id"],
                "chain_ok": e.get("chain_ok"),
            }
        )

    return {
        "metric": metric,
        "account": account if metric == "account" else None,
        "account_label": account_label,
        "period_label": period_label,
        "entity_id": entity_id,
        "summary": {
            "net": net,
            "entry_count": entry_count,
            "returned": len(out_entries),
            "chain": chain,
        },
        "entries": out_entries,
    }
