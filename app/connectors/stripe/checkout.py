"""Stripe Checkout for an invoice pay link.

This module does not read the tenant Stripe credential blob. That blob is a
restricted read-only key shared with Ops-Center, majiks, and Prospector.
Checkout uses STRIPE_CHECKOUT_SECRET_KEY only. When it is unset, no link is
created and nothing here invents a URL.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal
from typing import Any

import httpx

_API = "https://api.stripe.com/v1/checkout/sessions"
_VERSION = "2024-06-20"


def checkout_configured() -> bool:
    return bool(os.environ.get("STRIPE_CHECKOUT_SECRET_KEY"))


def verify_stripe_signature(
    payload: bytes,
    header: str | None,
    secret: str | None,
    *,
    now: float | None = None,
    tolerance: int = 300,
) -> bool:
    """Stripe-Signature: t=<unix>,v1=<hex hmac sha256 of '{t}.{payload}'>."""
    if not secret or not header:
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts.setdefault(key.strip(), []).append(value.strip())
    stamps = parts.get("t") or []
    signatures = parts.get("v1") or []
    if not stamps or not signatures:
        return False
    try:
        stamp = int(stamps[0])
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - stamp) > tolerance:
        return False
    signed = f"{stamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


async def create_checkout_session(
    *,
    amount: Decimal,
    currency: str,
    invoice_id: str,
    workspace_id: str,
    invoice_number: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> str | None:
    """Create one Checkout Session for card and US bank (ACH). None when no key is set."""
    key = os.environ.get("STRIPE_CHECKOUT_SECRET_KEY")
    if not key:
        return None
    cents = int((amount * 100).to_integral_value())
    if cents <= 0:
        return None
    data = {
        "mode": "payment",
        "success_url": success_url,
        "cancel_url": cancel_url,
        "payment_method_types[0]": "card",
        "payment_method_types[1]": "us_bank_account",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": (currency or "USD").lower(),
        "line_items[0][price_data][unit_amount]": str(cents),
        "line_items[0][price_data][product_data][name]": f"Invoice {invoice_number}",
        "metadata[invoice_id]": invoice_id,
        "metadata[workspace_id]": workspace_id,
        "payment_intent_data[metadata][invoice_id]": invoice_id,
        "payment_intent_data[metadata][workspace_id]": workspace_id,
    }
    if customer_email:
        data["customer_email"] = customer_email
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                _API,
                data=data,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Stripe-Version": _VERSION,
                    "Idempotency-Key": f"invoice-checkout-{invoice_id}",
                },
            )
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    try:
        body: dict[str, Any] = response.json()
    except (TypeError, ValueError):
        return None
    url = body.get("url")
    return str(url) if url else None
