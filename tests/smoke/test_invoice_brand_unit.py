"""Pure checks for the branded invoice and the Stripe signature. No database."""
import json
from decimal import Decimal
import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api.invoices import _public_token_ok
from app.api.invoices import LineItemBody, RecordPaymentBody
from app.api.stripe_webhook import stripe_webhook as checkout_webhook
from app.api import stripe_webhook as stripe_webhook_module
from app.ar.invoice_brand.email_draft import stage_invoice_email
from app.ar.invoice_brand.template import render_invoice_html
from app.connectors.stripe import checkout
from app.connectors.stripe.checkout import create_checkout_session, verify_stripe_signature


def test_invoice_html_uses_the_special_projects_sheet():
    html = render_invoice_html(
        {
            "invoice_number": "MU-2026-0042",
            "currency": "USD",
            "total": "2500.00",
            "subtotal": "2500.00",
            "tax_total": "0.00",
            "balance_due": "2500.00",
            "issue_date": "2026-09-30",
            "customer_name": "River Green",
            "customer_email": "ap@rivergreen.test",
            "billing_address": "1 Demo St\nCharleston, SC",
            "legal_name": "Magic Unicorn Unconventional Technology & Stuff Inc",
            "invoice_address": "39 Paddlecreek Ave\nCharleston, SC 29412",
            "status": "draft",
            "lines": [
                {"description": "Setup and coordination", "quantity": "1", "amount": "2500.00"}
            ],
            "presentation": {
                "headline": "Setup and coordination",
                "due_label": "on receipt",
                "deliverables": ["Written quotes", "One page"],
                "not_included": ["The car"],
                "next_steps": ["You pay this invoice", "We get it in writing"],
                "contact_name": "Studio desk",
                "contact_email": "desk@example.test",
            },
        }
    )
    assert "Magic Unicorn" in html
    assert "Special Projects" in html
    assert "MU-2026-0042" in html
    assert "$2,500.00" in html
    assert "Due <b>on receipt</b>" in html
    assert "Magic Unicorn Unconventional Technology &amp; Stuff Inc" in html
    assert "magicunicorn.dev · Charleston, SC" in html
    assert "Written quotes" in html
    assert "The car" in html
    assert "Studio desk" in html
    assert "PREVIEW for Aaron" not in html
    assert "Magic Unicorn Inc." not in html
    assert "revolutionary" not in html.lower()
    assert "game-changer" not in html.lower()
    assert "data:image/png;base64," in html
    assert "@page{size:letter;margin:0}" in html
    assert "The pay link isn't connected yet." in html


def test_invoice_html_hides_the_contact_column_without_a_name():
    html = render_invoice_html(
        {
            "invoice_number": "MU-1",
            "total": "10.00",
            "balance_due": "10.00",
            "customer_name": "River Green",
            "lines": [{"description": "Work", "quantity": 1, "amount": "10.00"}],
        }
    )
    assert "Your contact" not in html


def test_logo_url_data_uri_is_used_as_stored(caplog):
    """A data: logo_url is used as-is, no warning, no fallback."""
    import logging

    from app.ar.invoice_brand.template import render_invoice_html

    uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    with caplog.at_level(logging.WARNING, logger="app.ar.invoice_brand.template"):
        html = render_invoice_html(
            {
                "invoice_number": "MU-2",
                "total": "10.00",
                "balance_due": "10.00",
                "customer_name": "River Green",
                "logo_url": uri,
                "lines": [{"description": "Work", "quantity": 1, "amount": "10.00"}],
            }
        )
    assert uri in html, "the data: logo was not used as stored"
    assert not any("logo_url" in rec.message for rec in caplog.records), (
        "a data: logo_url must not trigger a drop warning"
    )


def test_logo_url_http_is_dropped_with_a_warning(caplog):
    """An http(s) logo_url is dropped (the safe renderer refuses it) and the
    drop is logged so the operator knows.  The invoice falls back to the
    default logo.
    """
    import logging

    from app.ar.invoice_brand.template import render_invoice_html

    with caplog.at_level(logging.WARNING, logger="app.ar.invoice_brand.template"):
        html = render_invoice_html(
            {
                "invoice_number": "MU-3",
                "total": "10.00",
                "balance_due": "10.00",
                "customer_name": "River Green",
                "logo_url": "https://example.com/logo.png",
                "lines": [{"description": "Work", "quantity": 1, "amount": "10.00"}],
            }
        )
    assert any("logo_url" in rec.message for rec in caplog.records), (
        "the dropped logo_url was not logged"
    )
    assert "https://example.com/logo.png" not in html, (
        "the http logo_url was passed into the renderer input"
    )
    assert "data:image/png;base64," in html, "the default logo fallback was not used"


def test_logo_url_file_is_dropped_with_a_warning(caplog):
    """A file: logo_url is dropped and logged, like an http: one."""
    import logging

    from app.ar.invoice_brand.template import render_invoice_html

    with caplog.at_level(logging.WARNING, logger="app.ar.invoice_brand.template"):
        html = render_invoice_html(
            {
                "invoice_number": "MU-4",
                "total": "10.00",
                "balance_due": "10.00",
                "customer_name": "River Green",
                "logo_url": "file:///etc/hostname",
                "lines": [{"description": "Work", "quantity": 1, "amount": "10.00"}],
            }
        )
    assert any("logo_url" in rec.message for rec in caplog.records), (
        "the dropped logo_url was not logged"
    )
    assert "file:///etc/hostname" not in html


