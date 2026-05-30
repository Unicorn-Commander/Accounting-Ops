"""Smoke tests for the Brigade discovery manifest at /.well-known/mcps.json.

Confirms:
  - Manifest structure matches the schema described in the brief.
  - Each tool in the live MCP server appears in the manifest.
  - Per-tool fields (scopes_required, tier_required, read_only, latency_ms,
    input_schema) are correctly derived from the per-tool metadata.
  - No auth is required to fetch the manifest.
"""
from __future__ import annotations

import httpx
import pytest

import app.api.discovery as discovery


@pytest.fixture(autouse=True)
def _reset_manifest_cache():
    discovery._reset_cache_for_tests()
    yield
    discovery._reset_cache_for_tests()


def _client():
    from starlette.applications import Starlette

    starlette_app = Starlette()
    starlette_app.include_router = discovery.router  # noqa: not callable; doc
    # Use a minimal FastAPI app that includes only the discovery router.
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(discovery.router)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_manifest_returns_200_no_auth():
    async with _client() as c:
        r = await c.get("/.well-known/mcps.json")
        assert r.status_code == 200


async def test_manifest_has_all_top_level_fields():
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    assert m["$schema"].endswith("mcps-schema-v1.json")
    assert m["name"] == "Accounting-Ops"
    assert m["version"]
    assert m["mcp_endpoint"].endswith("/mcp")
    assert m["tool_namespace"] == "account"
    assert m["contact"] == "support@magicunicorn.dev"
    assert m["resources"] == []
    assert m["prompts"] == []
    assert isinstance(m["tools"], list)
    assert len(m["tools"]) > 0


async def test_manifest_auth_block():
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    auth = m["auth"]
    assert auth["type"] == "pat"
    assert auth["prefix"] == "aops_pat_"
    assert auth["mint_url"].endswith("/settings/pats")
    fed = auth["federated_auth"]
    assert fed["type"] == "brigade_jwt"
    assert fed["trusted_issuer"] == "https://brigade.unicorncommander.ai"
    assert fed["jwks_url"].endswith("/jwks.json")
    assert fed["expected_audience"] == "accounting-ops"
    assert fed["signing_algorithm"] == "ed25519"
    assert "EdDSA" in fed["accepted_algorithms"]
    assert "RS256" in fed["accepted_algorithms"]


async def test_manifest_rate_limits():
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    rl = m["rate_limits"]
    assert rl["per_user_per_minute"] == 60
    assert rl["per_user_per_hour"] == 1000


async def test_each_tool_has_required_fields():
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    for tool in m["tools"]:
        assert tool["name"]
        assert "input_schema" in tool
        assert isinstance(tool["scopes_required"], list)
        assert tool["tier_required"] in {"free", "pro", "enterprise"}
        assert isinstance(tool["read_only"], bool)
        assert isinstance(tool["expected_latency_ms"], int)
        # Scopes must be one of the documented values.
        assert all(
            s in {"read:accounting", "write:accounting", "admin:accounting"}
            for s in tool["scopes_required"]
        )


async def test_read_tool_marked_free_and_read_only():
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    tools = {t["name"]: t for t in m["tools"]}
    # list_transactions is a viewer-floor pure read tool.
    lt = tools.get("list_transactions")
    assert lt is not None
    assert lt["read_only"] is True
    assert lt["tier_required"] == "free"
    assert lt["scopes_required"] == ["read:accounting"]
    assert lt["expected_latency_ms"] <= 500


async def test_write_tool_marked_pro_and_not_read_only():
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    tools = {t["name"]: t for t in m["tools"]}
    ct = tools.get("categorize_transaction")
    assert ct is not None
    assert ct["read_only"] is False
    assert ct["tier_required"] == "pro"
    assert ct["scopes_required"] == ["write:accounting"]


async def test_admin_tool_marked_enterprise():
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    tools = {t["name"]: t for t in m["tools"]}
    asr = tools.get("add_specialty_rule")
    assert asr is not None
    assert asr["tier_required"] == "enterprise"
    assert asr["scopes_required"] == ["admin:accounting"]


async def test_all_mcp_tools_appear_in_manifest():
    """Every tool registered in mcp_server.py must show up in the manifest."""
    from mcp_server import build_server

    server = build_server()
    live_tools = await server.list_tools()
    live_names = {t.name for t in live_tools}
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    manifest_names = {t["name"] for t in m["tools"]}
    assert live_names == manifest_names, (
        f"manifest and live MCP disagree:\n"
        f"  in MCP but not manifest: {live_names - manifest_names}\n"
        f"  in manifest but not MCP: {manifest_names - live_names}"
    )


async def test_tools_sorted_alphabetically():
    """Stable order for diff-friendly manifests."""
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    names = [t["name"] for t in m["tools"]]
    assert names == sorted(names)


async def test_input_schema_has_jsonschema_shape():
    """Each tool's input_schema must be a valid-looking JSONSchema fragment."""
    async with _client() as c:
        m = (await c.get("/.well-known/mcps.json")).json()
    for tool in m["tools"]:
        schema = tool["input_schema"]
        assert schema.get("type") == "object"
        assert "properties" in schema or schema == {"type": "object", "properties": {}}
