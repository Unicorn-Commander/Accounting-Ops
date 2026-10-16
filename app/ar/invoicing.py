"""Native AR / Invoicing service layer.

All double-entry is posted through the EXISTING journal primitives
(``app.journal.posting._insert_journal_entry`` + ``compute_hash`` +
``get_last_chain_hash`` + ``_log_post_attempt``) inside the SAME workspace_session
the caller opened — exactly the pattern the Stripe/eBay connectors use for
explicit-account postings. We do NOT modify ``posting.py``, the hash chain, or
the closed-period guard, and we NEVER write the ledger directly.

Posting model (each journal_entry row balances DR == CR by construction)
========================================================================
An invoice's gross can hit several accounts (revenue per line + sales-tax
liability). Because ``journal_entry`` stores ONE debit_account + ONE credit_account
per row, we decompose the issue into a set of balanced 2-account rows that, summed,
realize the full multi-leg entry — the same way the Stripe mapper posts a separate
balanced "fee" sidecar row:

  Issue an invoice with subtotal S (split across revenue accounts r1..rk) + tax T:
    row per revenue account ri:   DR Accounts Receivable (Si)  / CR Revenue ri (Si)
    one tax row (if T > 0):       DR Accounts Receivable (T)   / CR Sales Tax Payable (T)
  => net DR AR = S + T ; net CR = sum(revenue) + tax. Balanced.

  Payment of amount P:            DR Cash (P)                  / CR Accounts Receivable (P)

  Void / credit-note: a NEW reversing entry per issue row (debit/credit flipped,
  reverses_entry_id -> the original issue row). Never an UPDATE of a posted row.

Idempotency
===========
``create_invoice_from_draft`` is idempotent on (workspace_id, external_source,
external_ref): a repeat returns the EXISTING invoice and posts NO second journal
entry. Enforced by a pre-check + the partial unique index from migration 0019.
``record_payment`` is idempotent on (invoice_id, external_ref) when external_ref
is supplied.

Materiality note
================
The materiality/approval gate (app.journal.materiality) governs auto-categorized
bank/connector posts; an AR invoice is an explicit, human/agent-authored document
whose accounts are fully determined, so we post it directly (like the connectors'
explicit-account path) rather than routing it through the auto-post gate. A
materiality-style hold on very large invoices is a clean follow-up (TODO below) —
the hook is the ``materiality_tier`` we already stamp on each row.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.journal.accounts import canonical_form
from app.journal.hash_chain import compute_hash, get_last_chain_hash
from app.journal.materiality import materiality_tier
from app.journal.posting import _insert_journal_entry, _log_post_attempt
from app.core.business_time import business_today

# journal_entry.source label for every AR posting (issue + payment + void).
INVOICE_SOURCE = "ar_invoice"

# Default COA account NUMBERS the AR posting resolves against. Present in every
# COA template (c_corp/llc/s_corp/sole_prop/personal_1040/nonprofit/partnership)
# — see agents/_shared/coa_templates/*. We resolve by NUMBER (robust across
# templates whose account NAMES differ).
ACCT_AR = "1100"            # Accounts Receivable (asset)
ACCT_SALES_TAX = "2300"     # Sales Tax Payable (liability)
ACCT_REVENUE_DEFAULT = "4030"  # Revenue · Services (fallback line revenue acct)
ACCT_REVENUE_FALLBACKS = ("4030", "4010", "4090")  # try in order if 4030 missing
ACCT_CASH_DEFAULT = "1010"  # Cash · Primary Checking (payment debit side)

# Seed metadata for accounts we ensure exist (older entities seeded before these
# accounts were in the template). type + normal_balance match the templates.
_ENSURE_ACCOUNTS: dict[str, tuple[str, str, str]] = {
    # number: (name, account_type, normal_balance)
    ACCT_AR: ("Accounts Receivable", "asset", "debit"),
    ACCT_SALES_TAX: ("Sales Tax Payable", "liability", "credit"),
    ACCT_REVENUE_DEFAULT: ("Revenue · Services", "revenue", "credit"),
    ACCT_CASH_DEFAULT: ("Cash · Primary Checking", "asset", "debit"),
}

_CENTS = Decimal("0.01")
_PUBLIC_TOKEN_BYTES = 32
_PUBLIC_TOKEN_ATTEMPTS = 8


class InvoiceError(Exception):
    """A clean, caller-surfaceable AR error (bad input, missing entity, etc.)."""


def _q(amount: Decimal) -> Decimal:
    """Quantize to cents, half-up — money never carries sub-cent dust."""
    return amount.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _to_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise InvoiceError(f"{field_name} must be a number; got {value!r}") from exc


def _cents(amount: Decimal) -> int:
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, bool, str, dict, list)):
        return v
    return str(v)


# ---------------------------------------------------------------------------
# COA resolution
# ---------------------------------------------------------------------------


async def _resolve_account_canonical(
    session: AsyncSession, *, entity_id: str, account_number: str
) -> str | None:
    row = (
        await session.execute(
            text(
                """
                SELECT canonical_form FROM chart_of_account
                WHERE entity_id = :eid AND account_number = :num AND is_active = true
                LIMIT 1
                """
            ),
            {"eid": entity_id, "num": account_number},
        )
    ).mappings().first()
    return row["canonical_form"] if row else None


async def _ensure_account(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    account_number: str,
) -> str:
    """Return the canonical_form for an account number, seeding it if missing.

    Idempotent on (entity_id, account_number). Used so an entity seeded before
    1100/2300/4030/1010 existed in the template still gets a usable AR posting.
    """
    existing = await _resolve_account_canonical(
        session, entity_id=entity_id, account_number=account_number
    )
    if existing:
        return existing
    name, atype, nb = _ENSURE_ACCOUNTS[account_number]
    canonical = canonical_form(account_number, name)
    await session.execute(
        text(
            """
            INSERT INTO chart_of_account (
                workspace_id, entity_id, account_number, account_name,
                canonical_form, account_type, normal_balance, is_system, is_active, notes
            ) VALUES (
                :wid, :eid, :num, :name, :canonical, :atype, :nb, true, true,
                'Auto-seeded by AR/Invoicing (account required for invoice postings).'
            )
            ON CONFLICT (entity_id, account_number) DO NOTHING
            """
        ),
        {
            "wid": workspace_id,
            "eid": entity_id,
            "num": account_number,
            "name": name,
            "canonical": canonical,
            "atype": atype,
            "nb": nb,
        },
    )
    # Re-read in case a concurrent writer inserted a row with a different name.
    resolved = await _resolve_account_canonical(
        session, entity_id=entity_id, account_number=account_number
    )
    return resolved or canonical


async def _resolve_revenue_account(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    revenue_account_id: str | None,
) -> tuple[str | None, str]:
    """Resolve a line's revenue account → (revenue_account_id, canonical_form).

    If ``revenue_account_id`` is given and active, use it. Otherwise fall back to
    the first available default revenue account (4030 → 4010 → 4090), seeding
    4030 if none exist. Returns the (id, canonical) so the line can snapshot both.
    """
    if revenue_account_id:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, canonical_form, account_type
                    FROM chart_of_account
                    WHERE id = :id AND entity_id = :eid AND is_active = true
                    LIMIT 1
                    """
                ),
                {"id": revenue_account_id, "eid": entity_id},
            )
        ).mappings().first()
        if row is None:
            raise InvoiceError(
                f"revenue_account_id {revenue_account_id} not found / not active "
                "for this entity"
            )
        return row["id"], row["canonical_form"]

    # Fallback: first existing default revenue account.
    for num in ACCT_REVENUE_FALLBACKS:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, canonical_form FROM chart_of_account
                    WHERE entity_id = :eid AND account_number = :num AND is_active = true
                    LIMIT 1
                    """
                ),
                {"eid": entity_id, "num": num},
            )
        ).mappings().first()
        if row:
            return row["id"], row["canonical_form"]

    # None exist — seed the default and resolve its id.
    canonical = await _ensure_account(
        session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        account_number=ACCT_REVENUE_DEFAULT,
    )
    row = (
        await session.execute(
            text(
                "SELECT id::text AS id FROM chart_of_account "
                "WHERE entity_id = :eid AND account_number = :num LIMIT 1"
            ),
            {"eid": entity_id, "num": ACCT_REVENUE_DEFAULT},
        )
    ).mappings().first()
    return (row["id"] if row else None), canonical


# ---------------------------------------------------------------------------
# Invoice numbering
# ---------------------------------------------------------------------------


async def _next_invoice_number(
    session: AsyncSession, *, entity_id: str, issue_date: date
) -> str:
    """Generate the next per-entity invoice number: ``INV-{YYYY}-{NNNN}``.

    Counts existing invoices for the entity in the issue year and increments.
    The (entity_id, invoice_number) UNIQUE constraint is the final arbiter: on a
    rare collision the caller retries via the IntegrityError path.
    """
    prefix = f"INV-{issue_date.year}-"
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS n FROM invoice
                WHERE entity_id = :eid AND invoice_number LIKE :pfx
                """
            ),
            {"eid": entity_id, "pfx": f"{prefix}%"},
        )
    ).mappings().first()
    seq = int(row["n"]) + 1 if row else 1
    return f"{prefix}{seq:04d}"


