"""Stripe Checkout for an invoice pay link.

This module does not read the tenant Stripe credential blob. That blob is a
restricted read-only key shared with Ops-Center, majiks, and Prospector.
Checkout uses STRIPE_CHECKOUT_SECRET_KEY only. When it is unset, no link is
created and nothing here invents a URL.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from decimal import Decimal
from typing import Any

import httpx

_API = "https://api.stripe.com/v1/checkout/sessions"
_VERSION = "2024-06-20"

logger = logging.getLogger(__name__)

# Attempts for one Checkout Session create. Every attempt reuses the same
# Idempotency-Key (invoice-checkout-<invoice_id>), so a retry after a timeout
# whose first request did land returns the SAME session from Stripe and can
# never create a second one for the invoice.
_ATTEMPTS = 3
_BACKOFF_SECONDS = 0.5

PAY_LINK_UNAVAILABLE = "Pay link unavailable, retry issuing the invoice to create it."


class CheckoutUnavailableError(RuntimeError):
    """Checkout is configured but Stripe did not return a session URL."""


def checkout_return_url(public_base_url: str, invoice_id: str) -> str:
    """Stripe success / cancel URL for an invoice, built from its id only.

    The raw /pay/{token} value is never stored (only its hash), so a retry on a
    posted invoice could not rebuild it. This URL is stable across retries, so
    the Idempotency-Key invoice-checkout-<invoice_id> always sees the same
    parameters, and it carries no bearer secret into Stripe or our logs.
    """
    return f"{public_base_url.rstrip('/')}/pay/complete/{invoice_id}"


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
    """Create one Checkout Session for card and US bank (ACH).

    Returns None only when Checkout is not configured or nothing is owed.
    Raises CheckoutUnavailableError when Stripe fails after the retries, so a
    caller can surface "pay link unavailable" instead of a silent null link.
    """
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
    headers = {
        "Authorization": f"Bearer {key}",
        "Stripe-Version": _VERSION,
        "Idempotency-Key": f"invoice-checkout-{invoice_id}",
    }
    reason = "no attempt"
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(_API, data=data, headers=headers)
        except httpx.HTTPError as exc:
            reason = f"transport error {type(exc).__name__}"
        else:
            status = response.status_code
            if status < 400:
                try:
                    body: dict[str, Any] = response.json()
                except (TypeError, ValueError):
                    body = {}
                url = body.get("url") if isinstance(body, dict) else None
                if url:
                    return str(url)
                reason = "response carried no session url"
                break
            reason = f"HTTP {status}"
            if status != 429 and status < 500:
                # A 4xx other than 429 is not transient; retrying repeats it.
                break
        if attempt < _ATTEMPTS:
            await asyncio.sleep(_BACKOFF_SECONDS * attempt)
    logger.warning(
        "stripe checkout session failed for invoice %s (workspace %s): %s",
        invoice_id,
        workspace_id,
        reason,
    )
    raise CheckoutUnavailableError(reason)
