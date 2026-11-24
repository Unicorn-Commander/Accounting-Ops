"""AO-SECRET-1: eBay upstream error bodies never reach errors[] or the log.

GET /recent-transactions put up to 300 chars of the raw eBay / eBay-MCP error
body into ``errors[]`` for any workspace viewer, while the failing request
carried the client_secret (Basic) and the refresh_token. Now every
EbayFinancesError is fixed text plus the HTTP status; the log gets the body
length only. No network: httpx is driven by a MockTransport.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest

from app.api import transactions as tx
from app.clients import ebay_finances as ef
from app.tenancy import WorkspaceContext

SECRET = "FIXTURE-CLIENT-SECRET-7f3a"
REFRESH = "FIXTURE-REFRESH-TOKEN-91bc"
ECHO = f'{{"error":"invalid_client","client_secret":"{SECRET}","refresh_token":"{REFRESH}"}}'
AUTH = ef.EbayAuth(client_id="fixture-client", client_secret=SECRET, refresh_token=REFRESH)


def _install(monkeypatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(*a: Any, **k: Any) -> httpx.AsyncClient:
        k.pop("transport", None)
        return real(*a, transport=httpx.MockTransport(handler), **k)

    monkeypatch.setattr(ef.httpx, "AsyncClient", factory)
    ef._FULFILLMENT_TOKEN_CACHE.clear()


def _assert_clean(text: str) -> None:
    assert SECRET not in text
    assert REFRESH not in text
    assert "invalid_client" not in text


@pytest.mark.asyncio
async def test_token_refresh_failure_is_fixed_text(monkeypatch, caplog):
    _install(monkeypatch, lambda req: httpx.Response(401, text=ECHO))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ef.EbayFinancesError) as exc:
        await ef._get_fulfillment_token(AUTH)
    assert str(exc.value) == "eBay token refresh HTTP 401"
    _assert_clean(caplog.text)
    assert f"body_len={len(ECHO)}" in caplog.text


@pytest.mark.asyncio
async def test_fulfillment_failure_is_fixed_text(monkeypatch, caplog):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/oauth2/token"):
            return httpx.Response(200, json={"access_token": "fixture-access", "expires_in": 7200})
        return httpx.Response(403, text=ECHO)

    _install(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ef.EbayFinancesError) as exc:
        await ef.fulfillment_orders(AUTH)
    assert str(exc.value) == "eBay fulfillment HTTP 403"
    _assert_clean(caplog.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", ["initialize", "tools/call"])
async def test_mcp_http_failure_is_fixed_text(monkeypatch, caplog, fail_at):
    def handler(req: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(req.content or b"{}")
        method = body.get("method")
        if method == fail_at:
            return httpx.Response(500, text=ECHO)
        if method == "initialize":
            return httpx.Response(200, json={"result": {}}, headers={"mcp-session-id": "s-1"})
        return httpx.Response(202)

    _install(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ef.EbayFinancesError) as exc:
        await ef.recent_transactions(AUTH)
    _assert_clean(str(exc.value))
    assert str(exc.value).endswith("HTTP 500")
    _assert_clean(caplog.text)


@pytest.mark.asyncio
async def test_mcp_jsonrpc_error_keeps_only_the_code(monkeypatch, caplog):
    def handler(req: httpx.Request) -> httpx.Response:
        import json

        method = json.loads(req.content or b"{}").get("method")
        if method == "initialize":
            return httpx.Response(200, json={"result": {}}, headers={"mcp-session-id": "s-1"})
        if method == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2,
                                             "error": {"code": -32000, "message": ECHO}})
        return httpx.Response(202)

    _install(monkeypatch, handler)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ef.EbayFinancesError) as exc:
        await ef.recent_transactions(AUTH)
    assert str(exc.value) == "ebay-finances-mcp error (code -32000)"
    _assert_clean(caplog.text)


@pytest.mark.asyncio
async def test_route_errors_carry_no_upstream_body(monkeypatch, caplog):
    """The real handler, both eBay paths failing with an echoing body."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=ECHO)

    _install(monkeypatch, handler)

    async def none(*a, **k):
        return None

    async def empty_map(*a, **k):
        return {}

    async def empty_list(*a, **k):
        return []

    async def auth(*a, **k):
        return AUTH

    from app.api import account_mappings

    monkeypatch.setattr(tx, "_simplefin_access_url", none)
    monkeypatch.setattr(account_mappings, "get_account_to_entity_map", empty_map)
    monkeypatch.setattr(tx, "load_overrides_for_workspace", empty_list)
    monkeypatch.setattr(tx.ebay_finances, "load_ebay_auth", auth)
    ws = WorkspaceContext(uuid.uuid4(), "fixture", "viewer", "fixture-viewer", None, None, False)
    caplog.set_level(logging.DEBUG)
    out = await tx.workspace_recent_transactions(uuid.uuid4(), workspace=ws)
    joined = " | ".join(out["errors"])
    assert out["errors"], "both eBay paths failed; errors[] should say so"
    _assert_clean(joined)
    assert "HTTP 401" in joined
    _assert_clean(caplog.text)