# ---------------------------------------------------------------------------
# Customer (Counterparty) upsert
# ---------------------------------------------------------------------------


async def upsert_ar_customer(
    session: AsyncSession,
    *,
    workspace_id: str,
    name: str,
    contact_id: str | None = None,
    email: str | None = None,
    billing_address: str | dict | None = None,
) -> dict[str, Any]:
    """Find-or-create the AR customer as a Counterparty (``kind='customer'``).

    Counterparty is the workspace-shared AR-customer record. We DO NOT copy
    Contact-Ops records in: a thin ``contact_id`` reference is stored in
    ``external_refs.contact_id`` so the canonical contact stays in Contact-Ops.
    email + billing_address are convenience snapshots for rendering invoices.

    Match order (idempotent):
      1. existing customer with external_refs->>'contact_id' == contact_id
      2. existing customer with matching (case-insensitive) display_name
      3. else INSERT a new customer

    Returns {"customer_id", "existed_before", "display_name"}.
    """
    name = (name or "").strip()
    if not name:
        raise InvoiceError("customer name is required")

    # 1. Match on contact_id reference.
    existing = None
    if contact_id:
        existing = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, display_name FROM counterparty
                    WHERE kind = 'customer'
                      AND external_refs->>'contact_id' = :cid
                    LIMIT 1
                    """
                ),
                {"cid": str(contact_id)},
            )
        ).mappings().first()

    # 2. Match on name.
    if existing is None:
        existing = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, display_name FROM counterparty
                    WHERE kind = 'customer' AND lower(display_name) = lower(:name)
                    LIMIT 1
                    """
                ),
                {"name": name},
            )
        ).mappings().first()

    # Build the external_refs payload (thin references + render snapshots).
    import json as _json

    refs: dict[str, Any] = {"is_customer": True}
    if contact_id:
        refs["contact_id"] = str(contact_id)
    if email:
        refs["email"] = email
    if billing_address is not None:
        refs["billing_address"] = billing_address

    if existing is not None:
        cid = existing["id"]
        # Merge new refs onto the existing row (don't clobber other keys).
        await session.execute(
            text(
                """
                UPDATE counterparty
                SET external_refs = external_refs || CAST(:refs AS jsonb)
                WHERE id = :id
                """
            ),
            {"refs": _json.dumps(refs), "id": cid},
        )
        return {
            "customer_id": cid,
            "existed_before": True,
            "display_name": existing["display_name"],
        }

    new_id = uuid4()
    await session.execute(
        text(
            """
            INSERT INTO counterparty (id, workspace_id, display_name, kind, external_refs)
            VALUES (:id, :wid, :name, 'customer', CAST(:refs AS jsonb))
            """
        ),
        {
            "id": str(new_id),
            "wid": workspace_id,
            "name": name,
            "refs": _json.dumps(refs),
        },
    )
    return {"customer_id": str(new_id), "existed_before": False, "display_name": name}


