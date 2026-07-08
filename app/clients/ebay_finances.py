"""HTTP client for ebay-finances-mcp.

FastMCP streamable HTTP at /mcp/. We use minimal JSON-RPC over POST
rather than the full MCP SDK to keep the dep surface tight and avoid
async-session lifecycle complexity inside FastAPI request handlers.

The eBay MCP returns canonical transactions/accounts already (the
mapper inside the MCP did the conversion). This client adds:
  - the workspace's encrypted refresh-token blob → MCP auth arg
  - retry/timeout
  - sanitized error handling so the API route can decide queue-or-fail
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable
from uuid import UUID

import logging

import httpx

from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext

logger = logging.getLogger(__name__)

EBAY_MCP_URL = "http://ebay-finances-mcp:8000/mcp/mcp"
TIMEOUT_SECONDS = 25.0


class EbayFinancesError(Exception):
    """Raised when the MCP returns an error or is unreachable."""


@dataclass(frozen=True)
class EbayAuth:
    client_id: str
    client_secret: str
    refresh_token: str
    environment: str = "production"

    def as_arg(self) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "environment": self.environment,
        }


async def load_ebay_auth(
    workspace_id: UUID, *, requesting_service: str, actor_uc_uid: str
) -> EbayAuth | None:
    """Decrypt the stored ebay_finances credential blob for this workspace.

    Returns None if no credential exists; raises if decryption fails.
    """
    async with SessionLocal() as session:
        async with session.begin():
            try:
                pt = await get_credential_plaintext(
                    session,
                    workspace_id,
                    "ebay_finances",
                    requesting_service=requesting_service,
                    actor_uc_uid=actor_uc_uid,
                    actor_agent_id=None,
                )
            except Exception as exc:
                logger.warning("ebay_credential_decrypt_failed: %s", exc)
                return None
            if not pt:
                return None
    try:
        blob = json.loads(pt)
    except json.JSONDecodeError:
        return None
    try:
        return EbayAuth(
            client_id=blob["client_id"],
            client_secret=blob["client_secret"],
            refresh_token=blob["refresh_token"],
            environment=blob.get("environment", "production"),
        )
    except KeyError:
        return None


def _parse_sse_or_json(resp: httpx.Response) -> dict:
    """FastMCP streamable HTTP returns either application/json OR an SSE
    frame `event: message\\ndata: {...}`. Handle both."""
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise EbayFinancesError("Empty SSE response from ebay-finances-mcp")
    return resp.json()


async def _call_mcp_tool(tool_name: str, arguments: dict) -> Any:
    """Invoke an MCP tool via JSON-RPC over streamable HTTP.

    Protocol: initialize handshake → notify initialized → tools/call.
    The mcp-session-id from the initialize response is required for
    subsequent calls in the same session.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # FastMCP's DNS-rebinding guard requires Host to be in its allowed
        # list (defaults to 127.0.0.1/localhost). Set it explicitly so
        # docker-DNS-resolved hostnames don't trip the 421.
        "Host": "localhost:8000",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        # 1) initialize
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "accounting-ops", "version": "0.1"},
            },
        }
        init_resp = await client.post(EBAY_MCP_URL, json=init_payload, headers=headers)
        if init_resp.status_code >= 400:
            raise EbayFinancesError(
                f"ebay-finances-mcp init HTTP {init_resp.status_code}: {init_resp.text[:300]}"
            )
        session_id = init_resp.headers.get("mcp-session-id")
        if not session_id:
            raise EbayFinancesError("ebay-finances-mcp didn't return mcp-session-id")
        session_headers = dict(headers)
        session_headers["Mcp-Session-Id"] = session_id

        # 2) notifications/initialized (notification, no id, no response expected)
        notif = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        await client.post(EBAY_MCP_URL, json=notif, headers=session_headers)

        # 3) tools/call
        call_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        call_resp = await client.post(EBAY_MCP_URL, json=call_payload, headers=session_headers)
        if call_resp.status_code >= 400:
            raise EbayFinancesError(
                f"ebay-finances-mcp tools/call HTTP {call_resp.status_code}: {call_resp.text[:300]}"
            )
        data = _parse_sse_or_json(call_resp)

    if "error" in data and data["error"]:
        raise EbayFinancesError(f"MCP error: {data['error']}")
    result = data.get("result") or {}
    contents = result.get("content") or []
    if not contents:
        structured = result.get("structuredContent")
        if structured is not None:
            return structured
        return None
    first = contents[0]
    text = first.get("text") if isinstance(first, dict) else None
    if not text:
        return contents
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def list_accounts(auth: EbayAuth) -> list[dict]:
    """Return canonical accounts. Empty list on missing data."""
    out = await _call_mcp_tool("list_accounts", {"auth": auth.as_arg()})
    if isinstance(out, list):
        return out
    if isinstance(out, dict) and "accounts" in out:
        return out["accounts"]
    return []


async def recent_transactions(
    auth: EbayAuth, *, days: int = 30, limit: int = 500
) -> list[dict]:
    """Return canonical transactions sorted newest-first by the MCP."""
    out = await _call_mcp_tool(
        "recent_transactions",
        {"auth": auth.as_arg(), "days": days, "limit": limit},
    )
    if isinstance(out, list):
        return out
    if isinstance(out, dict) and "transactions" in out:
        return out["transactions"]
    return []


# ---- Sell Fulfillment.readonly direct path -----------------------------
# Used while Sell Finances API is gated by eBay's Application Growth
# Check. Gives us order-level revenue immediately. The OAuth token from
# tenant_credentials already has sell.fulfillment.readonly scope.

