"""MCP tools for AR / Invoicing (native + cross-app-gated).

Tools (all workspace + entity scoped, auth via authorize_workspace + entity
scope via assert_entity_in_scope inside authorize_workspace):

  upsert_ar_customer       (accountant) — find-or-create the AR customer.
  create_invoice_from_draft(accountant) — idempotent on (external_source,
                            external_ref); posts DR AR / CR Revenue (+ tax).
  get_invoice              (viewer)     — status, balance, payments, urls.
  void_invoice             (accountant) — reversing journal entries.
  record_payment           (accountant) — DR Cash / CR AR for outside-AO payments.

Entitlement gate
================
``create_invoice_from_draft`` is the SINGLE create path. Standalone AO invoicing
is NOT gated. The PROJECT-OPS cross-app origin (``external_source='project-ops'``)
IS gated: the caller's subscription must include BOTH Project-Ops + Accounting-Ops,
checked against the shared subscription claim on the caller's token via
``app.ar.entitlement.assert_entitled_for_invoice`` — never a hardcoded allowlist.

Posting integrity
=================
The tools delegate ALL double-entry to ``app.ar.invoicing`` which posts through
the existing journal primitives (the per-entity SHA-256 hash chain). The tools
never write the ledger directly.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from app.ar import (
    create_invoice_from_draft as _create_invoice,
    get_invoice as _get_invoice,
    record_payment as _record_payment,
    upsert_ar_customer as _upsert_customer,
    void_invoice as _void_invoice,
)
# NOTE (Community Edition carve-out): app.ar.entitlement is the enterprise
# cross-app (PO-originated) entitlement gate. It is a no-op for the standalone
# path (external_source is None). The import is deferred into the invoice-create
# path and only invoked when external_source is set, so community boot never
# hard-imports the entitlement module and standalone invoicing is unaffected.
from app.ar.invoicing import InvoiceError
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.workspace_session import workspace_session


def _parse_date(value: str | None, field: str) -> dt.date | None:
    if value is None or value == "":
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD): {value!r}") from exc


# ---------------------------------------------------------------------------
# upsert_ar_customer
# ---------------------------------------------------------------------------


async def upsert_ar_customer(
    workspace_id: UUID,
    entity_id: UUID,
    name: str,
    contact_id: str | None = None,
    email: str | None = None,
    billing_address: str | None = None,
) -> dict[str, Any]:
    """Find-or-create an AR customer (a Counterparty with kind='customer').

    The customer is workspace-shared (Counterparty has no entity_id); entity_id
    is required for authorization scope only. A thin ``contact_id`` reference to
    Contact-Ops is stored in external_refs — Contact-Ops records are NOT copied in.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Billing entity UUID (authorization scope).
        name: Customer display name (required).
        contact_id: Optional Contact-Ops contact id (thin reference, not copied).
        email: Optional billing email (render convenience).
        billing_address: Optional billing address string (render convenience).

    Returns: {"customer_id", "existed_before", "display_name"}.

    Role floor: accountant.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="accountant")
    try:
        async with workspace_session(SessionLocal, workspace_id) as session:
            return await _upsert_customer(
                session,
                workspace_id=str(workspace_id),
                name=name,
                contact_id=contact_id,
                email=email,
                billing_address=billing_address,
            )
    except InvoiceError as exc:
        raise ValueError(str(exc)) from exc


# ---------------------------------------------------------------------------
# create_invoice_from_draft
# ---------------------------------------------------------------------------