# ---------------------------------------------------------------------------
# Line normalization + totals
# ---------------------------------------------------------------------------


@dataclass
class _NormLine:
    line_number: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal          # pre-tax extended
    tax_code: str | None
    tax_amount: Decimal
    revenue_account_id: str | None
    revenue_account_canonical: str
    source_ref: str | None


@dataclass
class _Totals:
    subtotal: Decimal
    discount_total: Decimal
    tax_total: Decimal
    total: Decimal


async def _normalize_lines(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    line_items: list[dict[str, Any]],
) -> list[_NormLine]:
    if not line_items:
        raise InvoiceError("an invoice needs at least one line item")
    out: list[_NormLine] = []
    for i, li in enumerate(line_items, start=1):
        description = str(li.get("description") or "").strip()
        if not description:
            raise InvoiceError(f"line {i}: description is required")
        qty = _to_decimal(li.get("quantity", 1), f"line {i} quantity")
        unit_price = _to_decimal(li.get("unit_price", 0), f"line {i} unit_price")
        # amount: caller may supply it; otherwise qty*unit_price. We always
        # quantize to cents.
        if li.get("amount") is not None:
            amount = _q(_to_decimal(li["amount"], f"line {i} amount"))
        else:
            amount = _q(qty * unit_price)
        tax_amount = _q(_to_decimal(li.get("tax_amount", 0), f"line {i} tax_amount"))
        if amount < 0:
            raise InvoiceError(f"line {i}: amount cannot be negative")
        if tax_amount < 0:
            raise InvoiceError(f"line {i}: tax_amount cannot be negative")
        rev_id, rev_canonical = await _resolve_revenue_account(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            revenue_account_id=(
                str(li["revenue_account_id"]) if li.get("revenue_account_id") else None
            ),
        )
        out.append(
            _NormLine(
                line_number=i,
                description=description[:512],
                quantity=qty,
                unit_price=unit_price,
                amount=amount,
                tax_code=(str(li["tax_code"]) if li.get("tax_code") else None),
                tax_amount=tax_amount,
                revenue_account_id=rev_id,
                revenue_account_canonical=rev_canonical,
                source_ref=(str(li["source_ref"]) if li.get("source_ref") else None),
            )
        )
    return out


def _compute_totals(lines: list[_NormLine], discount: Decimal) -> _Totals:
    subtotal = _q(sum((li.amount for li in lines), Decimal("0")))
    tax_total = _q(sum((li.tax_amount for li in lines), Decimal("0")))
    discount_total = _q(discount)
    if discount_total < 0:
        raise InvoiceError("discount cannot be negative")
    if discount_total > subtotal:
        raise InvoiceError("discount cannot exceed the subtotal")
    total = _q(subtotal - discount_total + tax_total)
    if total <= 0:
        raise InvoiceError("invoice total must be positive")
    return _Totals(
        subtotal=subtotal,
        discount_total=discount_total,
        tax_total=tax_total,
        total=total,
    )


# ---------------------------------------------------------------------------
# Journal posting (via the existing primitives — never the ledger directly)
# ---------------------------------------------------------------------------


async def _post_balanced_row(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    debit_canonical: str,
    credit_canonical: str,
    amount: Decimal,
    effective_date: date,
    source_external_id: str,
    description: str,
    payee: str | None,
    memo: str | None,
    reasoning: str,
    actor_uc_uid: str,
    reverses_entry_id: UUID | None = None,
    category: str = "AR · Invoice",
) -> UUID:
    """Append ONE balanced (DR==CR) journal_entry row via _insert_journal_entry.

    Mirrors the Stripe/eBay connector posting exactly: compute the chain hash on
    the latest prev_hash, insert inside a savepoint, log the attempt. Raises
    IntegrityError on a true duplicate / chain race so the caller can decide
    (issue treats it as idempotent). ``amount`` is a positive Decimal (the row's
    DR == CR magnitude).
    """
    amount_cents = _cents(amount)
    tier = materiality_tier(amount_cents)
    posted_at = datetime.now(tz=timezone.utc)
    prev_hash = await get_last_chain_hash(session, entity_id)
    this_hash = compute_hash(
        prev_hash,
        effective_date=effective_date,
        amount_cents=amount_cents,
        debit_account=debit_canonical,
        credit_account=credit_canonical,
        source=INVOICE_SOURCE,
        source_external_id=source_external_id,
        payee=payee,
        description=description,
        posted_at=posted_at,
    )
    je_id = uuid4()
    async with session.begin_nested():
        await _insert_journal_entry(
            session,
            je_id=je_id,
            workspace_id=workspace_id,
            entity_id=entity_id,
            amount_cents=amount_cents,
            effective_date=effective_date,
            posted_at=posted_at,
            debit_account=debit_canonical,
            credit_account=credit_canonical,
            category=category,
            tier=tier,
            source=INVOICE_SOURCE,
            source_external_id=source_external_id,
            description=description,
            payee=payee,
            memo=memo,
            prev_hash=prev_hash,
            this_hash=this_hash,
            reasoning_trace=reasoning,
            model_version="ar_invoicing:v1",
            identity=actor_uc_uid,
            approving_uid=actor_uc_uid,
            receipt_link_id=None,
            reverses_entry_id=reverses_entry_id,
        )
    await _log_post_attempt(
        session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        source=INVOICE_SOURCE,
        source_external_id=source_external_id,
        outcome="posted" if reverses_entry_id is None else "reversed",
        journal_entry_id=je_id,
        reason=reasoning,
        actor_uc_uid=actor_uc_uid,
        actor_agent_id=None,
        amount_cents=amount_cents,
        category=category,
    )
    return je_id


# ---------------------------------------------------------------------------
# create_invoice_from_draft (idempotent)
# ---------------------------------------------------------------------------


