"""Unit tests for AR / Invoicing — no live Postgres.

Two tiers here:

  1. Pure-function tests (entitlement gate, totals arithmetic, payment-status
     transitions) — zero I/O, exercise the correctness-critical logic directly.

  2. Stubbed-session tests of the full create / idempotency / void flow. The
     AsyncSession is replaced with an in-memory fake that answers the COA +
     invoice lookups and records every INSERT/UPDATE so we can assert:
       - the issue posting BALANCES (sum DR == sum CR) and hits AR / Revenue /
         Sales-Tax-Payable correctly,
       - idempotency: a repeat create on the same (external_source, external_ref)
         returns the EXISTING invoice and posts NO second journal entry,
       - void appends REVERSING journal entries (debit/credit flipped, with
         reverses_entry_id set) and never updates a posted row.

The integration tier (tests/integration) exercises the same code against a live
hash chain; these stubbed tests keep the decision logic green on a laptop with
no stack (mirrors tests/smoke/test_stripe_mapper_unit.py).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.ar import entitlement
from app.ar import invoicing as ar


# ===========================================================================
# 1. Pure-function tests
# ===========================================================================


def test_entitlement_standalone_never_gated():
    # No claims at all, manual origin → must not raise.
    entitlement.assert_entitled_for_invoice(None, external_source="manual")
    entitlement.assert_entitled_for_invoice({}, external_source="stripe")


def test_entitlement_cross_app_requires_both_products():
    # Cross-app origin with NO entitlement claim → denied.
    with pytest.raises(entitlement.EntitlementError):
        entitlement.assert_entitled_for_invoice({}, external_source="project-ops")

    # Only one product → denied.
    with pytest.raises(entitlement.EntitlementError):
        entitlement.assert_entitled_for_invoice(
            {"entitlements": ["accounting-ops"]}, external_source="project-ops"
        )

    # BOTH products → allowed (no raise).
    entitlement.assert_entitled_for_invoice(
        {"entitlements": ["accounting-ops", "project-ops"]},
        external_source="project-ops",
    )


def test_entitlement_claim_shapes_are_forgiving():
    # space-string
    assert entitlement.workspace_has_products(
        {"entitlements": "accounting-ops project-ops"},
        "accounting-ops",
        "project-ops",
    )
    # nested subscription.products with dicts + aliases
    claims = {
        "subscription": {
            "products": [
                {"code": "AO", "active": True},
                {"code": "project_ops", "active": True},
            ]
        }
    }
    assert entitlement.workspace_has_products(
        claims, "accounting-ops", "project-ops"
    )
    # inactive product is not counted
    claims_inactive = {
        "products": [{"code": "project-ops", "active": False}, "accounting-ops"]
    }
    assert not entitlement.workspace_has_products(
        claims_inactive, "accounting-ops", "project-ops"
    )


def test_entitlement_open_mode(monkeypatch):
    # In 'open' bootstrap mode the cross-app path is allowed even w/o a claim.
    monkeypatch.setenv("AO_CROSS_APP_ENTITLEMENT_MODE", "open")
    entitlement.assert_entitled_for_invoice({}, external_source="project-ops")


def test_compute_totals_balances_and_quantizes():
    lines = [
        ar._NormLine(
            line_number=1, description="A", quantity=Decimal("2"),
            unit_price=Decimal("100"), amount=Decimal("200.00"),
            tax_code=None, tax_amount=Decimal("14.00"),
            revenue_account_id=None, revenue_account_canonical="4030·Rev",
            source_ref=None,
        ),
        ar._NormLine(
            line_number=2, description="B", quantity=Decimal("1"),
            unit_price=Decimal("50"), amount=Decimal("50.00"),
            tax_code=None, tax_amount=Decimal("0"),
            revenue_account_id=None, revenue_account_canonical="4030·Rev",
            source_ref=None,
        ),
    ]
    t = ar._compute_totals(lines, Decimal("0"))
    assert t.subtotal == Decimal("250.00")
    assert t.tax_total == Decimal("14.00")
    assert t.total == Decimal("264.00")
    # Total = subtotal - discount + tax  ⇒  the AR debit equals total (balanced).


def test_compute_totals_rejects_bad_discount_and_zero_total():
    line = ar._NormLine(
        line_number=1, description="A", quantity=Decimal("1"),
        unit_price=Decimal("10"), amount=Decimal("10.00"),
        tax_code=None, tax_amount=Decimal("0"),
        revenue_account_id=None, revenue_account_canonical="4030·Rev",
        source_ref=None,
    )
    with pytest.raises(ar.InvoiceError):
        ar._compute_totals([line], Decimal("20"))  # discount > subtotal
    with pytest.raises(ar.InvoiceError):
        ar._compute_totals([line], Decimal("10"))  # total == 0


def test_status_after_payment_transitions():
    # not fully paid → partial
    assert ar._status_after_payment(Decimal("100"), Decimal("40"), "sent") == "partial"
    # fully paid → paid
    assert ar._status_after_payment(Decimal("100"), Decimal("100"), "partial") == "paid"
    # over (defensive) → paid
    assert ar._status_after_payment(Decimal("100"), Decimal("120"), "sent") == "paid"
    # void stays void
    assert ar._status_after_payment(Decimal("100"), Decimal("50"), "void") == "void"
    # zero paid keeps current
    assert ar._status_after_payment(Decimal("100"), Decimal("0"), "draft") == "draft"


# ===========================================================================
# 2. Stubbed-session tests (create / idempotency / void)
# ===========================================================================


class _StubResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._rows[0] if self._rows else 0


class _Row(dict):
    """dict that also supports attribute + index access like a SA RowMapping."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(k) from e


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_COA = {
    "1100": "1100·Accounts Receivable",
    "2300": "2300·Sales Tax Payable",
    "4030": "4030·Revenue · Services",
    "1010": "1010·Cash · Primary Checking",
}
# canonical -> id for revenue resolution
_COA_IDS = {
    "1100": "acc-1100",
    "2300": "acc-2300",
    "4030": "acc-4030",
    "1010": "acc-1010",
}