async def create_invoice_from_draft(
    workspace_id: UUID,
    entity_id: UUID,
    external_source: str,
    external_ref: str | None,
    customer: dict[str, Any],
    line_items: list[dict[str, Any]],
    currency: str = "USD",
    issue_date: str | None = None,
    due_date: str | None = None,
    net_terms: int | None = None,
    discount: float | str | None = None,
    notes: str | None = None,
    po_number: str | None = None,
    auto_send: bool = False,
) -> dict[str, Any]:
    """Create + post an AR invoice (DR Accounts Receivable / CR Revenue [+ tax]).

    IDEMPOTENT on (external_source, external_ref): a repeat returns the EXISTING
    invoice and posts NO second journal entry. external_ref may be null for a
    purely-manual invoice (no upstream id).

    Entitlement: standalone invoicing is NOT gated. When external_source is the
    Project-Ops cross-app origin ('project-ops'), the caller's subscription must
    include BOTH Project-Ops and Accounting-Ops (checked from the token claim).

    Args:
        workspace_id: Workspace UUID.
        entity_id: Billing entity UUID.
        external_source: Origin label — 'manual' for native, 'project-ops' for the
            gated cross-app path, or another connector label.
        external_ref: Origin's unique id (idempotency key); null for pure-manual.
        customer: {contact_id?, name, email?, billing_address?}. name required.
        line_items: list of {description, quantity, unit_price, amount?, tax_code?,
            tax_amount?, source_ref?, revenue_account_id?}. amount defaults to
            quantity*unit_price; tax_amount defaults to 0 (sales-tax engine is later).
        currency: ISO-4217 (default USD).
        issue_date: ISO date (default today).
        due_date: ISO date. If omitted, derived from net_terms (default Net-30).
        net_terms: Net-N days (used when due_date omitted).
        discount: Optional invoice-level discount applied to the subtotal.
        notes: Optional free-text note on the invoice.
        po_number: Optional buyer PO reference.
        auto_send: When True the invoice starts in 'sent' (else 'draft').

    Returns the full invoice view: {id, invoice_number, status, total,
    balance_due, hosted_pay_url, pdf_url, journal_entry_id, lines[], payments[], ...}.

    Role floor: accountant.
    """
    # Authorize ONCE (this resolves a federation registry workspace_id -> the
    # internal AO workspace id) and use ctx.workspace_id everywhere below, so the
    # RLS session + ledger post bind to the resolved internal workspace, never the
    # raw (possibly uc-registry) arg.
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")
    wid = ctx.workspace_id

    # Entitlement gate for the cross-app (PO-originated) path. Standalone is a
    # no-op inside assert_entitled_for_invoice. EntitlementError subclasses
    # PermissionError so the MCP channel returns a clean denial.
    # Enterprise-only cross-app entitlement gate (no-op for standalone). Skipped
    # + never imported in Community Edition, which has no cross-app subscription.
    from app.config import get_settings as _get_settings

    if not _get_settings().is_community:
        from app.ar.entitlement import assert_entitled_for_invoice

        auth = current_auth()
        claims = auth.claims if auth is not None else None
        assert_entitled_for_invoice(
            claims, external_source=external_source, workspace_id=wid
        )

    try:
        async with workspace_session(SessionLocal, wid) as session:
            view = await _create_invoice(
                session,
                workspace_id=str(wid),
                entity_id=str(entity_id),
                actor_uc_uid=ctx.user_uc_uid,
                external_source=external_source,
                external_ref=external_ref,
                customer=customer,
                line_items=line_items,
                currency=currency,
                issue_date=_parse_date(issue_date, "issue_date"),
                due_date=_parse_date(due_date, "due_date"),
                net_terms=net_terms,
                discount=discount if discount is not None else 0,
                notes=notes,
                po_number=po_number,
                auto_send=auto_send,
            )
    except InvoiceError as exc:
        raise ValueError(str(exc)) from exc

    return _shape_invoice_result(view, wid)


# ---------------------------------------------------------------------------
# get_invoice
# ---------------------------------------------------------------------------


async def get_invoice(
    workspace_id: UUID,
    invoice_id: UUID | None = None,
    external_source: str | None = None,
    external_ref: str | None = None,
) -> dict[str, Any]:
    """Fetch an invoice by id OR by (external_source, external_ref).

    Args:
        workspace_id: Workspace UUID.
        invoice_id: Invoice UUID (preferred), OR
        external_source + external_ref: the idempotency key to look it up by.

    Returns: {status, balance_due, payments[], hosted_pay_url, pdf_url, ...}.

    Role floor: viewer.
    """
    ctx = await authorize_workspace(workspace_id, None, min_role="viewer")
    wid = ctx.workspace_id  # resolved (federation registry id -> internal AO id)
    try:
        async with workspace_session(SessionLocal, wid) as session:
            view = await _get_invoice(
                session,
                workspace_id=str(wid),
                invoice_id=str(invoice_id) if invoice_id else None,
                external_source=external_source,
                external_ref=external_ref,
            )
    except InvoiceError as exc:
        raise LookupError(str(exc)) from exc

    # Entity-scope enforcement: a scoped member can't read an out-of-scope
    # entity's invoice (report as not found to avoid leaking existence).
    from app.authz import assert_entity_in_scope

    scope = assert_entity_in_scope(ctx, None)
    if scope is not None and UUID(view["entity_id"]) not in scope:
        raise LookupError("invoice not found")
    return _shape_invoice_result(view, wid)


# ---------------------------------------------------------------------------
# void_invoice
# ---------------------------------------------------------------------------