async def _fetch_invoice_row(
    session: AsyncSession, *, invoice_id: str | None = None,
    workspace_id: str | None = None, external_source: str | None = None,
    external_ref: str | None = None,
) -> dict[str, Any] | None:
    if invoice_id:
        row = (
            await session.execute(
                text("SELECT * FROM invoice WHERE id = :id LIMIT 1"),
                {"id": invoice_id},
            )
        ).mappings().first()
    elif external_ref and external_source and workspace_id:
        row = (
            await session.execute(
                text(
                    """
                    SELECT * FROM invoice
                    WHERE workspace_id = :wid AND external_source = :src
                      AND external_ref = :ref
                    LIMIT 1
                    """
                ),
                {"wid": workspace_id, "src": external_source, "ref": external_ref},
            )
        ).mappings().first()
    else:
        return None
    return dict(row) if row else None


async def _invoice_view(
    session: AsyncSession, *, invoice_id: str
) -> dict[str, Any]:
    """Assemble the full invoice projection (header + lines + payments)."""
    inv = (
        await session.execute(
            text(
                """
                SELECT i.*, c.display_name AS customer_name,
                       c.external_refs->>'email' AS customer_email,
                       c.external_refs->>'billing_address' AS customer_billing_address,
                       e.display_name AS entity_display_name, e.slug AS entity_slug,
                       e.legal_name AS legal_name,
                       e.brand_slug, e.logo_url, e.color_primary, e.color_accent,
                       e.color_background, e.footer_line, e.from_address, e.reply_to,
                       e.invoice_address
                FROM invoice i
                LEFT JOIN counterparty c ON c.id = i.counterparty_id
                LEFT JOIN entity e ON e.id = i.entity_id
                WHERE i.id = :id
                """
            ),
            {"id": invoice_id},
        )
    ).mappings().first()
    if inv is None:
        raise InvoiceError(f"invoice {invoice_id} not found")
    lines = (
        await session.execute(
            text(
                """
                SELECT id::text, line_number, description, quantity, unit_price,
                       amount, tax_code, tax_amount,
                       revenue_account_id::text AS revenue_account_id,
                       revenue_account_canonical, source_ref
                FROM invoice_line WHERE invoice_id = :id
                ORDER BY line_number ASC
                """
            ),
            {"id": invoice_id},
        )
    ).mappings().all()
    payments = (
        await session.execute(
            text(
                """
                SELECT id::text, amount, method, received_date::text AS received_date,
                       recon_link_id::text AS recon_link_id,
                       journal_entry_id::text AS journal_entry_id,
                       external_ref, memo, created_at
                FROM invoice_payment WHERE invoice_id = :id
                ORDER BY received_date ASC, created_at ASC
                """
            ),
            {"id": invoice_id},
        )
    ).mappings().all()
    out = {k: _jsonable(v) for k, v in dict(inv).items()}
    out["lines"] = [{k: _jsonable(v) for k, v in dict(r).items()} for r in lines]
    out["payments"] = [{k: _jsonable(v) for k, v in dict(r).items()} for r in payments]
    return out


