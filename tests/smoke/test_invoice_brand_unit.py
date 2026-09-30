"""Pure checks for the branded invoice and the Stripe signature. No database."""
import hashlib
import hmac
import time

from app.ar.invoice_brand.template import render_invoice_html
from app.connectors.stripe.checkout import verify_stripe_signature


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