class _StubSession:
    """In-memory fake AsyncSession covering the AR create/void flow.

    Tracks journal_entry inserts (the postings), invoice/line/payment inserts,
    and serves COA lookups + the idempotency invoice lookup. ``existing_invoice``
    lets a test simulate a pre-existing invoice for the idempotency path.
    """

    def __init__(self, *, existing_invoice: dict | None = None,
                 issue_rows_for_void: list[dict] | None = None):
        self.journal_entries: list[dict] = []
        self.invoices: list[dict] = []
        self.invoice_lines: list[dict] = []
        self.payments: list[dict] = []
        self.counterparties: list[dict] = []
        self.posting_log: list[dict] = []
        self.updates: list[str] = []
        self.public_tokens: set[str] = set()
        self._existing_invoice = existing_invoice
        self._issue_rows_for_void = issue_rows_for_void or []
        # invoice-number counter
        self._invoice_count = 0

    def begin_nested(self):
        return _Savepoint()

    async def execute(self, query, params=None):  # noqa: C901 - dispatch table
        sql = " ".join(str(query).split())
        p = params or {}

        # ---- chart_of_account: resolve canonical by account_number ----
        if "FROM chart_of_account" in sql and "account_number = :num" in sql and "id::text AS id" in sql:
            num = p.get("num")
            if num in _COA:
                return _StubResult([_Row(id=_COA_IDS[num], canonical_form=_COA[num])])
            return _StubResult([])
        if "FROM chart_of_account" in sql and "account_number = :num" in sql:
            num = p.get("num")
            if num in _COA:
                return _StubResult([_Row(canonical_form=_COA[num])])
            return _StubResult([])
        if "FROM chart_of_account" in sql and "id = :id" in sql:
            # explicit revenue_account_id lookup — accept acc-4030
            rid = p.get("id")
            for num, aid in _COA_IDS.items():
                if aid == rid:
                    return _StubResult([_Row(id=aid, canonical_form=_COA[num], account_type="revenue")])
            return _StubResult([])
        if "INSERT INTO chart_of_account" in sql:
            return _StubResult([])

        # ---- counterparty ----
        if "FROM counterparty" in sql:
            return _StubResult([])  # no existing customer → insert path
        if "INSERT INTO counterparty" in sql:
            self.counterparties.append(dict(p))
            return _StubResult([])
        if "UPDATE counterparty" in sql:
            self.updates.append("counterparty")
            return _StubResult([])

        # ---- invoice idempotency lookup (SELECT * FROM invoice WHERE ... external_ref) ----
        if "FROM invoice" in sql and "external_ref = :ref" in sql:
            if self._existing_invoice is not None:
                return _StubResult([_Row(**self._existing_invoice)])
            return _StubResult([])
        # invoice fetch by id (the _invoice_view header + the void/payment header reads)
        if "FROM invoice i" in sql and "i.id = :id" in sql:
            if "c.email AS" in sql or "c.billing_address AS" in sql:
                raise AssertionError("invoice view must read customer snapshots from external_refs")
            inv = self.invoices[-1] if self.invoices else None
            if inv is None:
                return _StubResult([])
            row = dict(inv)
            row.update(
                customer_name="Acme LLC",
                entity_display_name="Test Co",
                entity_slug="test-co",
                customer_email="ap@acme.test",
                customer_billing_address="1 Demo St",
            )
            row.setdefault("journal_entry_id", None)
            row.setdefault("public_token", None)
            return _StubResult([_Row(**row)])
        if "FROM invoice WHERE id = :id" in sql:
            inv = self.invoices[-1] if self.invoices else None
            return _StubResult([_Row(**inv)] if inv else [])
        # next-invoice-number count
        if "count(*) AS n FROM invoice" in sql:
            return _StubResult([_Row(n=self._invoice_count)])
        if "INSERT INTO invoice " in sql or "INSERT INTO invoice(" in sql:
            self._invoice_count += 1
            self.invoices.append(dict(p))
            return _StubResult([])
        if "INSERT INTO public.invoice_pay_token" in sql:
            token = p["token"]
            if token in self.public_tokens:
                return _StubResult([])
            self.public_tokens.add(token)
            return _StubResult([_Row(token=token)])
        if "UPDATE invoice" in sql:
            self.updates.append("invoice")
            if self.invoices:
                # Apply bound params (journal_entry_id, balance, status, reason).
                self.invoices[-1].update({k: v for k, v in p.items() if k != "id"})
                # The void UPDATE uses literal `status = 'void'` (not a bind) —
                # detect it from the SQL and reflect it onto the stub row.
                if "status = 'void'" in sql:
                    self.invoices[-1]["status"] = "void"
                    self.invoices[-1]["balance_due"] = Decimal("0")
                # record_payment / create stamp balance via :bal / :balance.
                if "bal" in p:
                    self.invoices[-1]["balance_due"] = p["bal"]
                if "st" in p:
                    self.invoices[-1]["status"] = p["st"]
                if "public_token" in sql and "token" in p:
                    self.invoices[-1]["public_token"] = p["token"]
            return _StubResult([])

        # ---- invoice_line ----
        if "INSERT INTO invoice_line" in sql:
            self.invoice_lines.append(dict(p))
            return _StubResult([])
        if "FROM invoice_line" in sql:
            return _StubResult([_Row(**ln) for ln in self.invoice_lines])

        # ---- invoice_payment ----
        if "INSERT INTO invoice_payment" in sql:
            self.payments.append(dict(p))
            return _StubResult([])
        if "sum(amount)" in sql and "invoice_payment" in sql:
            total = sum(Decimal(str(x["amt"])) for x in self.payments)
            return _StubResult([_Row(paid=total)])
        if "FROM invoice_payment" in sql:
            return _StubResult([_Row(**pay) for pay in self.payments])

        # ---- journal_entry ----
        if "this_hash IS NOT NULL" in sql:  # get_last_chain_hash
            return _StubResult([])  # genesis
        if "FROM journal_entry" in sql and "reverses_entry_id IS NULL" in sql:
            # void: list the original issue rows
            return _StubResult([_Row(**r) for r in self._issue_rows_for_void])
        if "INSERT INTO journal_entry" in sql:
            self.journal_entries.append(dict(p))
            return _StubResult([])
        if "INSERT INTO posting_log" in sql:
            self.posting_log.append(dict(p))
            return _StubResult([])

        return _StubResult([])