async def create_invoice_from_draft(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    actor_uc_uid: str,
    external_source: str,
    external_ref: str | None,
    customer: dict[str, Any],
    line_items: list[dict[str, Any]],
    currency: str = "USD",
    issue_date: date | None = None,
    due_date: date | None = None,
    net_terms: int | None = None,
    discount: Any = 0,
    notes: str | None = None,
    po_number: str | None = None,
    auto_send: bool = False,
    post_journal: bool = True,
    quote_ref: str | None = None,
    customer_ops_customer_id: str | None = None,
    presentation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create + post an AR invoice. Idempotent on (external_source, external_ref).

    Posts (via the existing journal primitives) one balanced DR-AR/CR-Revenue row
    per distinct revenue account, plus one DR-AR/CR-Sales-Tax row if tax > 0.
    Returns the full invoice view including journal_entry_id.

    ``post_journal=False`` (quotes seam, 2026-09-12): create the invoice + lines
    as a pure DRAFT document and post NOTHING — ``journal_entry_id`` stays NULL
    and the hash chain is untouched. Only valid with ``auto_send=False`` (a
    deferred-posting invoice must be a draft). The default (``True``) keeps
    every existing caller byte-for-byte unchanged. ``quote_ref`` (nullable,
    "number + version") is stamped on the row for the quote → invoice link;
    it is written by a separate UPDATE so the INSERT the existing callers
    exercise is unchanged.

    NOTE: this is the NATIVE primitive. The entitlement gate for the cross-app
    (PO-originated) path is enforced by the CALLER (the MCP tool / HTTP route)
    via ``app.ar.entitlement.assert_entitled_for_invoice`` — standalone callers
    do not gate.
    """
    external_source = str(external_source or "").strip()
    if not external_source:
        raise InvoiceError("external_source is required")
    if len(external_source) > 64:
        raise InvoiceError("external_source cannot exceed 64 characters")
    if external_ref is not None:
        external_ref = str(external_ref).strip()
        if not external_ref:
            raise InvoiceError("external_ref cannot be blank when supplied")
        if len(external_ref) > 256:
            raise InvoiceError("external_ref cannot exceed 256 characters")
    currency = (currency or "USD").upper()[:3]
    issue_date = issue_date or business_today()
    if due_date is None:
        terms = int(net_terms) if net_terms is not None else 30
        if terms < 0:
            raise InvoiceError("net_terms cannot be negative")
        due_date = issue_date + timedelta(days=terms)
    if due_date < issue_date:
        raise InvoiceError("due_date cannot precede issue_date")
    if not post_journal and auto_send:
        raise InvoiceError("post_journal=False requires a draft (auto_send must be False)")

    # ---- Idempotency pre-check (external_ref present) ----
    if external_ref:
        existing = await _fetch_invoice_row(
            session,
            workspace_id=workspace_id,
            external_source=external_source,
            external_ref=external_ref,
        )
        if existing is not None:
            return await _invoice_view(session, invoice_id=str(existing["id"]))

    # ---- Customer (Counterparty) ----
    cust = await upsert_ar_customer(
        session,
        workspace_id=workspace_id,
        name=str(customer.get("name") or "").strip(),
        contact_id=customer.get("contact_id"),
        email=customer.get("email"),
        billing_address=customer.get("billing_address"),
    )
    counterparty_id = cust["customer_id"]

    # ---- Lines + totals ----
    norm_lines = await _normalize_lines(
        session, workspace_id=workspace_id, entity_id=entity_id, line_items=line_items
    )
    totals = _compute_totals(norm_lines, _to_decimal(discount or 0, "discount"))

    # ---- Resolve control accounts ----
    ar_canonical = await _ensure_account(
        session, workspace_id=workspace_id, entity_id=entity_id, account_number=ACCT_AR
    )
    tax_canonical = None
    if totals.tax_total > 0:
        tax_canonical = await _ensure_account(
            session, workspace_id=workspace_id, entity_id=entity_id,
            account_number=ACCT_SALES_TAX,
        )

    # ---- Insert invoice header (status begins draft; flips to sent if auto_send) ----
    initial_status = "sent" if auto_send else "draft"
    invoice_id = uuid4()
    invoice_number = await _next_invoice_number(
        session, entity_id=entity_id, issue_date=issue_date
    )
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    INSERT INTO invoice (
                        id, workspace_id, entity_id, counterparty_id, invoice_number,
                        status, currency, issue_date, due_date, net_terms,
                        subtotal, discount_total, tax_total, total, balance_due,
                        notes, po_number, external_source, external_ref,
                        created_by_uc_uid
                    ) VALUES (
                        :id, :wid, :eid, :cpid, :num,
                        :status, :cur, :issue, :due, :terms,
                        :subtotal, :discount, :tax, :total, :balance,
                        :notes, :po, :src, :ref,
                        :uid
                    )
                    """
                ),
                {
                    "id": str(invoice_id),
                    "wid": workspace_id,
                    "eid": entity_id,
                    "cpid": counterparty_id,
                    "num": invoice_number,
                    "status": initial_status,
                    "cur": currency,
                    "issue": issue_date,
                    "due": due_date,
                    "terms": net_terms,
                    "subtotal": totals.subtotal,
                    "discount": totals.discount_total,
                    "tax": totals.tax_total,
                    "total": totals.total,
                    "balance": totals.total,
                    "notes": notes,
                    "po": po_number,
                    "src": external_source,
                    "ref": external_ref,
                    "uid": actor_uc_uid,
                },
            )
    except IntegrityError:
        # A concurrent writer won the (workspace, source, external_ref) race, OR
        # an invoice_number collision. If it's the idempotency key, return the
        # existing invoice (no second journal entry). Otherwise re-raise.
        if external_ref:
            existing = await _fetch_invoice_row(
                session,
                workspace_id=workspace_id,
                external_source=external_source,
                external_ref=external_ref,
            )
            if existing is not None:
                return await _invoice_view(session, invoice_id=str(existing["id"]))
        raise

    # ---- Insert lines ----
    for li in norm_lines:
        await session.execute(
            text(
                """
                INSERT INTO invoice_line (
                    workspace_id, entity_id, invoice_id, line_number, description,
                    quantity, unit_price, amount, tax_code, tax_amount,
                    revenue_account_id, revenue_account_canonical, source_ref
                ) VALUES (
                    :wid, :eid, :iid, :ln, :desc,
                    :qty, :price, :amt, :tcode, :tamt,
                    :rev_id, :rev_canonical, :sref
                )
                """
            ),
            {
                "wid": workspace_id,
                "eid": entity_id,
                "iid": str(invoice_id),
                "ln": li.line_number,
                "desc": li.description,
                "qty": li.quantity,
                "price": li.unit_price,
                "amt": li.amount,
                "tcode": li.tax_code,
                "tamt": li.tax_amount,
                "rev_id": li.revenue_account_id,
                "rev_canonical": li.revenue_account_canonical,
                "sref": li.source_ref,
            },
        )

    # ---- Quote link (quotes seam) — separate UPDATE keeps the INSERT stable ----
    if quote_ref:
        await session.execute(
            text("UPDATE invoice SET quote_ref = :qr WHERE id = :id"),
            {"qr": str(quote_ref)[:160], "id": str(invoice_id)},
        )

    presentation_json = None
    if presentation:
        encoded = json.dumps(presentation)
        if len(encoded) <= 8000:
            presentation_json = encoded
    public_token = None
    for _ in range(_PUBLIC_TOKEN_ATTEMPTS):
        candidate = secrets.token_urlsafe(_PUBLIC_TOKEN_BYTES)
        inserted = (
            await session.execute(
                text(
                    """
                    INSERT INTO public.invoice_pay_token (token, workspace_id, invoice_id)
                    VALUES (:token, :wid, :iid)
                    ON CONFLICT (token) DO NOTHING
                    RETURNING token
                    """
                ),
                {"token": candidate, "wid": workspace_id, "iid": str(invoice_id)},
            )
        ).first()
        if inserted is None:
            continue
        public_token = candidate
        break
    if public_token is None:
        raise InvoiceError("could not allocate a unique public invoice token")
    await session.execute(
        text(
            """
            UPDATE invoice SET public_token = :token,
                               customer_ops_customer_id = :cust,
                               presentation = CAST(:presentation AS jsonb)
            WHERE id = :id
            """
        ),
        {
            "token": public_token,
            "cust": (str(customer_ops_customer_id)[:128] if customer_ops_customer_id else None),
            "presentation": presentation_json,
            "id": str(invoice_id),
        },
    )

    # ---- Draft-only (quotes seam): NO journal posting, journal_entry_id NULL ----
    if not post_journal:
        return await _invoice_view(session, invoice_id=str(invoice_id))

    # ---- Post the journal entry (DR AR / CR Revenue per account + tax row) ----
    # Group net-of-discount revenue by revenue account. We apply the (invoice-
    # level) discount proportionally across revenue accounts so the AR debit
    # equals subtotal - discount + tax and each row balances.
    payee = cust["display_name"]
    base_description = f"Invoice {invoice_number} to {payee}"

    # Sum line amounts per revenue canonical.
    rev_by_acct: dict[str, Decimal] = {}
    for li in norm_lines:
        rev_by_acct[li.revenue_account_canonical] = _q(
            rev_by_acct.get(li.revenue_account_canonical, Decimal("0")) + li.amount
        )

    # Distribute discount proportionally to revenue weight; fix rounding on last.
    net_revenue_by_acct: dict[str, Decimal] = {}
    if totals.discount_total > 0 and totals.subtotal > 0:
        accts = list(rev_by_acct.items())
        running = Decimal("0")
        for idx, (acct, gross) in enumerate(accts):
            if idx == len(accts) - 1:
                net = _q(totals.subtotal - totals.discount_total - running)
            else:
                share = _q(gross / totals.subtotal * totals.discount_total)
                net = _q(gross - share)
                running = _q(running + net)
            net_revenue_by_acct[acct] = net
    else:
        net_revenue_by_acct = dict(rev_by_acct)

    issue_je_ids: list[str] = []
    # One balanced row per revenue account: DR AR / CR Revenue(net share).
    row_index = 0
    for acct_canonical, net_amount in net_revenue_by_acct.items():
        if net_amount == 0:
            continue
        row_index += 1
        je_id = await _post_balanced_row(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            debit_canonical=ar_canonical,
            credit_canonical=acct_canonical,
            amount=net_amount,
            effective_date=issue_date,
            source_external_id=f"invoice:{invoice_id}:rev{row_index}",
            description=base_description,
            payee=payee,
            memo=(f"PO {po_number}" if po_number else None),
            reasoning=(
                f"AR invoice {invoice_number} issued: DR Accounts Receivable / "
                f"CR {acct_canonical} (revenue). external_source={external_source}"
            ),
            actor_uc_uid=actor_uc_uid,
        )
        issue_je_ids.append(str(je_id))

    # One balanced tax row: DR AR / CR Sales Tax Payable.
    if totals.tax_total > 0 and tax_canonical is not None:
        tax_je_id = await _post_balanced_row(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            debit_canonical=ar_canonical,
            credit_canonical=tax_canonical,
            amount=totals.tax_total,
            effective_date=issue_date,
            source_external_id=f"invoice:{invoice_id}:tax",
            description=base_description,
            payee=payee,
            memo="Sales tax",
            reasoning=(
                f"AR invoice {invoice_number} sales tax: DR Accounts Receivable / "
                f"CR Sales Tax Payable."
            ),
            actor_uc_uid=actor_uc_uid,
        )
        issue_je_ids.append(str(tax_je_id))

    # Record the PRIMARY journal entry id on the invoice (the first revenue row;
    # falls back to the tax row for the degenerate tax-only case which
    # _compute_totals already forbids since total>0 implies revenue or tax).
    primary_je = issue_je_ids[0] if issue_je_ids else None
    await session.execute(
        text("UPDATE invoice SET journal_entry_id = :je WHERE id = :id"),
        {"je": primary_je, "id": str(invoice_id)},
    )

    return await _invoice_view(session, invoice_id=str(invoice_id))


