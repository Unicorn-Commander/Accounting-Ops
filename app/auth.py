"""Keycloak OAuth 2.1 + OIDC bearer token validation with JWKS caching.

The `act` claim (RFC 8693) carries the acting agent identity when an agent
calls on behalf of a delegating user. uc_uid is the canonical user identifier
across the Unicorn ecosystem (preferred over `preferred_username` per
the OIDC-user-id canary lesson).

Service tokens: if the presented bearer token matches a registered service
token from ACCTOPS_SERVICE_TOKENS env (JSON map of token→{uc_uid, agent_id,
…}), we synthesize an AuthContext for the impersonated user. This is the
path used by long-running internal workers (receipt-reconciler, etc.) that
operate on behalf of a workspace owner without a Keycloak session.

H2 (2026-05-28): service tokens may carry optional scope fields that
constrain the token's reach below the impersonated user's full membership:

  - ``workspace_ids``: list of workspace UUIDs the token may access. If set
    and the request's workspace_id is not in the list, resolve_workspace
    raises 403 (independent of the user's membership).
  - ``entity_ids``: list of entity UUIDs the token may access. Intersected
    with the user's membership entity_scope; an empty intersection raises
    403. None = inherit the user's full membership.
  - ``role_floor``: caps the token's effective role at this value (e.g.
    ``viewer``). The token gets ``min(membership_role, role_floor)``.

A service token WITHOUT these fields keeps full impersonation reach for
backward compatibility — useful for trusted internal workers during the
transition. New tokens SHOULD declare the minimum scope they need.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

try:
    from jose import JWTError, jwt
except ImportError:  # pragma: no cover - exercised only without python-jose installed
    JWTError = Exception  # type: ignore[assignment]
    jwt = None  # type: ignore[assignment]

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


# Role hierarchy for the role_floor cap. Lower index = lower privilege.
# Must stay in sync with the ROLE_ORDER used in app.tenancy.require_role.
# Multi-persona expansion (2026-05-30): see app/tenancy.py for the rationale
# behind the ordering. Existing role_floor values keep working unchanged.
_ROLE_ORDER = (
    "viewer",
    "client_user",
    "bookkeeper",
    "accountant",
    "cpa",
    "admin",
    "owner",
    "client_owner",
    "platform_admin",
)


@dataclass(frozen=True)
class ServiceScope:
    """Per-service-token scope constraints applied at resolve_workspace.

    Each field is "additive restriction" — if None, the token inherits
    whatever the impersonated user has in the membership row. If set, it
    further restricts the effective reach.
    """

    workspace_ids: frozenset[UUID] | None = None
    entity_ids: frozenset[UUID] | None = None
    role_floor: str | None = None


@dataclass(frozen=True)
class AuthContext:
    uc_uid: str
    agent_id: str | None
    scopes: tuple[str, ...]
    claims: dict[str, Any]
    # H2: service-token scope constraints. None for Keycloak-validated user
    # tokens (no constraint — the user's full membership applies). Set when
    # the bearer matched a service token that declared scope fields.
    service_scope: ServiceScope | None = None


# JWKS cache: avoid refetching Keycloak's signing keys on every request.
# TTL chosen so a rotated key propagates within 5 minutes of next request.
_JWKS_CACHE_TTL_SEC = 300
_jwks_cache: dict[str, Any] = {"keys": None, "fetched_at": 0.0}


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch the JWKS from Keycloak. Cached for _JWKS_CACHE_TTL_SEC seconds.

    Cloudflare's default WAF blocks the default httpx/Python User-Agent
    with error 1010 ("banned based on browser signature"). We set an
    explicit UA so server-to-server traffic passes through.
    """
    now = time.time()
    if (
        _jwks_cache["keys"] is not None
        and (now - _jwks_cache["fetched_at"]) < _JWKS_CACHE_TTL_SEC
    ):
        return _jwks_cache["keys"]
    settings = get_settings()
    async with httpx.AsyncClient(
        timeout=5,
        headers={"User-Agent": "accounting-ops/0.1 (+https://accounting-ops.magicunicorn.dev)"},
    ) as client:
        response = await client.get(
            f"{settings.keycloak_realm_url}/protocol/openid-connect/certs"
        )
        response.raise_for_status()
        keys = response.json()
    _jwks_cache["keys"] = keys
    _jwks_cache["fetched_at"] = now
    return keys