@pytest.mark.asyncio
async def test_create_invoice_posts_balanced_entry():
    """A taxed invoice posts balanced rows: DR AR == CR Revenue + CR Sales Tax."""
    s = _StubSession()
    view = await ar.create_invoice_from_draft(
        s,
        workspace_id="wid",
        entity_id="eid",
        actor_uc_uid="tester",
        external_source="manual",
        external_ref="ext-1",
        customer={"name": "Acme LLC", "email": "ap@acme.test"},
        line_items=[
            {"description": "Consulting", "quantity": 10, "unit_price": "100.00",
             "tax_amount": "70.00"},
        ],
        net_terms=30,
    )
    assert view["status"] in ("draft", "sent")
    # Two journal rows: one revenue, one tax.
    assert len(s.journal_entries) == 2, s.journal_entries

    # Each row is a balanced 2-account entry; verify the aggregate balances.
    dr: dict[str, Decimal] = {}
    cr: dict[str, Decimal] = {}
    for je in s.journal_entries:
        amt = Decimal(str(je["amount"]))
        dr[je["debit"]] = dr.get(je["debit"], Decimal(0)) + amt
        cr[je["credit"]] = cr.get(je["credit"], Decimal(0)) + amt
    assert sum(dr.values()) == sum(cr.values()), (dr, cr)
    # AR debited the full 1070; revenue 1000; sales tax 70.
    assert dr[_COA["1100"]] == Decimal("1070.00")
    assert cr[_COA["4030"]] == Decimal("1000.00")
    assert cr[_COA["2300"]] == Decimal("70.00")
    # Invoice header recorded total + balance.
    assert Decimal(str(s.invoices[-1]["total"])) == Decimal("1070.00")
    assert Decimal(str(s.invoices[-1]["balance"])) == Decimal("1070.00")


