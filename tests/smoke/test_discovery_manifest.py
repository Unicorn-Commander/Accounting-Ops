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
    # Must equal "RS256" exactly — Brigade's manifest validator requires it and
    # Brigade signs RS256. We still accept EdDSA too (forward-compat).
    assert fed["signing_algorithm"] == "RS256"
    assert "RS256" in fed["accepted_algorithms"]
    assert "EdDSA" in fed["accepted_algorithms"]


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


# ---------------------------------------------------------------------------
# Audit 2026-05-31 Bug 11 — every registered MCP tool MUST have an explicit
# entry in _TOOL_METADATA (no silent fall-through to the conservative
# admin/write/800ms default that hides real warnings + over-gates read tools).
# ---------------------------------------------------------------------------


async def test_every_registered_tool_has_explicit_metadata():
    """Bug 11: an MCP tool added to mcp_server.py MUST have a matching entry
    in app.api.discovery._TOOL_METADATA. The previous default-fallback
    behavior emitted a per-request WARN and stamped admin/write/800ms which
    (a) misclassifies read-only tools and (b) drowns out real warnings in
    the log stream.

    This test FAILS if a new tool is registered without metadata, so the
    discovery layer keeps coverage as the surface grows.
    """
    from mcp_server import build_server
    from app.api.discovery import _TOOL_METADATA

    server = build_server()
    live_tools = await server.list_tools()
    live_names = {t.name for t in live_tools}
    metadata_names = set(_TOOL_METADATA.keys())
    missing = live_names - metadata_names
    assert not missing, (
        f"{len(missing)} MCP tool(s) lack a _TOOL_METADATA entry "
        "(audit 2026-05-31 Bug 11). Add a row in app/api/discovery.py for: "
        f"{sorted(missing)}"
    )


async def test_metadata_min_roles_are_known_values():
    """Per-tool min_role must be one of the documented values so the
    scope/tier mapping (_SCOPE_FOR_ROLE / _tier_for) routes correctly."""
    from app.api.discovery import _SCOPE_FOR_ROLE, _TOOL_METADATA

    known_roles = set(_SCOPE_FOR_ROLE.keys())
    for tool_name, meta in _TOOL_METADATA.items():
        assert meta["min_role"] in known_roles, (
            f"{tool_name}: min_role={meta['min_role']!r} is not in "
            f"_SCOPE_FOR_ROLE ({sorted(known_roles)})"
        )
