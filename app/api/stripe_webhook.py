"""Stripe Checkout webhook. Verifies the signature, then records the payment.

Rejects when STRIPE_WEBHOOK_SECRET is unset. A repeated event id is a 200:
record_payment is idempotent on (invoice_id, external_ref).
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request, status

from app.ar.invoicing import InvoiceError, record_payment
from app.connectors.stripe.checkout import verify_stripe_signature
from app.core.business_time import business_today
from app.db import SessionLocal
from app.workspace_session import workspace_session

router = APIRouter(tags=["stripe-webhook"])


@router.post("/api/stripe/webhook")
async def stripe_webhook(request: Request) -> dict:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET") or ""
    payload = await request.body()
    header = request.headers.get("stripe-signature")
    if not verify_stripe_signature(payload, header, secret):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid signature")
    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid payload") from exc
    if event.get("type") != "checkout.session.completed":
        return {"received": True}
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        return {"received": True, "applied": False}
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    invoice_id = meta.get("invoice_id")
    workspace_id = meta.get("workspace_id")
    if not invoice_id or not workspace_id:
        return {"received": True, "applied": False}
    amount = Decimal(obj.get("amount_total") or 0) / Decimal(100)
    if amount <= 0:
        return {"received": True, "applied": False}
    try:
        async with workspace_session(SessionLocal, workspace_id) as session:
            await record_payment(
                session,
                workspace_id=str(workspace_id),
                invoice_id=str(invoice_id),
                actor_uc_uid="stripe-webhook",
                amount=amount,
                method="stripe",
                received_date=business_today(),
                external_ref=event_id.strip(),
                memo="Stripe Checkout",
            )
    except InvoiceError as exc:
        message = str(exc)
        if "already fully paid" in message or "exceeds the remaining balance" in message:
            return {"received": True, "applied": False}
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message) from exc
    return {"received": True, "applied": True}