async def issue_invoice_draft(
    session: AsyncSession,
    *,
    workspace_id: str,
    invoice_id: str,
    actor_uc_uid: str,
) -> dict[str, Any]:
    """Post the journal for a draft and mark it sent.

    Idempotent: an invoice that already has journal_entry_id is returned as-is.
    The invoice number was assigned when the draft was created.
    """
    locked = (
        await session.execute(
            text(
                """
                SELECT id::text, entity_id::text AS entity_id, invoice_number, status,
                       issue_date, subtotal, discount_total, tax_total, po_number,
                       external_source, journal_entry_id::text AS journal_entry_id
                FROM invoice
                WHERE id = :id
                FOR UPDATE
                """
            ),
            {"id": invoice_id},
        )
    ).mappings().first()
    if locked is None:
        raise InvoiceError(f"invoice {invoice_id} not found")
    if locked["journal_entry_id"]:
        return await _invoice_view(session, invoice_id=invoice_id)
    if locked["status"] == "void":
        raise InvoiceError("cannot issue a voided invoice")

    lines = (
        await session.execute(
            text(
                """
                SELECT amount, tax_amount, revenue_account_canonical
                FROM invoice_line WHERE invoice_id = :id
                ORDER BY line_number ASC
                """
            ),
            {"id": invoice_id},
        )
    ).mappings().all()
    if not lines:
        raise InvoiceError("invoice has no lines to issue")

    entity_id = str(locked["entity_id"])
    ar_canonical = await _ensure_account(
        session, workspace_id=workspace_id, entity_id=entity_id, account_number=ACCT_AR
    )
    rev_by_acct: dict[str, Decimal] = {}
    tax_total = Decimal("0")
    for line in lines:
        canonical = str(line["revenue_account_canonical"])
        rev_by_acct[canonical] = _q(
            rev_by_acct.get(canonical, Decimal("0")) + _to_decimal(line["amount"], "amount")
        )
        tax_total = _q(tax_total + _to_decimal(line["tax_amount"] or 0, "tax_amount"))
    subtotal = _to_decimal(locked["subtotal"], "subtotal")
    discount_total = _to_decimal(locked["discount_total"] or 0, "discount_total")
    net_revenue = _net_of_discount(rev_by_acct, subtotal, discount_total)
    tax_canonical = None
    if tax_total > 0:
        tax_canonical = await _ensure_account(
            session, workspace_id=workspace_id, entity_id=entity_id, account_number=ACCT_SALES_TAX
        )

    issue_date = locked["issue_date"]
    if isinstance(issue_date, datetime):
        issue_date = issue_date.date()
    invoice_number = locked["invoice_number"]
    payee = str(invoice_number)
    external_source = locked["external_source"] or "manual"
    po_number = locked["po_number"]
    base_description = f"Invoice {invoice_number}"
    issue_je_ids: list[str] = []
    row_index = 0
    for acct_canonical, net_amount in net_revenue.items():
        if net_amount == 0:
            continue
        row_index += 1
        je_id = await _post_balanced_row(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            debit_canonical=ar_canonical,
            credit_canonical=acct_canonical,
            amount=net_amount,
            effective_date=issue_date,
            source_external_id=f"invoice:{invoice_id}:rev{row_index}",
            description=base_description,
            payee=payee,
            memo=(f"PO {po_number}" if po_number else None),
            reasoning=(
                f"AR invoice {invoice_number} issued: DR Accounts Receivable / "
                f"CR {acct_canonical}. external_source={external_source}"
            ),
            actor_uc_uid=actor_uc_uid,
        )
        issue_je_ids.append(str(je_id))
    if tax_total > 0 and tax_canonical is not None:
        tax_je_id = await _post_balanced_row(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            debit_canonical=ar_canonical,
            credit_canonical=tax_canonical,
            amount=tax_total,
            effective_date=issue_date,
            source_external_id=f"invoice:{invoice_id}:tax",
            description=base_description,
            payee=payee,
            memo="Sales tax",
            reasoning=f"AR invoice {invoice_number} sales tax.",
            actor_uc_uid=actor_uc_uid,
        )
        issue_je_ids.append(str(tax_je_id))

    primary_je = issue_je_ids[0] if issue_je_ids else None
    await session.execute(
        text(
            """
            UPDATE invoice
               SET journal_entry_id = :je, status = 'sent'
             WHERE id = :id AND journal_entry_id IS NULL
            """
        ),
        {"je": primary_je, "id": invoice_id},
    )
    return await _invoice_view(session, invoice_id=invoice_id)


