"""Contact-Ops federation client — pull a user's contacts into AO.

The long-term contacts story: Contact-Ops is the authoritative person directory
(it syncs iCloud / Microsoft / Gmail server-side). AO doesn't own contacts — it
references them. This module lets AO PULL a caller's people from Contact-Ops and
shape them into the same preview rows the file importer produces, so the user
confirms + tags before anything lands as a counterparty. Each imported row
carries ``contact_id = <contact-ops person id>`` so the two apps stay deduped.

Auth (uchub RFC-8693 on-behalf-of):
  1. AO holds the caller's access token (their uchub bearer).
  2. AO exchanges it at the uchub token endpoint for a token scoped to the
     Contact-Ops audience (grant_type=token-exchange).
  3. AO calls the Contact-Ops MCP (JSON-RPC) with that token.

Go-live is GATED (not by this code) on:
  * a uchub client registered for AO with token-exchange + the Contact-Ops
    audience allowed (needs a Keycloak admin), and
  * tenant alignment — the exchanged token must carry the tenant_id that owns
    the caller's Contact-Ops people.
Until a client secret + audience are configured, ``is_configured()`` is False
and the API surfaces a clean "not connected yet" message.

The pure functions (``parse_jsonrpc_result``, ``map_person_to_row``) are unit-
tested without any network; the I/O functions are thin wrappers over httpx.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import get_settings

_log = logging.getLogger("acctops.contact_ops")

_UA = "accounting-ops/0.1 (+https://accounting-ops.magicunicorn.dev)"
_TIMEOUT = 15.0


class ContactOpsError(RuntimeError):
    """Raised on any Contact-Ops federation failure (config or remote)."""


class ContactOpsNotConfigured(ContactOpsError):
    """Sync isn't wired up yet (no client secret / audience)."""


# ---------------------------------------------------------------------------
# Config gate
# ---------------------------------------------------------------------------


def is_configured() -> bool:
    """True iff we have enough to attempt a token-exchange + MCP call."""
    s = get_settings()
    secret = s.contact_ops_client_secret or s.keycloak_client_secret
    return bool(s.contact_ops_mcp_url and s.contact_ops_audience and secret)


def _exchange_client_creds() -> tuple[str, str]:
    s = get_settings()
    client_id = s.contact_ops_client_id or s.keycloak_client_id
    secret = s.contact_ops_client_secret or s.keycloak_client_secret
    if not secret:
        raise ContactOpsNotConfigured(
            "Contact-Ops sync is not connected yet (no client secret configured)."
        )
    return client_id, secret


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no I/O)
# ---------------------------------------------------------------------------


def parse_jsonrpc_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Unwrap an MCP JSON-RPC ``tools/call`` response to the tool's JSON output.

    MCP returns ``{"result": {"content": [{"type":"text","text":"<json>"}],
    "isError": false}}``. We decode that inner text. Raises ContactOpsError on
    a JSON-RPC error envelope or an ``isError`` result.
    """
    if "error" in payload and payload["error"]:
        err = payload["error"]
        raise ContactOpsError(
            f"Contact-Ops error {err.get('code')}: {err.get('message')}"
        )
    result = payload.get("result") or {}
    if result.get("isError"):
        raise ContactOpsError("Contact-Ops returned an error result")
    content = result.get("content") or []
    for block in content:
        if block.get("type") == "text" and block.get("text"):
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError as exc:
                raise ContactOpsError(f"Unparseable Contact-Ops payload: {exc}") from exc
    # Some tools may return structured content directly.
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    return {}


def _first_primary(items: list[dict[str, Any]] | None, value_key: str) -> str | None:
    """Pick the primary (or first) value from a list of attribute dicts."""
    if not items:
        return None
    primary = next((i for i in items if i.get("is_primary")), None)
    chosen = primary or items[0]
    val = chosen.get(value_key)
    return str(val) if val else None


def map_person_to_row(person: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Contact-Ops person (summary OR full) → an AO preview row.

    Tolerant of both ``list_people`` summaries (id, display_name,
    primary_email) and full ``get_person`` objects (emails[], phones[],
    current_employments[]). Returns None if there's no usable name.
    """
    person_id = person.get("id") or person.get("person_id")
    display = (person.get("display_name") or "").strip()
    if not display:
        given = (person.get("given_name") or "").strip()
        family = (person.get("family_name") or "").strip()
        display = " ".join(p for p in (given, family) if p).strip()
    if not display:
        return None

    email = person.get("primary_email") or _first_primary(person.get("emails"), "address")
    phone = _first_primary(person.get("phones"), "e164")

    # Company: from a current employment's org name when present.
    company = None
    employments = person.get("current_employments") or person.get("employments")
    if employments:
        first = employments[0]
        company = (
            first.get("organization_name")
            or (first.get("organization") or {}).get("display_name")
        )

    return {
        "display_name": display,
        "person_name": display,
        "company": company,
        "email": email,
        "phone": phone,
        "address": None,
        "suggested_kind": "customer",
        "source": "contact-ops",
        "contact_id": str(person_id) if person_id else None,
    }


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


