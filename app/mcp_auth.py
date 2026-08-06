"""Authentication + authorization bridge for the mounted /mcp sub-app.

A FastMCP streamable-HTTP app mounted under /mcp is an independent ASGI app:
it does NOT run the parent FastAPI's ``Depends(validate_bearer_token)``. Without
the bridge in this module, the MCP tools would receive ``workspace_id`` /
``entity_id`` as free parameters with no tie to the caller's identity — any
authenticated uchub user could read any workspace (re-opening audit findings
C2/C3). This module closes that:

  1. ``MCPAuthMiddleware`` (pure ASGI, so it never buffers the MCP stream)
     validates the Bearer token via the same ``authenticate_token`` core the
     HTTP routes use, and stashes the resulting ``AuthContext`` in a contextvar
     for the duration of the request. No token / bad token -> JSON 401.

  2. ``authorize_workspace`` is called at the TOP of every tool body. It reads
     the contextvar, resolves the caller's workspace membership, and enforces
     role + entity_scope using the exact helpers the HTTP layer uses
     (``_resolve_membership``, ``require_role``, ``assert_entity_in_scope``).
     The caller's TOKEN decides what is reachable — never the tool arguments.

Denials are raised as ``PermissionError`` so a tool returns a clean error to the
agent rather than leaking a Starlette ``HTTPException`` through the MCP channel.
"""
from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

from fastapi import Depends, HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app.auth import AuthContext, authenticate_token, validate_bearer_token
from app.authz import assert_entity_in_scope
from app.tenancy import WorkspaceContext, _resolve_membership, require_role

# Set per-request by MCPAuthMiddleware; read by authorize_workspace. Defaults to
# None so a tool invoked outside the middleware (e.g. a stdio transport) fails
# closed rather than running unauthenticated.
_current_auth: ContextVar[AuthContext | None] = ContextVar(
    "mcp_current_auth", default=None
)


def current_auth() -> AuthContext | None:
    """Return the AuthContext bound to the current MCP request, if any."""
    return _current_auth.get()


async def bind_http_caller(auth: AuthContext = Depends(validate_bearer_token)):
    """FastAPI dependency that bridges the HTTP-validated bearer identity into the
    SAME ``_current_auth`` contextvar ``MCPAuthMiddleware`` sets.

    ``app.tools`` functions authorize via ``authorize_workspace``, which reads that
    contextvar. Mounted under ``/mcp`` the middleware sets it; over plain HTTP (the
    ``app/api/tools.py`` routes) nothing did, so those routes raised
    ``PermissionError`` ("No authenticated caller") -> 500. Applied as a router
    dependency, this sets the contextvar from the request's already-validated token
    on entry and resets it on exit (no cross-request bleed), so the HTTP tool routes
    authorize identically to the MCP surface. ``validate_bearer_token`` is the same
    dependency ``resolve_workspace``/``resolve_entity`` use, so FastAPI resolves it
    once per request — no double validation.
    """
    reset = _current_auth.set(auth)
    try:
        yield
    finally:
        _current_auth.reset(reset)


def _bearer_from_scope(scope) -> str | None:
    headers = Headers(scope=scope)
    raw = headers.get("authorization")
    if not raw:
        return None
    parts = raw.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