def _net_of_discount(
    rev_by_acct: dict[str, Decimal], subtotal: Decimal, discount_total: Decimal
) -> dict[str, Decimal]:
    if discount_total > 0 and subtotal > 0:
        net: dict[str, Decimal] = {}
        running = Decimal("0")
        accts = list(rev_by_acct.items())
        for idx, (acct, gross) in enumerate(accts):
            if idx == len(accts) - 1:
                net[acct] = _q(subtotal - discount_total - running)
            else:
                share = _q(gross / subtotal * discount_total)
                amount = _q(gross - share)
                running = _q(running + amount)
                net[acct] = amount
        return net
    return dict(rev_by_acct)


# ---------------------------------------------------------------------------
# get_invoice
# ---------------------------------------------------------------------------


async def get_invoice(
    session: AsyncSession,
    *,
    workspace_id: str,
    invoice_id: str | None = None,
    external_source: str | None = None,
    external_ref: str | None = None,
) -> dict[str, Any]:
    """Fetch a full invoice by id OR by (external_source, external_ref)."""
    if invoice_id is None:
        if not (external_source and external_ref):
            raise InvoiceError(
                "get_invoice requires invoice_id OR (external_source + external_ref)"
            )
        row = await _fetch_invoice_row(
            session,
            workspace_id=workspace_id,
            external_source=external_source,
            external_ref=external_ref,
        )
        if row is None:
            raise InvoiceError("invoice not found")
        invoice_id = str(row["id"])
    return await _invoice_view(session, invoice_id=invoice_id)


# ---------------------------------------------------------------------------
# record_payment (DR Cash / CR AR)
# ---------------------------------------------------------------------------


def _status_after_payment(total: Decimal, paid: Decimal, current: str) -> str:
    """Compute the invoice status after a payment, never un-voiding a void."""
    if current == "void":
        return "void"
    remaining = _q(total - paid)
    if remaining <= 0:
        return "paid"
    if paid > 0:
        return "partial"
    return current


async def record_payment(
    session: AsyncSession,
    *,
    workspace_id: str,
    invoice_id: str,
    actor_uc_uid: str,
    amount: Any,
    method: str = "manual",
    received_date: date | None = None,
    external_ref: str | None = None,
    cash_account_number: str = ACCT_CASH_DEFAULT,
    memo: str | None = None,
) -> dict[str, Any]:
    """Record a payment received against an invoice → DR Cash / CR AR.

    For payments captured OUTSIDE AO (manual "mark paid", or a bank deposit not
    yet auto-reconciled). Idempotent on (invoice_id, external_ref) when external_ref
    is given. Updates the invoice balance_due + status. Never overpays: the
    payment is capped at the remaining balance and rejects a zero/negative amount.
    """
    received_date = received_date or business_today()
    if external_ref is not None:
        external_ref = str(external_ref).strip()
        if not external_ref:
            raise InvoiceError("external_ref cannot be blank when supplied")
        if len(external_ref) > 256:
            raise InvoiceError("external_ref cannot exceed 256 characters")
    pay_amount = _q(_to_decimal(amount, "amount"))
    if pay_amount <= 0:
        raise InvoiceError("payment amount must be positive")

    inv = (
        await session.execute(
            text(
                "SELECT id::text, entity_id::text AS entity_id, invoice_number, "
                "status, total, balance_due, currency FROM invoice WHERE id = :id"
            ),
            {"id": invoice_id},
        )
    ).mappings().first()
    if inv is None:
        raise InvoiceError(f"invoice {invoice_id} not found")
    if inv["status"] == "void":
        raise InvoiceError("cannot record a payment against a voided invoice")
    entity_id = inv["entity_id"]
    balance_due = _q(_to_decimal(inv["balance_due"], "balance_due"))
    total = _q(_to_decimal(inv["total"], "total"))
    if balance_due <= 0:
        raise InvoiceError("invoice is already fully paid")
    if pay_amount > balance_due:
        raise InvoiceError(
            f"payment {pay_amount} exceeds the remaining balance {balance_due}"
        )

    # Idempotency: same (invoice_id, external_ref) returns the existing view.
    if external_ref:
        dup = (
            await session.execute(
                text(
                    "SELECT id::text FROM invoice_payment "
                    "WHERE invoice_id = :iid AND external_ref = :ref LIMIT 1"
                ),
                {"iid": invoice_id, "ref": external_ref},
            )
        ).first()
        if dup is not None:
            return await _invoice_view(session, invoice_id=invoice_id)

    cash_canonical = await _ensure_account(
        session, workspace_id=workspace_id, entity_id=entity_id,
        account_number=cash_account_number,
    )
    ar_canonical = await _ensure_account(
        session, workspace_id=workspace_id, entity_id=entity_id, account_number=ACCT_AR
    )

    payment_id = uuid4()
    src_ext = f"payment:{payment_id}"
    je_id = await _post_balanced_row(
        session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        debit_canonical=cash_canonical,
        credit_canonical=ar_canonical,
        amount=pay_amount,
        effective_date=received_date,
        source_external_id=src_ext,
        description=f"Payment on invoice {inv['invoice_number']}",
        payee=None,
        memo=memo or f"{method} payment",
        reasoning=(
            f"AR payment on invoice {inv['invoice_number']}: DR Cash / CR Accounts "
            f"Receivable. method={method}"
        ),
        actor_uc_uid=actor_uc_uid,
    )

    await session.execute(
        text(
            """
            INSERT INTO invoice_payment (
                id, workspace_id, entity_id, invoice_id, amount, method,
                received_date, journal_entry_id, external_ref, memo, created_by_uc_uid
            ) VALUES (
                :id, :wid, :eid, :iid, :amt, :method,
                :rdate, :je, :ref, :memo, :uid
            )
            """
        ),
        {
            "id": str(payment_id),
            "wid": workspace_id,
            "eid": entity_id,
            "iid": invoice_id,
            "amt": pay_amount,
            "method": method[:32],
            "rdate": received_date,
            "je": str(je_id),
            "ref": external_ref,
            "memo": memo,
            "uid": actor_uc_uid,
        },
    )

    # Recompute paid + balance from the payment rows (authoritative).
    paid_row = (
        await session.execute(
            text(
                "SELECT COALESCE(sum(amount), 0) AS paid FROM invoice_payment "
                "WHERE invoice_id = :iid"
            ),
            {"iid": invoice_id},
        )
    ).mappings().first()
    paid = _q(_to_decimal(paid_row["paid"], "paid"))
    new_balance = _q(total - paid)
    new_status = _status_after_payment(total, paid, inv["status"])
    await session.execute(
        text("UPDATE invoice SET balance_due = :bal, status = :st WHERE id = :id"),
        {"bal": new_balance, "st": new_status, "id": invoice_id},
    )

    return await _invoice_view(session, invoice_id=invoice_id)