@pytest.mark.asyncio
async def test_create_invoice_untaxed_single_row():
    """An untaxed invoice posts ONE balanced row: DR AR / CR Revenue."""
    s = _StubSession()
    await ar.create_invoice_from_draft(
        s, workspace_id="wid", entity_id="eid", actor_uc_uid="tester",
        external_source="manual", external_ref=None,
        customer={"name": "Beta Inc"},
        line_items=[{"description": "Setup", "quantity": 1, "unit_price": "250.00"}],
    )
    assert len(s.journal_entries) == 1
    je = s.journal_entries[0]
    assert je["debit"] == _COA["1100"]
    assert je["credit"] == _COA["4030"]
    assert Decimal(str(je["amount"])) == Decimal("250.00")


@pytest.mark.asyncio
async def test_omitted_invoice_and_payment_dates_use_business_date(monkeypatch):
    expected = date(2026, 12, 31)
    monkeypatch.setattr(ar, "business_today", lambda: expected)
    s = _StubSession()

    view = await ar.create_invoice_from_draft(
        s,
        workspace_id="wid",
        entity_id="eid",
        actor_uc_uid="tester",
        external_source="manual",
        external_ref=None,
        customer={"name": "Business Date Customer"},
        line_items=[{"description": "Work", "quantity": 1, "unit_price": "10.00"}],
        post_journal=False,
    )
    assert s.invoices[-1]["issue"] == expected

    s.invoices[-1].update(
        entity_id="eid",
        invoice_number="INV-2026-0001",
        status="draft",
        total=s.invoices[-1]["total"],
        balance_due=s.invoices[-1]["balance"],
        currency="USD",
    )
    await ar.record_payment(
        s,
        workspace_id="wid",
        invoice_id=view["id"],
        actor_uc_uid="tester",
        amount="10.00",
    )
    assert s.payments[-1]["rdate"] == expected


