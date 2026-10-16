"""Regression tests for money-path business dates and lookback boundaries."""
from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pypdf import PdfReader
from starlette.requests import Request

from app.api import invoices
from app.api import stripe_webhook as stripe_webhook_module
from app.connectors.ebay import mapper as ebay_mapper
from app.connectors.ebay import reconcile as ebay_reconcile
from app.connectors.stripe import mapper as stripe_mapper
from app.connectors.stripe import reconcile as stripe_reconcile


def _request_with_body(payload: bytes) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/stripe/webhook",
        "headers": [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
    }
    return Request(scope, receive)


class _MappingsResult:
    def mappings(self):
        return self

    def all(self):
        return []


class _CaptureSession:
    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement, _params):
        self.statements.append(str(statement))
        return _MappingsResult()


def test_business_date_converts_instants_and_epochs_to_new_york():
    from app.core.business_time import business_date, business_today

    just_after_utc_midnight = datetime(2026, 10, 16, 1, tzinfo=timezone.utc)
    assert business_date(just_after_utc_midnight) == date(2026, 10, 15)
    assert business_date(int(just_after_utc_midnight.timestamp())) == date(2026, 10, 15)
    assert business_date(datetime(2026, 10, 16, 1)) == date(2026, 10, 15)

    late_october = datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)
    assert business_today(late_october) == date(2026, 10, 31)


def test_stripe_balance_transaction_timestamp_uses_business_date():
    instant = datetime(2026, 10, 16, 1, tzinfo=timezone.utc)
    assert stripe_mapper._parse_stripe_ts(int(instant.timestamp())) == date(2026, 10, 15)


def test_ebay_payout_timestamp_uses_business_date():
    assert ebay_mapper._parse_iso_date("2026-10-16T01:00:00Z") == date(2026, 10, 15)


def test_mapper_fallbacks_use_business_today(monkeypatch):
    expected = date(2026, 10, 15)
    monkeypatch.setattr(stripe_mapper, "business_today", lambda: expected)
    monkeypatch.setattr(ebay_mapper, "business_today", lambda: expected)

    assert stripe_mapper._effective_date({"created": "not-an-epoch"}) == expected
    assert ebay_mapper._effective_date({"transactionDate": "not-an-instant"}) == expected


@pytest.mark.asyncio
async def test_stripe_webhook_uses_business_received_date(monkeypatch):
    instant = datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)
    from app.core.business_time import business_today

    captured: dict[str, object] = {}

    async def fake_record_payment(_session, **kwargs):
        captured.update(kwargs)

    @asynccontextmanager
    async def fake_workspace_session(*_args, **_kwargs):
        yield object()

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_unit")
    monkeypatch.setattr(
        stripe_webhook_module,
        "verify_stripe_signature",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(stripe_webhook_module, "record_payment", fake_record_payment)
    monkeypatch.setattr(stripe_webhook_module, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(stripe_webhook_module, "business_today", lambda: business_today(instant))

    payload = json.dumps(
        {
            "id": "evt_money_date",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "amount_total": 100,
                    "metadata": {"invoice_id": "invoice-1", "workspace_id": "workspace-1"},
                }
            },
        }
    ).encode()

    result = await stripe_webhook_module.stripe_webhook(_request_with_body(payload))

    assert result == {"received": True, "applied": True}
    assert captured["received_date"] == date(2026, 10, 16)


def test_invoice_due_date_is_current_at_business_evening(monkeypatch):
    from app.core.business_time import business_today

    instant = datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(invoices, "business_today", lambda: business_today(instant))

    assert invoices._aging_bucket("2026-10-16", "open", "100.00") == "current"


def test_invoice_pdf_footer_uses_business_date(monkeypatch):
    from app.core.business_time import business_today

    instant = datetime(2026, 10, 17, 0, 30, tzinfo=timezone.utc)
    expected = business_today(instant)
    monkeypatch.setattr(invoices, "business_today", lambda: expected)

    pdf = invoices._render_invoice_pdf(
        {
            "invoice_number": "INV-2026-0010",
            "entity_display_name": "Magic Unicorn Inc",
            "customer_name": "River Green",
            "issue_date": "2026-10-01",
            "due_date": "2026-10-16",
            "lines": [],
            "subtotal": "0.00",
            "tax_total": "0.00",
            "total": "0.00",
            "balance_due": "0.00",
            "status": "draft",
        }
    )
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()

    assert expected.isoformat() in text


@pytest.mark.asyncio
async def test_reconcile_lookbacks_name_the_business_timezone():
    session = _CaptureSession()
    await ebay_reconcile.find_unmatched_ebay_payouts(session, entity_id=str(uuid4()))
    await ebay_reconcile.find_simplefin_bofa_inflows(session, entity_id=str(uuid4()))
    await stripe_reconcile.find_unmatched_stripe_payouts(session, entity_id=str(uuid4()))
    await stripe_reconcile.find_simplefin_bofa_inflows(session, entity_id=str(uuid4()))

    assert len(session.statements) == 4
    for statement in session.statements:
        assert "(now() AT TIME ZONE 'America/New_York')::date" in statement
        assert "now()::date" not in statement