async def exchange_token(subject_token: str) -> str:
    """uchub RFC-8693 on-behalf-of exchange → a Contact-Ops-audience token."""
    s = get_settings()
    client_id, client_secret = _exchange_client_creds()
    token_url = f"{s.keycloak_realm_url}/protocol/openid-connect/token"
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": client_id,
        "client_secret": client_secret,
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": s.contact_ops_audience,
        "scope": "person:read",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
        resp = await client.post(token_url, data=data)
    if resp.status_code != 200:
        _log.info("[contact-ops] token exchange failed: %s %s", resp.status_code, resp.text[:300])
        raise ContactOpsError(
            "Could not obtain a Contact-Ops access token (token-exchange "
            f"returned {resp.status_code}). The uchub client may not be "
            "registered for Contact-Ops yet."
        )
    tok = resp.json().get("access_token")
    if not tok:
        raise ContactOpsError("Token-exchange response had no access_token")
    return tok


async def _call_tool(obo_token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    s = get_settings()
    body = {
        "jsonrpc": "2.0",
        "id": "ao-sync",
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
        resp = await client.post(
            s.contact_ops_mcp_url,
            json=body,
            headers={"Authorization": f"Bearer {obo_token}"},
        )
    if resp.status_code != 200:
        raise ContactOpsError(
            f"Contact-Ops MCP call '{name}' returned {resp.status_code}"
        )
    return parse_jsonrpc_result(resp.json())


async def fetch_contact_rows(
    subject_token: str,
    *,
    query: str | None = None,
    tags_any: list[str] | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Pull the caller's people from Contact-Ops as AO preview rows.

    Uses ``search_people`` when a query is given, else ``list_people`` with a
    filter (Contact-Ops requires at least one filter on list_people). Follows
    ``next_cursor`` up to ``limit`` rows. Each row is shaped by
    ``map_person_to_row`` and carries ``contact_id``.
    """
    if not is_configured():
        raise ContactOpsNotConfigured(
            "Contact-Ops sync isn't connected yet. It needs a uchub client "
            "(token-exchange + Contact-Ops audience) and tenant alignment."
        )
    obo = await exchange_token(subject_token)

    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    use_search = bool(query)
    tool = "search_people" if use_search else "list_people"

    while len(rows) < limit:
        args: dict[str, Any] = {"limit": min(100, limit - len(rows))}
        if cursor:
            args["cursor"] = cursor
        if use_search:
            args["query"] = query
        else:
            # list_people requires a narrowing filter; default to any tag the
            # caller passed. (Pulling "everything" may need a Contact-Ops-side
            # affordance — see docs/CONTACT-OPS-SYNC.md.)
            args["tags_any"] = tags_any or []
        out = await _call_tool(obo, tool, args)
        items = out.get("items") or out.get("people") or []
        for p in items:
            row = map_person_to_row(p)
            if row is not None:
                rows.append(row)
        cursor = out.get("next_cursor")
        if not cursor or not items:
            break
    return rows


__all__ = (
    "ContactOpsError",
    "ContactOpsNotConfigured",
    "is_configured",
    "parse_jsonrpc_result",
    "map_person_to_row",
    "exchange_token",
    "fetch_contact_rows",
)