async def void_invoice(
    workspace_id: UUID,
    invoice_id: UUID,
    reason: str,
) -> dict[str, Any]:
    """Void an invoice by appending REVERSING journal entries (never overwrite).

    A NEW entry per original issue row is appended with debit/credit flipped and
    reverses_entry_id pointing at the original. The invoice flips to status='void'
    and balance_due=0. Refuses if the invoice has any payment recorded (refund
    flow is a later phase). Idempotent: re-voiding returns the existing view.

    Args:
        workspace_id: Workspace UUID.
        invoice_id: Invoice UUID.
        reason: Required human-readable void reason (recorded on the invoice).

    Returns the updated invoice view.

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, None, min_role="accountant")
    wid = ctx.workspace_id  # resolved (federation registry id -> internal AO id)
    try:
        async with workspace_session(SessionLocal, wid) as session:
            # Scope check: resolve the invoice's entity then assert it's in scope.
            view = await _get_invoice(
                session, workspace_id=str(wid), invoice_id=str(invoice_id)
            )
            from app.authz import assert_entity_in_scope

            scope = assert_entity_in_scope(ctx, None)
            if scope is not None and UUID(view["entity_id"]) not in scope:
                raise LookupError("invoice not found")
            updated = await _void_invoice(
                session,
                workspace_id=str(wid),
                invoice_id=str(invoice_id),
                actor_uc_uid=ctx.user_uc_uid,
                reason=reason,
            )
    except InvoiceError as exc:
        raise ValueError(str(exc)) from exc
    return _shape_invoice_result(updated, wid)


# ---------------------------------------------------------------------------
# record_payment
# ---------------------------------------------------------------------------


async def record_payment(
    workspace_id: UUID,
    invoice_id: UUID,
    amount: float | str,
    method: str = "manual",
    received_date: str | None = None,
    external_ref: str | None = None,
    memo: str | None = None,
) -> dict[str, Any]:
    """Record a payment received against an invoice → DR Cash / CR AR.

    For payments captured OUTSIDE AO (manual "mark paid" / a deposit not yet
    auto-reconciled). Idempotent on (invoice_id, external_ref) when external_ref
    is supplied. Updates the invoice balance_due + status (partial/paid). Caps at
    the remaining balance; rejects zero/negative.

    Args:
        workspace_id: Workspace UUID.
        invoice_id: Invoice UUID.
        amount: Payment amount (positive). String or number; parsed exactly.
        method: Payment method label (manual|ach|card|check|wire|cash|stripe|other).
        received_date: ISO date (default today).
        external_ref: Optional idempotency key (e.g. processor charge id).
        memo: Optional memo recorded on the payment.

    Returns the updated invoice view.

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, None, min_role="accountant")
    try:
        async with workspace_session(SessionLocal, workspace_id) as session:
            view = await _get_invoice(
                session, workspace_id=str(workspace_id), invoice_id=str(invoice_id)
            )
            from app.authz import assert_entity_in_scope

            scope = assert_entity_in_scope(ctx, None)
            if scope is not None and UUID(view["entity_id"]) not in scope:
                raise LookupError("invoice not found")
            updated = await _record_payment(
                session,
                workspace_id=str(workspace_id),
                invoice_id=str(invoice_id),
                actor_uc_uid=ctx.user_uc_uid,
                amount=amount,
                method=method,
                received_date=_parse_date(received_date, "received_date"),
                external_ref=external_ref,
                memo=memo,
            )
    except InvoiceError as exc:
        raise ValueError(str(exc)) from exc
    return _shape_invoice_result(updated, workspace_id)


# ---------------------------------------------------------------------------
# Result shaping (adds the convenience urls the brief's return shapes promise)
# ---------------------------------------------------------------------------


def _shape_invoice_result(view: dict[str, Any], workspace_id: UUID) -> dict[str, Any]:
    """Augment the raw invoice view with pdf_url + ensure the brief's keys exist.

    pdf_url points at the HTTP route that renders the invoice PDF on the fly.
    hosted_pay_url is the (Phase-2) Stripe pay link — carried through from the
    row (NULL in MVP).
    """
    out = dict(view)
    inv_id = out.get("id")
    out.setdefault("hosted_pay_url", None)
    out["pdf_url"] = (
        f"/api/workspaces/{workspace_id}/invoices/{inv_id}/pdf" if inv_id else None
    )
    return out


__all__ = (
    "create_invoice_from_draft",
    "get_invoice",
    "record_payment",
    "upsert_ar_customer",
    "void_invoice",
)
