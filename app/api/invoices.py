"""AR / Invoicing HTTP routes (workspace + entity scoped) + invoice PDF.

  GET  /api/workspaces/{wid}/invoices                      — list (filters + aging)
  POST /api/workspaces/{wid}/invoices                      — create (idempotent)
  GET  /api/workspaces/{wid}/invoices/{invoice_id}         — detail
  POST /api/workspaces/{wid}/invoices/{invoice_id}/void    — void (reversing entries)
  POST /api/workspaces/{wid}/invoices/{invoice_id}/payments — record a payment
  GET  /api/workspaces/{wid}/invoices/{invoice_id}/pdf     — server-rendered PDF

These routes delegate ALL ledger work to ``app.ar`` (which posts through the
existing journal primitives / hash chain — never the ledger directly). Auth +
entity scope mirror app/api/journal.py: resolve_workspace + require_role +
assert_entity_in_scope. The cross-app (PO-originated) create path is gated by
``app.ar.entitlement`` using the caller's subscription claim.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.ar import (
    create_invoice_from_draft,
    get_invoice,
    record_payment,
    void_invoice,
)
from app.ar.entitlement import EntitlementError, assert_entitled_for_invoice
from app.ar.invoicing import InvoiceError
from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["invoices"])


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class CustomerBody(BaseModel):
    name: str
    contact_id: str | None = None
    email: str | None = None
    billing_address: str | None = None


class LineItemBody(BaseModel):
    description: str
    quantity: float | str = 1
    unit_price: float | str = 0
    amount: float | str | None = None
    tax_code: str | None = None
    tax_amount: float | str | None = None
    revenue_account_id: UUID | None = None
    source_ref: str | None = None


class CreateInvoiceBody(BaseModel):
    entity_id: UUID
    external_source: str = Field(default="manual", max_length=64)
    external_ref: str | None = Field(default=None, max_length=256)
    customer: CustomerBody
    line_items: list[LineItemBody] = Field(..., min_length=1)
    currency: str = "USD"
    issue_date: date | None = None
    due_date: date | None = None
    net_terms: int | None = None
    discount: float | str | None = None
    notes: str | None = None
    po_number: str | None = Field(default=None, max_length=128)
    auto_send: bool = False


class RecordPaymentBody(BaseModel):
    amount: float | str
    method: str = Field(default="manual", max_length=32)
    received_date: date | None = None
    external_ref: str | None = Field(default=None, max_length=256)
    memo: str | None = None


class VoidInvoiceBody(BaseModel):
    reason: str = Field(..., min_length=1)


def _invoice_error(exc: InvoiceError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _attach_pdf_url(view: dict[str, Any], workspace_id: UUID) -> dict[str, Any]:
    out = dict(view)
    inv_id = out.get("id")
    out.setdefault("hosted_pay_url", None)
    out["pdf_url"] = (
        f"/api/workspaces/{workspace_id}/invoices/{inv_id}/pdf" if inv_id else None
    )
    return out


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/invoices")
async def list_invoices(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: UUID | None = None,
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """List invoices with optional filters + a derived aging bucket per row.

    Filters: entity_id, status, customer_id. Aging is computed off due_date for
    rows that still carry a balance (current | 1-30 | 31-60 | 61-90 | 90+).
    Role floor: viewer.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    where = ["1=1"]
    params: dict[str, Any] = {
        "limit": min(max(int(limit), 1), 500),
        "offset": max(int(offset), 0),
    }
    if entity_id:
        where.append("i.entity_id = :eid")
        params["eid"] = str(entity_id)
    if scope_restriction is not None:
        where.append("i.entity_id = ANY(:entity_scope)")
        params["entity_scope"] = [str(e) for e in scope_restriction]
    if status_filter:
        where.append("i.status = :status")
        params["status"] = status_filter
    if customer_id:
        where.append("i.counterparty_id = :cpid")
        params["cpid"] = str(customer_id)
    where_sql = " AND ".join(where)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT i.id::text, i.entity_id::text,
                           e.display_name AS entity_display_name,
                           i.counterparty_id::text,
                           c.display_name AS customer_name,
                           i.invoice_number, i.status, i.currency,
                           i.issue_date::text, i.due_date::text,
                           i.subtotal, i.tax_total, i.total, i.balance_due,
                           i.po_number, i.external_source, i.external_ref,
                           i.created_at
                    FROM invoice i
                    LEFT JOIN counterparty c ON c.id = i.counterparty_id
                    LEFT JOIN entity e ON e.id = i.entity_id
                    WHERE {where_sql}
                    ORDER BY i.issue_date DESC, i.created_at DESC, i.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM invoice i WHERE {where_sql}"),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

    today = datetime.now(tz=timezone.utc).date()

    def _aging(due: str, status_value: str, balance: Any) -> str:
        if status_value in ("paid", "void") or Decimal(str(balance)) <= 0:
            return "paid"
        try:
            due_d = date.fromisoformat(due)
        except (ValueError, TypeError):
            return "current"
        days = (today - due_d).days
        if days <= 0:
            return "current"
        if days <= 30:
            return "1-30"
        if days <= 60:
            return "31-60"
        if days <= 90:
            return "61-90"
        return "90+"

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    out_rows = []
    for r in rows:
        d = {k: _b(v) for k, v in dict(r).items()}
        d["aging"] = _aging(d["due_date"], d["status"], d["balance_due"])
        out_rows.append(d)

    return {"invoices": out_rows, "total": int(total)}


