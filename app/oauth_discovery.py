"""OAuth protected-resource discovery and the MCP HTTP edge contract.

This module owns the RFC 9728 metadata routes and the challenge emitted by the
MCP bearer gate. Keeping both in one place makes the resource URL, issuer, and
realm stable across the dogfood and production rings while the app's existing
configuration supplies the ring-specific values.
"""
from __future__ import annotations

from collections.abc import Iterable

from fastapi import APIRouter
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import Receive, Scope, Send

from app.config import get_settings

MCP_PATH = "/mcp"
MCP_REALM = "accounting-ops-mcp"
MCP_CORS_METHODS = "GET, POST, DELETE, OPTIONS"
MCP_CORS_HEADERS = (
    "Authorization, Content-Type, Accept, Mcp-Session-Id, "
    "Mcp-Protocol-Version, Last-Event-ID"
)
MCP_CORS_EXPOSE_HEADERS = "Mcp-Session-Id"
MCP_RESOURCE_DOCUMENTATION = "https://unicorncommander.ai/platform/bring-your-own-ai"
# Exactly what Keycloak's anonymous-DCR policy allows; the realm scope `mcp` carries this app's audience + uc_uid.
# App scopes such as accounting:* here make DCR registration fail (403 "Not permitted to use specified clientScope").
MCP_SCOPES = ("openid", "profile", "email", "offline_access", "mcp")

router = APIRouter(tags=["oauth-discovery"])


def canonical_mcp_url(trailing_slash: bool = False) -> str:
    """Return the configured public MCP URL.

    Both ``/mcp`` and ``/mcp/`` serve MCP (no redirect: behind Traefik the app
    sees http, and every existing client already uses ``/mcp/``), so each form
    gets metadata naming itself.
    """
    base = f"{str(get_settings().public_base_url).rstrip('/')}{MCP_PATH}"
    return f"{base}/" if trailing_slash else base


def protected_resource_metadata_url(trailing_slash: bool = False) -> str:
    """Return the path-suffixed RFC 9728 URL for the MCP path the client used."""
    base = str(get_settings().public_base_url).rstrip("/")
    return f"{base}/.well-known/oauth-protected-resource{MCP_PATH}" + ("/" if trailing_slash else "")


def _metadata_document(trailing_slash: bool = False) -> dict[str, object]:
    settings = get_settings()
    return {
        "resource": canonical_mcp_url(trailing_slash),
        "authorization_servers": [str(settings.keycloak_realm_url).rstrip("/")],
        "bearer_methods_supported": ["header"],
        "scopes_supported": list(MCP_SCOPES),
        "resource_name": "Accounting-Ops MCP",
        "resource_documentation": MCP_RESOURCE_DOCUMENTATION,
    }


def _metadata_response(trailing_slash: bool = False) -> JSONResponse:
    return JSONResponse(
        _metadata_document(trailing_slash),
        headers={
            # RFC 9728 metadata is public and must be readable by hosted clients.
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": MCP_CORS_HEADERS,
            "Cache-Control": "public, max-age=3600",
        },
    )


def mcp_unauthorized_response(*, token_present: bool, trailing_slash: bool = False) -> JSONResponse:
    """Build the contract 401 without reflecting any bearer material."""
    challenge = (
        f'Bearer realm="{MCP_REALM}", '
        f'resource_metadata="{protected_resource_metadata_url(trailing_slash)}"'
    )
    if token_present:
        challenge += ', error="invalid_token"'
    return JSONResponse(
        {
            "error": "unauthorized",
            "detail": "Invalid bearer token" if token_present else "Missing bearer token",
        },
        status_code=401,
        headers={"WWW-Authenticate": challenge},
    )


def _normalise_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _mcp_path(path: str) -> bool:
    return path == MCP_PATH or path.startswith(f"{MCP_PATH}/")


class MCPPathCORSMiddleware:
    """Apply browser CORS only to the mounted MCP path.

    The middleware handles OPTIONS before the bearer gate and rejects an
    untrusted browser origin before any authentication work. Non-browser calls
    (without ``Origin``) pass through unchanged.
    """

    def __init__(self, app, allowed_origins: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed_origins = tuple(allowed_origins)

    def _allowed(self, origin: str) -> bool:
        settings = get_settings()
        configured = (
            settings.public_base_url,
            *settings.mcp_allowed_origins,
            *self.allowed_origins,
        )
        return _normalise_origin(origin) in {
            _normalise_origin(value) for value in configured if value
        }

    @staticmethod
    def _response_headers(origin: str | None) -> dict[str, str]:
        headers = {
            "Access-Control-Allow-Methods": MCP_CORS_METHODS,
            "Access-Control-Allow-Headers": MCP_CORS_HEADERS,
            "Access-Control-Expose-Headers": MCP_CORS_EXPOSE_HEADERS,
            "Access-Control-Max-Age": "3600",
        }
        if origin:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _mcp_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if origin and not self._allowed(origin):
            await JSONResponse(
                {"error": "forbidden", "detail": "Origin not allowed"},
                status_code=403,
            )(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS":
            await Response(
                status_code=204,
                headers=self._response_headers(origin),
            )(scope, receive, send)
            return

        if not origin:
            await self.app(scope, receive, send)
            return

        async def send_with_cors(message) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                response_headers = list(message.get("headers", []))
                existing = {name.lower() for name, _value in response_headers}
                for name, value in self._response_headers(origin).items():
                    encoded_name = name.lower().encode("latin-1")
                    if encoded_name not in existing:
                        response_headers.append(
                            (encoded_name, value.encode("latin-1"))
                        )
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_with_cors)


@router.api_route(
    "/.well-known/oauth-protected-resource",
    methods=["GET", "OPTIONS"],
    include_in_schema=False,
)
async def protected_resource_metadata_root() -> JSONResponse:
    return _metadata_response()


@router.api_route(
    "/.well-known/oauth-protected-resource/mcp",
    methods=["GET", "OPTIONS"],
    include_in_schema=False,
)
async def protected_resource_metadata_mcp() -> JSONResponse:
    return _metadata_response()


@router.api_route(
    "/.well-known/oauth-protected-resource/",
    methods=["GET", "OPTIONS"],
    include_in_schema=False,
)
async def protected_resource_metadata_root_slash() -> RedirectResponse:
    return RedirectResponse(
        url="/.well-known/oauth-protected-resource",
        status_code=308,
    )


@router.api_route(
    "/.well-known/oauth-protected-resource/mcp/",
    methods=["GET", "OPTIONS"],
    include_in_schema=False,
)
async def protected_resource_metadata_mcp_slash() -> JSONResponse:
    # The slash form names the slash URL: clients compare `resource` with the URL they connect to.
    return _metadata_response(trailing_slash=True)


__all__ = (
    "MCPPathCORSMiddleware",
    "MCP_REALM",
    "MCP_PATH",
    "canonical_mcp_url",
    "mcp_unauthorized_response",
    "protected_resource_metadata_url",
    "router",
)