async def _fetch_jwks_force_refresh() -> dict[str, Any]:
    """Bypass the cache. Used when an unknown kid is seen (rotation)."""
    _jwks_cache["keys"] = None
    return await _fetch_jwks()


_service_tokens_cache: dict[str, dict[str, Any]] | None = None


def _parse_uuid_list(value: Any) -> frozenset[UUID] | None:
    """Parse a JSON list-of-strings into a frozenset of UUIDs.

    Returns None on missing/None (= unbounded). Returns an empty frozenset on
    an explicit empty list (= deny-all, intentional). Skips invalid UUIDs.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    out: set[UUID] = set()
    for v in value:
        try:
            out.add(UUID(str(v)))
        except (ValueError, TypeError):
            # Skip malformed entries — better to drop than to widen scope.
            continue
    return frozenset(out)


def _parse_role_floor(value: Any) -> str | None:
    """Validate a role_floor string against the known hierarchy. Drop unknowns."""
    if value is None or not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in _ROLE_ORDER:
        return value
    return None


def _load_service_tokens() -> dict[str, dict[str, Any]]:
    """Parse ACCTOPS_SERVICE_TOKENS env var.

    Format::

        {
          "<token>": {
            "uc_uid": "aaron",
            "agent_id": "receipt-reconciler",
            # H2 optional scope fields (least-privilege; backward compatible):
            "workspace_ids": ["<wid>", "<wid>"],   // None = all workspaces the user is in
            "entity_ids":    ["<eid>"],            // None = all entities in membership
            "role_floor":    "accountant"          // None = inherit membership role
          }
        }

    Token strings must be at least 32 chars (entropy) and are compared with
    ``secrets.compare_digest``. Returns {} if unset or malformed.
    """
    global _service_tokens_cache
    if _service_tokens_cache is not None:
        return _service_tokens_cache
    raw = os.getenv("ACCTOPS_SERVICE_TOKENS", "").strip()
    if not raw:
        _service_tokens_cache = {}
        return _service_tokens_cache
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("ACCTOPS_SERVICE_TOKENS must be a JSON object")
        tokens: dict[str, dict[str, Any]] = {}
        for token, ident in parsed.items():
            if not isinstance(ident, dict):
                continue
            uc_uid = ident.get("uc_uid")
            if not uc_uid or len(token) < 32:
                continue
            tokens[token] = {
                "uc_uid": uc_uid,
                "agent_id": ident.get("agent_id") or "service",
                "workspace_ids": _parse_uuid_list(ident.get("workspace_ids")),
                "entity_ids": _parse_uuid_list(ident.get("entity_ids")),
                "role_floor": _parse_role_floor(ident.get("role_floor")),
            }
        _service_tokens_cache = tokens
        return tokens
    except (ValueError, TypeError):
        _service_tokens_cache = {}
        return _service_tokens_cache


def _match_service_token(presented: str) -> dict[str, Any] | None:
    """Constant-time compare against registered service tokens. None on miss."""
    for token, ident in _load_service_tokens().items():
        if secrets.compare_digest(token, presented):
            return ident
    return None


_log = logging.getLogger("acctops.auth")


async def authenticate_token(token: str) -> AuthContext:
    """Validate a raw bearer token string and return its AuthContext.

    This is the transport-agnostic core of bearer authentication. The FastAPI
    dependency ``validate_bearer_token`` is a thin wrapper around it; the MCP
    sub-app (mounted under /mcp, which bypasses FastAPI dependencies) calls this
    directly from its ASGI auth middleware. Both paths therefore validate
    identically — there is exactly one token-validation implementation.

    Raises HTTPException(401) on any failure (so the FastAPI wrapper propagates
    it unchanged; the MCP middleware translates the status into a JSON 401).
    """
    # Service token fast-path: long-lived shared-secret tokens for internal
    # workers. Bypass JWT validation; impersonate the configured uc_uid.
    # H2: any declared scope constraints travel with the AuthContext and are
    # applied by resolve_workspace below.
    service_ident = _match_service_token(token)
    if service_ident is not None:
        wsids = service_ident.get("workspace_ids")
        eids = service_ident.get("entity_ids")
        floor = service_ident.get("role_floor")
        scope: ServiceScope | None = None
        if wsids is not None or eids is not None or floor is not None:
            scope = ServiceScope(
                workspace_ids=wsids, entity_ids=eids, role_floor=floor
            )
        ctx = AuthContext(
            uc_uid=service_ident["uc_uid"],
            agent_id=service_ident["agent_id"],
            scopes=("service",),
            claims={"sub": service_ident["uc_uid"], "uc_uid": service_ident["uc_uid"]},
            service_scope=scope,
        )
        _stamp_audit_actor(ctx)
        return ctx

    if jwt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="python-jose is required for token validation",
        )
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(token)
        kid = header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing kid"
            )
        jwks = await _fetch_jwks()
        key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
        if key is None:
            # Key rotation: force refresh once before giving up.
            jwks = await _fetch_jwks_force_refresh()
            key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
            if key is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unknown signing key",
                )
        # Strictly validate signature + issuer + expiry here. We do our OWN
        # audience check below against the configurable accepted_audience, so
        # `verify_aud` stays off in jwt.decode: Keycloak routinely stamps
        # aud="account" on tokens, and jose's built-in check has no notion of
        # "also accept via resource_access". (Signature/issuer/exp are NOT
        # weakened — they are enforced exactly as before.)
        claims = jwt.decode(
            token,
            key,
            algorithms=[header["alg"]],
            issuer=str(settings.keycloak_realm_url),
            options={"verify_at_hash": False, "verify_aud": False},
        )
        # OAuth resource-server audience validation (finding H3). A valid uchub
        # token is authorized for THIS resource server iff its configured
        # audience (settings.accepted_audience, default = our client id) is
        # present in the `aud` claim (string OR list) OR appears as a client key
        # under `resource_access`. Agent/Brigade clients carry it via a Keycloak
        # Audience mapper, so a token minted by a different client is accepted
        # when — and only when — it is explicitly scoped to this resource. We no
        # longer key off azp: that would reject valid cross-client tokens (H3).
        expected_aud = settings.accepted_audience
        aud = claims.get("aud")
        aud_list = aud if isinstance(aud, list) else [aud] if aud else []
        resource_access = claims.get("resource_access")
        in_resource_access = (
            isinstance(resource_access, dict) and expected_aud in resource_access
        )
        if expected_aud not in aud_list and not in_resource_access:
            _log.warning(
                "[acctops.auth] token rejected: aud=%r / resource_access keys=%r "
                "do not include required audience %r",
                aud,
                list(resource_access) if isinstance(resource_access, dict) else None,
                expected_aud,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token audience does not include this resource server",
            )
    except HTTPException:
        raise
    except (JWTError, httpx.HTTPError, KeyError) as exc:
        _log.info("JWT validation failed: %s: %s", exc.__class__.__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token"
        ) from exc

    uc_uid = claims.get("uc_uid") or claims.get("sub")
    if not uc_uid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token lacks uc_uid/sub"
        )

    # RFC 8693 `act` claim: acting agent on behalf of delegating user.
    act = claims.get("act") or {}
    agent_id: str | None = None
    if isinstance(act, dict):
        agent_id = act.get("sub") or act.get("client_id")
    scope = tuple(str(claims.get("scope", "")).split())
    ctx = AuthContext(uc_uid=uc_uid, agent_id=agent_id, scopes=scope, claims=claims)
    _stamp_audit_actor(ctx)
    return ctx


async def validate_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    """FastAPI dependency: extract the bearer credential and validate it.

    Thin wrapper over ``authenticate_token`` so HTTP routes and the mounted MCP
    sub-app share one validation path.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    return await authenticate_token(credentials.credentials)


def _stamp_audit_actor(ctx: AuthContext) -> None:
    """Record the VALIDATED caller on the current audit span (finding M2).

    Imported lazily to avoid any import-time coupling to the OTel SDK (which is
    optional). Best-effort: never let telemetry break request auth.
    """
    try:
        from app.otel import set_actor_attributes

        set_actor_attributes(ctx.uc_uid, ctx.agent_id)
    except Exception:  # pragma: no cover - telemetry must never break auth
        pass
