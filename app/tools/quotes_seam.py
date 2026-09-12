"""MCP tools for the Quotes → money seam (Customer-Ops ``quote.accepted``).

  create_invoices_from_quote (accountant) — DRAFT deposit + milestone + first
                              recurring-period invoices, the recurring
                              schedules, and (with a delivery partner + split
                              rule) a DRAFT partner BILL. Idempotent on
                              quote_ref + version. Never issues, never posts.
  run_invoice_schedules      (accountant) — generate the next DRAFT for every
                              schedule that is due. Never posts.
  list_invoice_schedules     (viewer)     — the schedules in a workspace.

Same payload + same service as ``POST /api/workspaces/{wid}/entities/{eid}/quotes/accepted``
(``app.ar.quotes_seam``), so the two surfaces cannot drift. Authorization is
the usual ``authorize_workspace`` membership path (the trusted M2M actor rail
is HTTP-only — see app/api/quotes_seam.py).
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.ar.invoicing import InvoiceError
from app.ar.quotes_seam import (
    QuoteAcceptedPayload,
    QuoteSeamError,
    apply_quote_acceptance,
    generate_due_schedule_invoices,
    list_invoice_schedules as _list_schedules,
)
from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session


def _parse_date(value: str | None, field: str) -> dt.date | None:
    if value is None or value == "":
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD): {value!r}") from exc


async def create_invoices_from_quote(
    workspace_id: UUID,
    entity_id: UUID,
    quote_ref: str,
    customer: dict[str, Any],
    lines: list[dict[str, Any]],
    version: int = 1,
    currency: str = "USD",
    totals: dict[str, Any] | None = None,
    delivery_partner: dict[str, Any] | None = None,
    split_rule: dict[str, Any] | None = None,
    vendor_entity_id: UUID | None = None,
    accepted_at: str | None = None,
    net_terms: int = 30,
) -> dict[str, Any]:
    """Create the DRAFT paper an accepted quote implies. Idempotent on quote_ref + version.

    Creates (never issues, never posts a journal entry):
      * the AR customer (upserted from the Contact-Ops identity),
      * the DEPOSIT draft = Σ (one_time + milestone line amount × payment_split[0] / 100),
      * one draft per remaining payment part ("Due on acceptance of <line label>"),
      * an invoice schedule per recurring line (month|quarter|year) + the first period's draft,
      * with delivery_partner + split_rule: a DRAFT partner BILL (accounts payable) from the
        partner to the vendor of record — hosting in full when hosting_to_partner, else
        percent_net of the fixed scope after direct_costs_total_cents.

    A second call with the same quote_ref + version returns the same ids and creates nothing.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Billing entity UUID (issues the customer invoices).
        quote_ref: Quote number, e.g. "Q-2026-0001".
        customer: {display_name, email?, org?, contact_id?, billing_address?} (Contact-Ops identity).
        lines: [{kind one_time|milestone|recurring|hourly|option|included|excluded, label,
            amount_cents, position, payment_split? [50,50], milestone? {payment_split},
            recurring? {interval month|quarter|year, term_months?}, is_hosting?}].
        version: Accepted version number (idempotency key with quote_ref). Default 1.
        currency: ISO-4217, 2-decimal currencies only. Default USD.
        totals: Customer-Ops computed totals {one_time_total_cents, milestone_total_cents,
            deposit_cents, recurring: {month|quarter|year: cents}} — cross-checked, mismatch refuses.
        delivery_partner: {name, contact_id?, partner_supplier_id?} (AO supplier counterparty id).
        split_rule: {kind percent_net|fee|none, percent?, hosting_to_partner?,
            direct_costs_total_cents?, fee_cents?}.
        vendor_entity_id: Entity that OWES the partner bill (default entity_id).
        accepted_at: ISO date the quote was accepted (issue-date basis; default today).
        net_terms: Net-N days for the drafts' due dates. Default 30.

    Returns: {ar_customer_id, deposit_invoice_id, milestone_invoice_ids[], schedule_ids[],
    partner_invoice_id, invoices[], schedules[], partner_bill, review_required, totals,
    federated: {accounting_ops: done|failed, partner: done|failed|skipped, reason}}.

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")
    wid = ctx.workspace_id
    if vendor_entity_id is not None:
        assert_entity_in_scope(ctx, vendor_entity_id)
    try:
        payload = QuoteAcceptedPayload.model_validate(
            {
                "quote_ref": quote_ref,
                "version": version,
                "customer": customer,
                "currency": currency,
                "lines": lines,
                "totals": totals,
                "delivery_partner": delivery_partner,
                "split_rule": split_rule,
                "vendor_entity_id": str(vendor_entity_id) if vendor_entity_id else None,
                "accepted_at": _parse_date(accepted_at, "accepted_at"),
                "net_terms": net_terms,
            }
        )
    except ValidationError as exc:
        raise ValueError(f"PAYLOAD_INVALID: {exc.errors(include_url=False)}") from exc
    try:
        async with workspace_session(SessionLocal, wid) as session:
            return await apply_quote_acceptance(
                session,
                workspace_id=str(wid),
                entity_id=str(entity_id),
                actor_uc_uid=ctx.user_uc_uid,
                payload=payload,
            )
    except QuoteSeamError as exc:
        raise ValueError(f"{exc.code}: {exc.reason}") from exc
    except InvoiceError as exc:
        raise ValueError(str(exc)) from exc


