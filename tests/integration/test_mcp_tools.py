"""Integration tests for the mounted /mcp server (live stack).

Proves the MCP-first core end-to-end against the real app:
  1. /mcp is gated — no bearer -> 401 (the MCPAuthMiddleware is mounted).
  2. A uchub-realm caller (here the integration service token) can initialize
     the MCP session, discover the tools, and call get_p_and_l on a workspace it
     belongs to -> authorized success.
  3. NEGATIVE CONTROL: the SAME caller calling get_p_and_l on workspace B — a
     fully separate workspace it has no membership in — is denied. The tool
     errors (cross-tenant access is impossible even though B is a valid arg).

Skips cleanly when the stack isn't reachable (live_stack/seeded_workspace).
"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_B_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
)

pytestmark = pytest.mark.integration


async def test_mcp_endpoint_requires_bearer(live_stack):
    """Both spellings of the MCP endpoint reject an unauthenticated request."""
    async with httpx.AsyncClient(timeout=10) as c:
        for path in ("/mcp", "/mcp/"):
            r = await c.get(f"{live_stack.base_url}{path}")
            assert r.status_code == 401, f"GET {path} expected 401, got {r.status_code}"
            assert "location" not in r.headers
            r = await c.post(f"{live_stack.base_url}{path}")
            assert r.status_code == 401, f"POST {path} expected 401, got {r.status_code}"


async def _call_tool(base_url: str, token: str, name: str, args: dict):
    """Initialize an MCP streamable-HTTP session and call one tool."""
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(f"{base_url}/mcp", headers=headers) as (
        read,
        write,
        _,
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            assert "get_p_and_l" in tool_names, f"tools discovered: {tool_names}"
            return await session.call_tool(name, args)


def _result_payload(result) -> dict:
    """Extract the tool's dict result from a CallToolResult."""
    if getattr(result, "structuredContent", None):
        sc = result.structuredContent
        # FastMCP may nest a non-dict return under "result"; dicts come through flat.
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


async def test_mcp_get_p_and_l_authorized(seeded_workspace):
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_p_and_l",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "period": "2026",
        },
    )
    assert result.isError is False, result.content
    data = _result_payload(result)
    # Structurally a P&L: revenue/expenses/net_income present. (Empty ledger ->
    # zeros; the point is the authorized call returns a real, well-formed P&L.)
    assert "net_income" in data
    assert "revenue" in data and "expenses" in data


async def test_mcp_list_transactions_runs(seeded_workspace):
    """list_transactions executes its journal SQL against the real schema and
    returns a list (empty on a freshly-seeded workspace with no journal rows)."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "list_transactions",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "filter": {"limit": 10},
        },
    )
    assert result.isError is False, result.content
    payload = _result_payload(result)
    rows = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert isinstance(rows, list)


async def test_mcp_query_specialty_rules_runs(seeded_workspace):
    """query_specialty_rules executes its compliance_rule/profile join and returns
    a list (empty when none are configured)."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "query_specialty_rules",
        {"workspace_id": str(TEST_WORKSPACE_ID), "entity_id": str(TEST_ENTITY_ID)},
    )
    assert result.isError is False, result.content
    payload = _result_payload(result)
    rows = payload.get("result", payload) if isinstance(payload, dict) else payload
    assert isinstance(rows, list)


async def test_mcp_consolidated_p_and_l_runs(seeded_workspace):
    """The seeded caller is an unscoped owner, so the consolidated (cross-entity)
    P&L is allowed and returns a well-formed report."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_consolidated_p_and_l",
        {"workspace_id": str(TEST_WORKSPACE_ID), "period": "2026"},
    )
    assert result.isError is False, result.content
    data = _result_payload(result)
    assert "net_income" in data and "revenue" in data


async def test_mcp_cross_workspace_denied(seeded_workspace):
    """The token is a member of workspace A only. Asking for workspace B's P&L
    must be denied — B is a valid argument, but the caller has no membership."""
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_p_and_l",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "period": "2026",
        },
    )
    assert result.isError is True, "cross-workspace call should error"
    text = " ".join(
        getattr(c, "text", "") for c in result.content
    ).lower()
    assert "membership" in text or "permission" in text or "no active" in text
