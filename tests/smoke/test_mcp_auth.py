"""Smoke tests for the /mcp auth bridge (app/mcp_auth.py).

These run WITHOUT a live stack (no DB, no docker) by monkeypatching the
membership lookup, so they execute in CI and on a bare machine. They are the
security crux of the MCP mount: they prove that the caller's *token* — not the
tool's workspace_id/entity_id arguments — decides what is reachable, with
explicit NEGATIVE CONTROLS for every denial path (cross-workspace, out-of-scope
entity, insufficient role, consolidated-by-a-scoped-member, no auth context).
"""
from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import HTTPException

import app.mcp_auth as mcp_auth
from app.auth import AuthContext
from app.mcp_auth import MCPAuthMiddleware, authorize_workspace, current_auth

WS_A = uuid.uuid4()
WS_B = uuid.uuid4()
ENT_IN = uuid.uuid4()
ENT_OUT = uuid.uuid4()


def _auth(uc_uid: str = "tester") -> AuthContext:
    return AuthContext(uc_uid=uc_uid, agent_id=None, scopes=("service",), claims={})


def _set_auth(ctx: AuthContext | None):
    """Bind (or clear) the per-request AuthContext; returns the reset token."""
    return mcp_auth._current_auth.set(ctx)


def _membership(role: str, entity_scope=None):
    async def _fake(uc_uid, workspace_id):
        # Model the real _resolve_membership: 403 when no membership exists.
        if workspace_id != WS_A:
            raise HTTPException(status_code=403, detail="No active membership")
        return role, entity_scope, False, "ws-a"
    return _fake


# --------------------------------------------------------------------------
# authorize_workspace — the binding + denial logic
# --------------------------------------------------------------------------
async def test_no_auth_context_denies():
    token = _set_auth(None)
    try:
        with pytest.raises(PermissionError):
            await authorize_workspace(WS_A, ENT_IN)
    finally:
        mcp_auth._current_auth.reset(token)


async def test_member_of_a_allowed(monkeypatch):
    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("owner"))
    token = _set_auth(_auth())
    try:
        ctx = await authorize_workspace(WS_A, ENT_IN)
        assert ctx.workspace_id == WS_A
        assert ctx.role == "owner"
    finally:
        mcp_auth._current_auth.reset(token)


async def test_cross_workspace_denied(monkeypatch):
    """A caller with membership in A asking for B is denied — even though B is a
    perfectly valid tool argument. This is the core tenant-isolation control."""
    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("owner"))
    token = _set_auth(_auth())
    try:
        with pytest.raises(PermissionError):
            await authorize_workspace(WS_B, ENT_IN)
    finally:
        mcp_auth._current_auth.reset(token)


async def test_out_of_scope_entity_denied(monkeypatch):
    monkeypatch.setattr(
        mcp_auth, "_resolve_membership", _membership("accountant", (ENT_IN,))
    )
    token = _set_auth(_auth())
    try:
        # In-scope entity allowed.
        await authorize_workspace(WS_A, ENT_IN)
        # Out-of-scope entity denied.
        with pytest.raises(PermissionError):
            await authorize_workspace(WS_A, ENT_OUT)
    finally:
        mcp_auth._current_auth.reset(token)


async def test_insufficient_role_denied(monkeypatch):
    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("viewer"))
    token = _set_auth(_auth())
    try:
        with pytest.raises(PermissionError):
            await authorize_workspace(WS_A, ENT_IN, min_role="accountant")
    finally:
        mcp_auth._current_auth.reset(token)


async def test_consolidated_requires_unscoped(monkeypatch):
    """A scoped member must not get a full-workspace consolidated view."""
    monkeypatch.setattr(
        mcp_auth, "_resolve_membership", _membership("admin", (ENT_IN,))
    )
    token = _set_auth(_auth())
    try:
        with pytest.raises(PermissionError):
            await authorize_workspace(WS_A, min_role="admin", require_unscoped=True)
    finally:
        mcp_auth._current_auth.reset(token)


# --------------------------------------------------------------------------
# MCPAuthMiddleware — the ASGI bearer gate
# --------------------------------------------------------------------------
async def _inner(scope, receive, send):
    from starlette.responses import JSONResponse

    await JSONResponse({"saw_auth": current_auth() is not None})(scope, receive, send)


def _client():
    gated = MCPAuthMiddleware(_inner)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gated), base_url="http://mcp.test"
    )


async def test_middleware_rejects_missing_bearer():
    async with _client() as c:
        r = await c.get("/mcp")
        assert r.status_code == 401
        assert r.headers.get("WWW-Authenticate") == "Bearer"


async def test_middleware_rejects_garbage_bearer():
    async with _client() as c:
        r = await c.get("/mcp", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401


async def test_middleware_passes_valid_service_token(monkeypatch):
    """A valid service token reaches the inner app with the AuthContext bound."""
    import app.auth as auth_mod

    token = "x" * 40
    # Audit 2026-05-31 Bug 7: declare role_floor so the loader admits this
    # token in production mode. The actual authorization downstream uses
    # workspace_membership; role_floor just satisfies the "at least one scope
    # field set" requirement.
    monkeypatch.setenv(
        "ACCTOPS_SERVICE_TOKENS",
        '{"%s": {"uc_uid": "svc-user", "agent_id": "tester", "role_floor": "viewer"}}' % token,
    )
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)
    try:
        async with _client() as c:
            r = await c.get("/mcp", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200
            assert r.json() == {"saw_auth": True}
    finally:
        auth_mod._service_tokens_cache = None