async def run_invoice_schedules(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    as_of: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Generate the next DRAFT invoice for every recurring schedule that is due.

    A schedule that fell behind catches up one period at a time (bounded). Each
    period's draft is idempotent, so re-running is safe. Periods inside a closed
    accounting period are skipped and reported. Never issues, never posts.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Optional entity UUID to restrict the run.
        as_of: ISO date; schedules due on or before it run (default today).
        limit: Max schedules per run (default 200).

    Returns: {as_of, schedules_due, generated[], skipped[]}.

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")
    wid = ctx.workspace_id
    scope = assert_entity_in_scope(ctx, entity_id)
    if scope is not None and entity_id is None:
        # Scoped member without a concrete entity: run each in-scope entity.
        results = []
        async with workspace_session(SessionLocal, wid) as session:
            for eid in scope:
                results.append(
                    await generate_due_schedule_invoices(
                        session, workspace_id=str(wid), actor_uc_uid=ctx.user_uc_uid,
                        as_of=_parse_date(as_of, "as_of"), entity_id=str(eid), limit=limit,
                    )
                )
        return {
            "as_of": results[0]["as_of"] if results else as_of,
            "schedules_due": sum(r["schedules_due"] for r in results),
            "generated": [g for r in results for g in r["generated"]],
            "skipped": [s for r in results for s in r["skipped"]],
        }
    async with workspace_session(SessionLocal, wid) as session:
        return await generate_due_schedule_invoices(
            session, workspace_id=str(wid), actor_uc_uid=ctx.user_uc_uid,
            as_of=_parse_date(as_of, "as_of"),
            entity_id=str(entity_id) if entity_id else None, limit=limit,
        )


async def list_invoice_schedules(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    active_only: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    """List recurring invoice schedules (created by accepted quotes).

    Args:
        workspace_id: Workspace UUID.
        entity_id: Optional entity filter.
        active_only: Only schedules that still have periods to generate.
        limit: Max rows (default 200).

    Returns: {schedules: [{id, entity_id, quote_ref, ar_customer_id, customer_name, cadence,
    amount_cents, currency, description, periods_total, periods_generated, next_run_at,
    generated_invoice_ids[], active}], total}.

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    wid = ctx.workspace_id
    scope = assert_entity_in_scope(ctx, entity_id)
    async with workspace_session(SessionLocal, wid) as session:
        rows = await _list_schedules(
            session, workspace_id=str(wid),
            entity_id=str(entity_id) if entity_id else None,
            entity_scope=scope, active_only=active_only, limit=limit,
        )
    return {"schedules": rows, "total": len(rows)}


__all__ = ("create_invoices_from_quote", "list_invoice_schedules", "run_invoice_schedules")
