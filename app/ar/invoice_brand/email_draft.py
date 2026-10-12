"""Stage an invoice email draft in Email-Ops.

The invoice issue path is never an email-send path: this module only asks
Email-Ops to create a draft for later human approval. It skips cleanly when
Email-Ops is not configured or the customer has no address.
"""
from __future__ import annotations

import os
from typing import Any

import httpx


async def stage_invoice_email(view: dict[str, Any]) -> str:
    """Return draft|skipped. Never sends mail or logs the bearer."""
    base = (os.environ.get("EMAIL_OPS_BASE_URL") or "").rstrip("/")
    token = os.environ.get("EMAIL_OPS_TOKEN") or ""
    to_address = (view.get("customer_email") or "").strip()
    if not base or not token or not to_address:
        return "skipped"
    workspace_id = os.environ.get("EMAIL_OPS_WORKSPACE_ID") or str(view.get("workspace_id") or "")
    if not workspace_id:
        return "skipped"
    number = view.get("invoice_number") or "invoice"
    total = view.get("total")
    public_url = view.get("public_url") or ""
    lines = [
        f"Invoice {number}",
        f"Amount due: {total} {view.get('currency') or 'USD'}",
    ]
    if public_url:
        lines.append(f"View and pay: {public_url}")
    legal = view.get("legal_name") or view.get("entity_legal_name") or ""
    if legal:
        lines.append("")
        lines.append(str(legal))
    footer = view.get("footer_line") or ""
    if footer:
        lines.append(str(footer))
    args: dict[str, Any] = {
        "workspace_id": workspace_id,
        "to_address": to_address,
        "subject": f"Invoice {number}",
        "body": "\n".join(lines),
        "mode": "draft",
        "external_source": "accounting-ops",
        "external_ref": f"invoice:{view.get('id')}",
    }
    sender = (view.get("from_address") or "").strip()
    if sender:
        args["from_address"] = sender
    reply = (view.get("reply_to") or "").strip()
    if reply:
        args["reply_to"] = reply
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "compose_email", "arguments": args},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{base}/mcp",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except httpx.HTTPError:
        return "skipped"
    if response.status_code >= 400:
        return "skipped"
    return "draft"