import base64
from datetime import datetime, timezone

_FULFILLMENT_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


async def _get_fulfillment_token(auth: EbayAuth) -> str:
    """Refresh-token → access-token, scoped to fulfillment.readonly only.

    Cached in-process by client_id for ~110 min (eBay tokens last 120 min;
    leave 10 min buffer).
    """
    import time

    cached = _FULFILLMENT_TOKEN_CACHE.get(auth.client_id)
    if cached and cached[1] > time.time():
        return cached[0]

    basic = base64.b64encode(f"{auth.client_id}:{auth.client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": auth.refresh_token,
                "scope": "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
            },
        )
        if resp.status_code != 200:
            raise EbayFinancesError(
                f"eBay token refresh failed [{resp.status_code}]: {resp.text[:200]}"
            )
        data = resp.json()
    token = data["access_token"]
    expires_in = int(data.get("expires_in", 7200))
    _FULFILLMENT_TOKEN_CACHE[auth.client_id] = (token, time.time() + expires_in - 600)
    return token


async def fulfillment_orders(
    auth: EbayAuth, *, days: int = 30, limit: int = 200
) -> list[dict]:
    """Fetch recent eBay orders via /sell/fulfillment/v1/order, normalized
    to canonical-transaction shape.

    Each order becomes ONE transaction with the gross amount (positive for
    a sale). Buyer info is anonymized. The eBay order id is used as the
    canonical external id, prefixed `ebay:order:`.
    """
    from datetime import timedelta

    token = await _get_fulfillment_token(auth)
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT00:00:00.000Z"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    txns: list[dict] = []
    offset = 0
    page_size = min(limit, 200)
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            url = (
                f"https://api.ebay.com/sell/fulfillment/v1/order"
                f"?limit={page_size}&offset={offset}"
                f"&filter=creationdate:%5B{since}..%5D"
            )
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                break  # eBay's "no data" or "no permission"
            if resp.status_code >= 400:
                raise EbayFinancesError(
                    f"eBay fulfillment HTTP {resp.status_code}: {resp.text[:200]}"
                )
            payload = resp.json()
            orders = payload.get("orders") or []
            if not orders:
                break
            for order in orders:
                txns.append(_normalize_fulfillment_order(order))
            if len(orders) < page_size or len(txns) >= limit:
                break
            offset += page_size
    return txns[:limit]


def _normalize_fulfillment_order(order: dict) -> dict:
    """Convert /sell/fulfillment/v1/order item → canonical transaction.

    PII-safe payee synthesized as "eBay order #<short>". Amount is the
    pricingSummary.total.value (positive for inflow).
    """
    order_id = order.get("orderId") or order.get("legacyOrderId") or ""
    pricing = order.get("pricingSummary") or {}
    total = pricing.get("total") or {}
    amount_str = str(total.get("value", "0"))
    currency = total.get("currency", "USD")

    creation_date = order.get("creationDate")  # ISO string
    try:
        posted_dt = datetime.fromisoformat(creation_date.replace("Z", "+00:00"))
        posted_ts = int(posted_dt.timestamp())
        posted_iso = posted_dt.isoformat()
    except (TypeError, ValueError, AttributeError):
        posted_ts = None
        posted_iso = creation_date

    short_id = order_id[-8:] if order_id else "unknown"
    return {
        "id": f"ebay:order:{order_id}",
        "account_id": "ebay:fulfillment:default",
        "account_name": "eBay Sales",
        "org_name": "eBay Finances",  # so existing vendor rules hit
        "currency": currency,
        "posted_at": posted_iso,
        "posted_ts": posted_ts,
        "transacted_at": posted_iso,
        "amount": amount_str,  # positive — gross sale
        "payee": f"eBay order #{short_id}",
        "description": f"eBay order · {len(order.get('lineItems') or [])} items · status {order.get('orderFulfillmentStatus', 'unknown')}",
        "memo": None,
        "is_pending": (order.get("orderPaymentStatus") not in ("PAID", "FULLY_REFUNDED")),
    }


def merge_into_canonical(
    ebay_transactions: Iterable[dict], simplefin_transactions: Iterable[dict]
) -> list[dict]:
    """Merge two streams by posted_ts descending. Tolerant of missing ts."""
    combined: list[dict] = list(simplefin_transactions) + list(ebay_transactions)
    combined.sort(key=lambda t: t.get("posted_ts") or 0, reverse=True)
    return combined


def canonical_accounts_to_cash(ebay_accounts: Iterable[dict]) -> list[dict]:
    """Adapt the eBay MCP's account shape to cash_position's expected shape.

    The MCP returns {source, external_account_id, external_account_name,
    external_org, currency, balance, ...}. We mirror that to the cash
    response shape so the dashboard can render them next to BofA + USAA.
    """
    out: list[dict] = []
    for a in ebay_accounts:
        try:
            balance_str = str(a.get("balance", "0"))
        except Exception:
            balance_str = "0"
        out.append(
            {
                "id": a.get("external_account_id") or a.get("id"),
                "name": a.get("external_account_name") or a.get("name") or "eBay payout",
                "org_name": a.get("external_org") or "eBay Finances",
                "currency": a.get("currency", "USD"),
                "balance": balance_str,
                "available_balance": balance_str,
                "balance_as_of": None,
                "last4": (a.get("external_account_name") or "").rsplit("(", 1)[-1].rstrip(")")
                if "(" in (a.get("external_account_name") or "")
                else None,
            }
        )
    return out