@pytest.mark.asyncio
async def test_create_invoice_draft_does_not_post_until_issue():
    s = _StubSession()
    view = await ar.create_invoice_from_draft(
        s,
        workspace_id="wid",
        entity_id="eid",
        actor_uc_uid="tester",
        external_source="customer-ops",
        external_ref="quote-1",
        customer={"name": "Draft Customer"},
        line_items=[{"description": "Draft work", "quantity": 1, "unit_price": "125.00"}],
        post_journal=False,
    )
    assert view["status"] == "draft"
    assert view["journal_entry_id"] is None
    assert s.journal_entries == []


@pytest.mark.asyncio
async def test_public_token_collision_retries(monkeypatch):
    s = _StubSession()
    s.public_tokens.add("collision")
    candidates = iter(("collision", "fresh-token"))
    monkeypatch.setattr(ar.secrets, "token_urlsafe", lambda n: next(candidates))

    view = await ar.create_invoice_from_draft(
        s,
        workspace_id="wid",
        entity_id="eid",
        actor_uc_uid="tester",
        external_source="manual",
        external_ref=None,
        customer={"name": "Token Customer"},
        line_items=[{"description": "Token work", "quantity": 1, "unit_price": "10.00"}],
        post_journal=False,
    )
    assert view["public_token"] == "fresh-token"
    assert "fresh-token" in s.public_tokens


@pytest.mark.asyncio
async def test_invoice_view_uses_counterparty_external_refs_for_render_snapshots():
    s = _StubSession()
    s.invoices.append({"id": "invoice-1", "entity_id": "eid", "status": "draft"})
    view = await ar._invoice_view(s, invoice_id="invoice-1")
    assert view["customer_email"] == "ap@acme.test"
    assert view["customer_billing_address"] == "1 Demo St"


@pytest.mark.asyncio
async def test_create_invoice_idempotent_returns_existing_no_new_je():
    """Repeat create on the same (external_source, external_ref) returns the
    existing invoice and posts NO journal entry."""
    existing = {
        "id": "existing-invoice-id",
        "workspace_id": "wid",
        "entity_id": "eid",
        "counterparty_id": "cp-1",
        "invoice_number": "INV-2026-0001",
        "status": "sent",
        "currency": "USD",
        "issue_date": "2026-06-02",
        "due_date": "2026-07-02",
        "net_terms": 30,
        "subtotal": Decimal("1000.00"),
        "discount_total": Decimal("0"),
        "tax_total": Decimal("0"),
        "total": Decimal("1000.00"),
        "balance_due": Decimal("1000.00"),
        "notes": None,
        "po_number": None,
        "external_source": "manual",
        "external_ref": "ext-1",
        "journal_entry_id": "je-existing",
        "void_journal_entry_id": None,
        "voided_at": None,
        "void_reason": None,
        "hosted_pay_url": None,
        "pdf_key": None,
        "created_by_uc_uid": "tester",
    }
    s = _StubSession(existing_invoice=existing)
    # seed the invoices list so _invoice_view's by-id read finds it
    s.invoices.append(existing)
    view = await ar.create_invoice_from_draft(
        s, workspace_id="wid", entity_id="eid", actor_uc_uid="tester",
        external_source="manual", external_ref="ext-1",
        customer={"name": "Acme LLC"},
        line_items=[{"description": "dup", "quantity": 1, "unit_price": "1.00"}],
    )
    assert view["id"] == "existing-invoice-id"
    # NO journal entry posted on the idempotent repeat.
    assert s.journal_entries == []
    # NO new invoice inserted.
    assert all(i.get("id") == "existing-invoice-id" for i in s.invoices)