def test_stripe_signature_accepts_a_known_secret_and_rejects_the_rest():
    secret = "whsec_unit_test_only"
    payload = b'{"id":"evt_1","type":"checkout.session.completed"}'
    stamp = 1_700_000_000
    signed = f"{stamp}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={stamp},v1={digest}"
    assert verify_stripe_signature(payload, header, secret, now=stamp + 10) is True
    assert verify_stripe_signature(payload, header, "other", now=stamp + 10) is False
    assert verify_stripe_signature(payload, header, secret, now=stamp + 600) is False
    assert verify_stripe_signature(payload, "", secret, now=time.time()) is False
    assert verify_stripe_signature(payload, header, "", now=stamp) is False


def test_public_invoice_tokens_require_urlsafe_128_bit_length():
    assert not _public_token_ok("a" * 21)
    assert _public_token_ok("a" * 22)
    assert not _public_token_ok("é" * 22)
    assert not _public_token_ok("a" * 22 + "/")


def test_invoice_money_models_materialize_decimal_values():
    line = LineItemBody(description="Work", quantity="1", unit_price="10.005", amount="10.01")
    payment = RecordPaymentBody(amount="10.01")
    assert isinstance(line.unit_price, Decimal)
    assert isinstance(line.amount, Decimal)
    assert line.tax_amount == Decimal("0")
    explicit_zero = LineItemBody(description="Work", tax_amount="0.00")
    assert str(explicit_zero.tax_amount) == "0.00"
    assert isinstance(payment.amount, Decimal)


def test_invoice_line_tax_amount_rejects_negative_values():
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        LineItemBody(description="Work", tax_amount="-0.01")


@pytest.mark.asyncio
async def test_invoice_email_is_always_staged_even_if_send_mode_is_requested(monkeypatch):
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, json, headers):
            captured.update({"url": url, "json": json, "headers": headers})
            return _Response()

    monkeypatch.setenv("EMAIL_OPS_BASE_URL", "https://email-ops.test")
    monkeypatch.setenv("EMAIL_OPS_TOKEN", "unit-token")
    monkeypatch.setenv("EMAIL_OPS_WORKSPACE_ID", "workspace-1")
    monkeypatch.setenv("INVOICE_EMAIL_MODE", "send")
    monkeypatch.setattr("app.ar.invoice_brand.email_draft.httpx.AsyncClient", _Client)

    result = await stage_invoice_email(
        {
            "id": "invoice-1",
            "invoice_number": "INV-2026-0001",
            "customer_email": "ap@example.test",
            "total": Decimal("10.00"),
            "currency": "USD",
        }
    )
    assert result == "draft"
    assert captured["json"]["params"]["arguments"]["mode"] == "draft"


@pytest.mark.asyncio
async def test_checkout_uses_invoice_idempotency_key(monkeypatch):
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"url": "https://checkout.stripe.test/cs_test"}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, data, headers):
            captured.update({"url": url, "data": data, "headers": headers})
            return _Response()

    monkeypatch.setenv("STRIPE_CHECKOUT_SECRET_KEY", "sk_test_unit")
    monkeypatch.setattr(checkout.httpx, "AsyncClient", _Client, raising=False)
    url = await create_checkout_session(
        amount=Decimal("12.34"),
        currency="USD",
        invoice_id="invoice-1",
        workspace_id="workspace-1",
        invoice_number="INV-2026-0001",
        success_url="https://app.test/pay/abc",
        cancel_url="https://app.test/pay/abc",
    )
    assert url == "https://checkout.stripe.test/cs_test"
    assert captured["headers"]["Idempotency-Key"] == "invoice-checkout-invoice-1"


def _request_with_body(payload: bytes, headers: list[tuple[bytes, bytes]] | None = None) -> Request:
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
        "headers": headers or [],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
    }
    return Request(scope, receive)


@pytest.mark.asyncio
async def test_checkout_webhook_requires_the_env_signature_secret(monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    with pytest.raises(HTTPException) as exc:
        await checkout_webhook(_request_with_body(b"{}"))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_checkout_webhook_does_not_post_without_event_id(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_unit")
    monkeypatch.setattr(stripe_webhook_module, "verify_stripe_signature", lambda *args, **kwargs: True)

    async def unexpected_payment(*args, **kwargs):
        raise AssertionError("a webhook without an event id must not post")

    monkeypatch.setattr(stripe_webhook_module, "record_payment", unexpected_payment)
    payload = json.dumps(
        {
            "type": "checkout.session.completed",
            "data": {"object": {"amount_total": 100, "metadata": {"invoice_id": "i", "workspace_id": "w"}}},
        }
    ).encode()
    result = await checkout_webhook(_request_with_body(payload))
    assert result == {"received": True, "applied": False}