class MCPAuthMiddleware:
    """Pure-ASGI bearer-auth gate for the mounted MCP app.

    A pure-ASGI middleware (rather than starlette BaseHTTPMiddleware) is
    required: BaseHTTPMiddleware buffers the response body, which breaks the
    streamable-HTTP / SSE transport MCP uses.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            # Lifespan / websocket pass through untouched.
            await self.app(scope, receive, send)
            return

        token = _bearer_from_scope(scope)
        if token is None:
            await self._deny(scope, receive, send, "Missing bearer token")
            return
        try:
            ctx = await authenticate_token(token)
        except HTTPException as exc:
            await self._deny(scope, receive, send, str(exc.detail))
            return
        except Exception:  # pragma: no cover - defensive; never 500 the gate
            await self._deny(scope, receive, send, "Token validation failed")
            return

        reset = _current_auth.set(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_auth.reset(reset)
            # Best-effort credit metering for customer uc_ keys only. Never
            # blocks or fails the response (already sent). Service tokens /
            # PATs / Keycloak sessions are billed elsewhere or unmetered here.
            try:
                if ctx.claims.get("uc_key") and ctx.claims.get("_uc_bearer"):
                    from app.security.uc_api_key import meter_uc_mcp_call
                    import asyncio

                    # Fire-and-forget: tool name is not available at the ASGI
                    # gate without buffering the body; meter as a generic call.
                    # Tool-level metering can be added later inside tools that
                    # care; this ensures every authenticated uc_ MCP request
                    # hits the shared credit ledger at least once.
                    path = scope.get("path", "") or ""
                    tool_hint = "mcp_request"
                    if "tools/call" in path:
                        tool_hint = "tools_call"
                    elif path.rstrip("/").endswith("/mcp") or path.rstrip("/").endswith("mcp"):
                        tool_hint = "mcp_http"
                    asyncio.create_task(
                        meter_uc_mcp_call(
                            token=str(ctx.claims["_uc_bearer"]),
                            tool=tool_hint,
                        )
                    )
            except Exception:  # pragma: no cover - never break the response path
                pass

    @staticmethod
    async def _deny(scope, receive, send, detail: str) -> None:
        response = JSONResponse(
            {"error": "unauthorized", "detail": detail},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


async def authorize_workspace(
    workspace_id: UUID,
    entity_id: UUID | str | None = None,
    *,
    min_role: str = "viewer",
    require_unscoped: bool = False,
) -> WorkspaceContext:
    """Authorize the current MCP caller for ``workspace_id`` (+ optional entity).

    Resolves the caller's membership and enforces role + entity_scope with the
    SAME helpers the HTTP layer uses, so the two surfaces can never diverge.

    Args:
        workspace_id: workspace the tool was asked to operate on.
        entity_id: concrete entity (UUID/str) for entity-scoped tools, or None
            for workspace-level tools. When set, an out-of-scope entity is
            denied.
        min_role: minimum workspace role required (viewer|accountant|admin|owner).
        require_unscoped: when True (consolidated/cross-entity tools), a member
            with a restricted entity_scope is denied a full-workspace view.

    Returns the authorized ``WorkspaceContext``.

    Raises ``PermissionError`` on any auth/authz failure (no auth context, no
    membership, insufficient role, out-of-scope entity).
    """
    auth = _current_auth.get()
    if auth is None:
        raise PermissionError("No authenticated caller for this MCP request")

    # Federation: a verified Brigade token carries the uc-registry tenant id as
    # its workspace_id. Map it to the internal AO workspace BEFORE membership +
    # RLS binding so AO's existing membership authz applies UNCHANGED (no bypass:
    # the delegated user must still be a member of the resolved workspace). The
    # tool-arg workspace_id must equal the token's scoped workspace_id claim, so a
    # federated caller can only act within the workspace Brigade scoped it to.
    # Federation (Brigade uc-registry → internal workspace mapping) is an
    # ENTERPRISE seam. Community Edition never mints federation tokens, so the
    # branch — and the app.platform.workspace_registry import — is skipped there.
    from app.config import get_settings as _get_settings

    if not _get_settings().is_community:
        from app.platform.workspace_registry import (
            federation_claim_workspace_id,
            is_federation_token,
            resolve_internal_workspace_id,
        )

        if is_federation_token(auth):
            claim_wsid = federation_claim_workspace_id(auth)
            if claim_wsid and str(workspace_id) != claim_wsid:
                raise PermissionError(
                    "workspace does not match the federation token's scoped workspace"
                )
            workspace_id = await resolve_internal_workspace_id(workspace_id)

    try:
        role, entity_scope, hipaa, slug = await _resolve_membership(
            auth.uc_uid, workspace_id, autoprovision=auth.may_autoprovision
        )
        ctx = WorkspaceContext(
            workspace_id=workspace_id,
            workspace_slug=slug,
            role=role,
            user_uc_uid=auth.uc_uid,
            agent_id=auth.agent_id,
            entity_scope=entity_scope,
            hipaa_protected=hipaa,
        )
        require_role(ctx, min_role)
        if require_unscoped and ctx.entity_scope is not None:
            raise HTTPException(
                status_code=403,
                detail="Consolidated views require unrestricted entity scope",
            )
        if entity_id is not None:
            assert_entity_in_scope(ctx, entity_id)
    except HTTPException as exc:
        # Translate the HTTP-layer denial into a transport-neutral error so the
        # MCP channel returns a clean tool error, not a Starlette exception.
        raise PermissionError(str(exc.detail)) from exc

    return ctx


__all__ = ("MCPAuthMiddleware", "authorize_workspace", "bind_http_caller", "current_auth")
