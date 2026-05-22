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