# ---------------------------------------------------------------------------
# Create (idempotent + entitlement-gated for the cross-app path)
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/invoices", status_code=status.HTTP_201_CREATED)
async def create_invoice(
    workspace_id: UUID,
    body: CreateInvoiceBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Create + post an AR invoice. Idempotent on (external_source, external_ref).

    Standalone invoicing is not gated. The Project-Ops cross-app origin requires
    a subscription with BOTH Project-Ops + Accounting-Ops (token claim).
    Role floor: accountant.
    """
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, body.entity_id)

    # Entitlement gate (no-op for standalone). The user's verified token claims
    # are not on WorkspaceContext; read them from the MCP/HTTP auth contextvar
    # (bind_http_caller sets it for tools routes, but the journal-style routes
    # don't — so we read it defensively and fall back to None, which the gate
    # treats as "no entitlement claim present").
    from app.mcp_auth import current_auth

    auth = current_auth()
    claims = auth.claims if auth is not None else None
    try:
        assert_entitled_for_invoice(
            claims, external_source=body.external_source, workspace_id=workspace_id
        )
    except EntitlementError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    line_items = [li.model_dump(exclude_none=False) for li in body.line_items]
    # Normalize revenue_account_id UUID → str for the service layer.
    for li in line_items:
        if li.get("revenue_account_id") is not None:
            li["revenue_account_id"] = str(li["revenue_account_id"])

    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            view = await create_invoice_from_draft(
                session,
                workspace_id=str(workspace.workspace_id),
                entity_id=str(body.entity_id),
                actor_uc_uid=workspace.user_uc_uid,
                external_source=body.external_source,
                external_ref=body.external_ref,
                customer=body.customer.model_dump(exclude_none=False),
                line_items=line_items,
                currency=body.currency,
                issue_date=body.issue_date,
                due_date=body.due_date,
                net_terms=body.net_terms,
                discount=body.discount if body.discount is not None else 0,
                notes=body.notes,
                po_number=body.po_number,
                auto_send=body.auto_send,
            )
    except InvoiceError as exc:
        raise _invoice_error(exc) from exc

    return _attach_pdf_url(view, workspace_id)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


async def _load_invoice_scoped(
    workspace: WorkspaceContext, invoice_id: UUID
) -> dict[str, Any]:
    """Load an invoice view + enforce entity scope (404 if out-of-scope)."""
    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            view = await get_invoice(
                session,
                workspace_id=str(workspace.workspace_id),
                invoice_id=str(invoice_id),
            )
    except InvoiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    scope = assert_entity_in_scope(workspace, None)
    if scope is not None and UUID(view["entity_id"]) not in scope:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return view


@router.get("/api/workspaces/{workspace_id}/invoices/{invoice_id}")
async def get_invoice_detail(
    workspace_id: UUID,
    invoice_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Fetch one invoice (header + lines + payments). Role floor: viewer."""
    require_role(workspace, "viewer")
    view = await _load_invoice_scoped(workspace, invoice_id)
    return _attach_pdf_url(view, workspace_id)


# ---------------------------------------------------------------------------
# Void
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/invoices/{invoice_id}/void")
async def void_invoice_route(
    workspace_id: UUID,
    invoice_id: UUID,
    body: VoidInvoiceBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Void an invoice via reversing journal entries. Role floor: accountant."""
    require_role(workspace, "accountant")
    # Scope check first.
    await _load_invoice_scoped(workspace, invoice_id)
    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            view = await void_invoice(
                session,
                workspace_id=str(workspace.workspace_id),
                invoice_id=str(invoice_id),
                actor_uc_uid=workspace.user_uc_uid,
                reason=body.reason,
            )
    except InvoiceError as exc:
        raise _invoice_error(exc) from exc
    return _attach_pdf_url(view, workspace_id)


# ---------------------------------------------------------------------------
# Record payment
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/invoices/{invoice_id}/payments")
async def record_payment_route(
    workspace_id: UUID,
    invoice_id: UUID,
    body: RecordPaymentBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Record a payment received against an invoice (DR Cash / CR AR).

    Backs the manual "mark paid" action. Role floor: accountant.
    """
    require_role(workspace, "accountant")
    await _load_invoice_scoped(workspace, invoice_id)
    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            view = await record_payment(
                session,
                workspace_id=str(workspace.workspace_id),
                invoice_id=str(invoice_id),
                actor_uc_uid=workspace.user_uc_uid,
                amount=body.amount,
                method=body.method,
                received_date=body.received_date,
                external_ref=body.external_ref,
                memo=body.memo,
            )
    except InvoiceError as exc:
        raise _invoice_error(exc) from exc
    return _attach_pdf_url(view, workspace_id)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/invoices/{invoice_id}/pdf")
async def get_invoice_pdf(
    workspace_id: UUID,
    invoice_id: UUID,
    disposition: str = Query("inline"),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    """Render the invoice as a clean server-side PDF (reportlab). Role floor: viewer."""
    require_role(workspace, "viewer")
    view = await _load_invoice_scoped(workspace, invoice_id)

    pdf_bytes = _render_invoice_pdf(view)
    cd_mode = "inline" if disposition.lower().strip() == "inline" else "attachment"
    filename = f"{view.get('invoice_number', 'invoice')}.pdf".replace(" ", "")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'{cd_mode}; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "Cache-Control": "no-store",
        },
    )


def _money(value: Any, currency: str = "USD") -> str:
    try:
        n = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return str(value)
    sym = "$" if currency == "USD" else f"{currency} "
    neg = n < 0
    n = abs(n)
    whole = int(n)
    cents = int((n - whole) * 100)
    return f"{'-' if neg else ''}{sym}{whole:,}.{cents:02d}"


def _render_invoice_pdf(view: dict[str, Any]) -> bytes:
    """Render a clean single-page invoice PDF with reportlab (matches the
    cover_sheet.py approach — vector PDF, lazy import, graceful failure)."""
    try:
        from reportlab.lib.colors import Color, HexColor, black
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - dep is in main deps
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="reportlab is required for invoice PDF generation.",
        ) from exc

    LETTER_W, LETTER_H = 612.0, 792.0
    margin_x = 54.0
    cursor_y = LETTER_H - 54.0

    currency = view.get("currency") or "USD"
    status_value = (view.get("status") or "draft").upper()

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))

    # Header band.
    brand = HexColor("#6D28D9")
    c.setFillColor(brand)
    c.rect(0, LETTER_H - 8, LETTER_W, 8, fill=1, stroke=0)

    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 26)
    c.drawString(margin_x, cursor_y, "INVOICE")
    c.setFont("Helvetica", 11)
    c.drawRightString(LETTER_W - margin_x, cursor_y + 4, view.get("invoice_number", ""))
    c.setFont("Helvetica-Oblique", 9)
    status_color = HexColor("#16a34a") if status_value == "PAID" else (
        HexColor("#9ca3af") if status_value == "VOID" else HexColor("#d97706")
    )
    c.setFillColor(status_color)
    c.drawRightString(LETTER_W - margin_x, cursor_y - 10, status_value)
    c.setFillColor(black)
    cursor_y -= 40

    # From (entity) / To (customer).
    entity_name = view.get("entity_display_name") or "—"
    customer_name = view.get("customer_name") or "—"
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin_x, cursor_y, "FROM")
    c.drawString(margin_x + 280, cursor_y, "BILL TO")
    c.setFont("Helvetica", 10)
    c.drawString(margin_x, cursor_y - 15, entity_name[:42])
    c.drawString(margin_x + 280, cursor_y - 15, customer_name[:42])
    cursor_y -= 44

    # Dates.
    c.setFont("Helvetica", 9)
    c.setFillColor(Color(0.35, 0.35, 0.35))
    c.drawString(margin_x, cursor_y, f"Issue date: {view.get('issue_date', '')}")
    c.drawString(margin_x + 200, cursor_y, f"Due date: {view.get('due_date', '')}")
    if view.get("po_number"):
        c.drawString(margin_x + 400, cursor_y, f"PO: {view['po_number']}")
    c.setFillColor(black)
    cursor_y -= 26

    # Line-item table header.
    c.setFillColor(HexColor("#1e293b"))
    c.rect(margin_x, cursor_y - 4, LETTER_W - 2 * margin_x, 20, fill=1, stroke=0)
    c.setFillColor(Color(1, 1, 1))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(margin_x + 6, cursor_y + 2, "Description")
    c.drawRightString(margin_x + 330, cursor_y + 2, "Qty")
    c.drawRightString(margin_x + 410, cursor_y + 2, "Unit")
    c.drawRightString(LETTER_W - margin_x - 6, cursor_y + 2, "Amount")
    c.setFillColor(black)
    cursor_y -= 22

    c.setFont("Helvetica", 9)
    for li in view.get("lines", []):
        if cursor_y < 160:  # simple overflow guard (MVP: one page)
            c.drawString(margin_x + 6, cursor_y, "… (additional lines truncated)")
            cursor_y -= 14
            break
        c.drawString(margin_x + 6, cursor_y, str(li.get("description", ""))[:58])
        c.drawRightString(margin_x + 330, cursor_y, str(li.get("quantity", "")))
        c.drawRightString(margin_x + 410, cursor_y, _money(li.get("unit_price", 0), currency))
        c.drawRightString(LETTER_W - margin_x - 6, cursor_y, _money(li.get("amount", 0), currency))
        cursor_y -= 16

    cursor_y -= 6
    c.setStrokeColor(Color(0.8, 0.8, 0.8))
    c.line(margin_x + 300, cursor_y, LETTER_W - margin_x, cursor_y)
    cursor_y -= 18

    # Totals block (right-aligned).
    def _total_line(label: str, value: str, bold: bool = False) -> None:
        nonlocal cursor_y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 10 if bold else 9)
        c.drawRightString(LETTER_W - margin_x - 90, cursor_y, label)
        c.drawRightString(LETTER_W - margin_x - 6, cursor_y, value)
        cursor_y -= 16

    _total_line("Subtotal", _money(view.get("subtotal", 0), currency))
    if Decimal(str(view.get("discount_total", 0) or 0)) > 0:
        _total_line("Discount", "-" + _money(view.get("discount_total", 0), currency))
    if Decimal(str(view.get("tax_total", 0) or 0)) > 0:
        _total_line("Sales tax", _money(view.get("tax_total", 0), currency))
    _total_line("Total", _money(view.get("total", 0), currency), bold=True)
    _total_line("Balance due", _money(view.get("balance_due", 0), currency), bold=True)

    # Payments (if any).
    payments = view.get("payments", [])
    if payments:
        cursor_y -= 8
        c.setFont("Helvetica-Bold", 9)
        c.drawString(margin_x, cursor_y, "Payments")
        cursor_y -= 14
        c.setFont("Helvetica", 8)
        for p in payments[:8]:
            c.drawString(
                margin_x + 6,
                cursor_y,
                f"{p.get('received_date', '')} · {p.get('method', '')} · "
                f"{_money(p.get('amount', 0), currency)}",
            )
            cursor_y -= 12

    # Notes + footer.
    if view.get("notes"):
        cursor_y -= 8
        c.setFont("Helvetica-Oblique", 8)
        c.setFillColor(Color(0.4, 0.4, 0.4))
        c.drawString(margin_x, cursor_y, f"Notes: {str(view['notes'])[:120]}")
        c.setFillColor(black)

    c.setFont("Helvetica-Oblique", 7)
    c.setFillColor(Color(0.55, 0.55, 0.55))
    c.drawString(
        margin_x,
        40,
        f"Generated by Accounting-Ops · {datetime.now(tz=timezone.utc).date().isoformat()}",
    )
    if status_value == "VOID":
        # Big VOID watermark.
        c.saveState()
        c.setFont("Helvetica-Bold", 90)
        c.setFillColor(Color(0.85, 0.2, 0.2, alpha=0.18))
        c.translate(LETTER_W / 2, LETTER_H / 2)
        c.rotate(30)
        c.drawCentredString(0, 0, "VOID")
        c.restoreState()

    c.save()
    buf.seek(0)
    return buf.read()