# ---------------------------------------------------------------------------
# void_invoice (reversing entries — never overwrite a posted entry)
# ---------------------------------------------------------------------------


async def void_invoice(
    session: AsyncSession,
    *,
    workspace_id: str,
    invoice_id: str,
    actor_uc_uid: str,
    reason: str,
) -> dict[str, Any]:
    """Void an invoice by appending REVERSING journal entries (credit-note).

    For each original issue row we append a new entry with debit/credit FLIPPED
    and ``reverses_entry_id`` pointing at the original — we NEVER UPDATE/DELETE a
    posted entry. The invoice flips to status='void' and balance_due=0. Idempotent:
    re-voiding a void returns the existing view.
    """
    reason = (reason or "").strip()
    if not reason:
        raise InvoiceError("a void reason is required")

    inv = (
        await session.execute(
            text(
                "SELECT id::text, entity_id::text AS entity_id, invoice_number, "
                "status, total FROM invoice WHERE id = :id"
            ),
            {"id": invoice_id},
        )
    ).mappings().first()
    if inv is None:
        raise InvoiceError(f"invoice {invoice_id} not found")
    if inv["status"] == "void":
        return await _invoice_view(session, invoice_id=invoice_id)
    # Guard: a (partially) paid invoice should be credited via a payment reversal
    # flow, not a blunt void. For MVP we refuse to void an invoice that has any
    # payment recorded — the clean path is out of scope (credit-note w/ refund).
    paid_row = (
        await session.execute(
            text(
                "SELECT COALESCE(sum(amount),0) AS paid FROM invoice_payment "
                "WHERE invoice_id = :iid"
            ),
            {"iid": invoice_id},
        )
    ).mappings().first()
    if _q(_to_decimal(paid_row["paid"], "paid")) > 0:
        raise InvoiceError(
            "cannot void an invoice that has payments recorded; reverse the "
            "payment(s) first (refund flow is a later phase)"
        )

    entity_id = inv["entity_id"]

    # Fetch every original issue row for this invoice (the ones we posted at
    # issue time, identified by source + the invoice: source_external_id prefix).
    issue_rows = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, amount, debit_account, credit_account,
                       effective_date, payee, description, source_external_id
                FROM journal_entry
                WHERE entity_id = :eid AND source = :src
                  AND source_external_id LIKE :pfx
                  AND reverses_entry_id IS NULL
                ORDER BY posted_at ASC, id ASC
                """
            ),
            {
                "eid": entity_id,
                "src": INVOICE_SOURCE,
                "pfx": f"invoice:{invoice_id}:%",
            },
        )
    ).mappings().all()

    void_je_ids: list[str] = []
    for r in issue_rows:
        amount = _q(_to_decimal(r["amount"], "amount"))
        # Reversing row: flip DR/CR, point reverses_entry_id at the original.
        je_id = await _post_balanced_row(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            debit_canonical=r["credit_account"],
            credit_canonical=r["debit_account"],
            amount=amount,
            effective_date=r["effective_date"],
            source_external_id=f"void:{r['source_external_id']}",
            description=f"VOID invoice {inv['invoice_number']}: {reason[:200]}",
            payee=r["payee"],
            memo=f"Reversal of {r['id']}",
            reasoning=(
                f"AR invoice {inv['invoice_number']} voided — reversing entry of "
                f"{r['id']} (debit/credit flipped). Reason: {reason[:200]}"
            ),
            actor_uc_uid=actor_uc_uid,
            reverses_entry_id=UUID(r["id"]),
        )
        void_je_ids.append(str(je_id))

    await session.execute(
        text(
            """
            UPDATE invoice
            SET status = 'void', balance_due = 0, voided_at = now(),
                void_reason = :reason, void_journal_entry_id = :je
            WHERE id = :id
            """
        ),
        {
            "reason": reason,
            "je": void_je_ids[0] if void_je_ids else None,
            "id": invoice_id,
        },
    )
    return await _invoice_view(session, invoice_id=invoice_id)