@pytest.mark.asyncio
async def test_void_appends_reversing_entries():
    """Void appends a reversing entry per issue row (debit/credit flipped, with
    reverses_entry_id set) and flips the invoice to void — never an UPDATE of a
    posted row."""
    # One unpaid invoice already issued; supply its two issue rows for the void
    # path to reverse.
    rev_row_id = "11111111-1111-1111-1111-111111111111"
    tax_row_id = "22222222-2222-2222-2222-222222222222"
    issue_rows = [
        {
            "id": rev_row_id, "amount": Decimal("1000.00"),
            "debit_account": _COA["1100"], "credit_account": _COA["4030"],
            "effective_date": "2026-06-02", "payee": "Acme LLC",
            "description": "Invoice INV-2026-0001", "source_external_id": "invoice:inv-1:rev1",
        },
        {
            "id": tax_row_id, "amount": Decimal("70.00"),
            "debit_account": _COA["1100"], "credit_account": _COA["2300"],
            "effective_date": "2026-06-02", "payee": "Acme LLC",
            "description": "Invoice INV-2026-0001", "source_external_id": "invoice:inv-1:tax",
        },
    ]
    s = _StubSession(issue_rows_for_void=issue_rows)
    # Seed an unpaid invoice header for the void's by-id reads.
    s.invoices.append({
        "id": "inv-1", "workspace_id": "wid", "entity_id": "eid",
        "counterparty_id": "cp-1", "invoice_number": "INV-2026-0001",
        "status": "sent", "currency": "USD",
        "issue_date": "2026-06-02", "due_date": "2026-07-02", "net_terms": 30,
        "subtotal": Decimal("1000.00"), "discount_total": Decimal("0"),
        "tax_total": Decimal("70.00"), "total": Decimal("1070.00"),
        "balance_due": Decimal("1070.00"), "notes": None, "po_number": None,
        "external_source": "manual", "external_ref": "ext-1",
        "journal_entry_id": rev_row_id, "void_journal_entry_id": None,
        "voided_at": None, "void_reason": None, "hosted_pay_url": None,
        "pdf_key": None, "created_by_uc_uid": "tester",
    })

    view = await ar.void_invoice(
        s, workspace_id="wid", invoice_id="inv-1", actor_uc_uid="tester",
        reason="customer cancelled",
    )
    # Two reversing entries appended, each with reverses_entry_id set + flipped.
    assert len(s.journal_entries) == 2, s.journal_entries
    rev1, rev2 = s.journal_entries
    assert rev1["reverses_entry_id"] == rev_row_id
    # Flipped: original DR AR / CR Revenue → reversal DR Revenue / CR AR.
    assert rev1["debit"] == _COA["4030"]
    assert rev1["credit"] == _COA["1100"]
    assert rev2["reverses_entry_id"] == tax_row_id
    assert rev2["debit"] == _COA["2300"]
    assert rev2["credit"] == _COA["1100"]
    # posting_log marks the void rows as 'reversed'.
    assert all(pl["outcome"] == "reversed" for pl in s.posting_log)
    assert view["status"] == "void"


@pytest.mark.asyncio
async def test_ar_tools_fail_closed_without_auth():
    """Every AR MCP tool DENIES (PermissionError) when no auth context is bound —
    it must never run unauthenticated (multi-tenant safety, parity with
    test_mcp_write_tools_authz)."""
    import uuid as _uuid

    from app.tools import (
        create_invoice_from_draft,
        get_invoice,
        record_payment,
        upsert_ar_customer,
        void_invoice,
    )

    wid, eid, iid = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
    with pytest.raises(PermissionError):
        await upsert_ar_customer(wid, eid, "Acme")
    with pytest.raises(PermissionError):
        await create_invoice_from_draft(
            wid, eid, "manual", None, {"name": "Acme"},
            [{"description": "x", "quantity": 1, "unit_price": "1.00"}],
        )
    with pytest.raises(PermissionError):
        await get_invoice(wid, iid)
    with pytest.raises(PermissionError):
        await void_invoice(wid, iid, "reason")
    with pytest.raises(PermissionError):
        await record_payment(wid, iid, "1.00")


@pytest.mark.asyncio
async def test_void_refuses_when_payments_exist():
    """A void on an invoice with payments is refused (refund flow is later)."""
    s = _StubSession()
    s.invoices.append({
        "id": "inv-2", "entity_id": "eid", "invoice_number": "INV-2026-0002",
        "status": "partial", "total": Decimal("100.00"),
    })
    # Simulate a recorded payment so the void guard trips.
    s.payments.append({"amt": Decimal("50.00")})
    with pytest.raises(ar.InvoiceError, match="payments"):
        await ar.void_invoice(
            s, workspace_id="wid", invoice_id="inv-2", actor_uc_uid="tester",
            reason="oops",
        )
    # No reversing entries posted.
    assert s.journal_entries == []
