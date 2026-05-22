"""Keycloak OAuth 2.1 + OIDC bearer token validation with JWKS caching.

The `act` claim (RFC 8693) carries the acting agent identity when an agent
calls on behalf of a delegating user. uc_uid is the canonical user identifier
across the Unicorn ecosystem (preferred over `preferred_username` per
the OIDC-user-id canary lesson).

Service tokens: if the presented bearer token matches a registered service
token from ACCTOPS_SERVICE_TOKENS env (JSON map of token→{uc_uid, agent_id}),
we synthesize an AuthContext for the impersonated user. This is the path
used by long-running internal workers (receipt-reconciler, etc.) that
operate on behalf of a workspace owner without a Keycloak session.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any

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


@dataclass(frozen=True)
class AuthContext:
    uc_uid: str
    agent_id: str | None
    scopes: tuple[str, ...]
    claims: dict[str, Any]


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


_service_tokens_cache: dict[str, dict[str, str]] | None = None


def _load_service_tokens() -> dict[str, dict[str, str]]:
    """Parse ACCTOPS_SERVICE_TOKENS env var.

    Format: {"<random-token>": {"uc_uid": "aaron", "agent_id": "receipt-reconciler"}}
    The token strings must be at least 32 chars (entropy) and are compared
    with secrets.compare_digest. Returns {} if unset or malformed.
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
        tokens: dict[str, dict[str, str]] = {}
        for token, ident in parsed.items():
            if not isinstance(ident, dict):
                continue
            uc_uid = ident.get("uc_uid")
            if not uc_uid or len(token) < 32:
                continue
            tokens[token] = {
                "uc_uid": uc_uid,
                "agent_id": ident.get("agent_id") or "service",
            }
        _service_tokens_cache = tokens
        return tokens
    except (ValueError, TypeError):
        _service_tokens_cache = {}
        return _service_tokens_cache


def _match_service_token(presented: str) -> dict[str, str] | None:
    """Constant-time compare against registered service tokens. None on miss."""
    for token, ident in _load_service_tokens().items():
        if secrets.compare_digest(token, presented):
            return ident
    return None


_log = logging.getLogger("acctops.auth")


async def validate_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )

    # Service token fast-path: long-lived shared-secret tokens for internal
    # workers. Bypass JWT validation; impersonate the configured uc_uid.
    service_ident = _match_service_token(credentials.credentials)
    if service_ident is not None:
        return AuthContext(
            uc_uid=service_ident["uc_uid"],
            agent_id=service_ident["agent_id"],
            scopes=("service",),
            claims={"sub": service_ident["uc_uid"], "uc_uid": service_ident["uc_uid"]},
        )

    if jwt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="python-jose is required for token validation",
        )
    settings = get_settings()
    try:
        header = jwt.get_unverified_header(credentials.credentials)
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
        # Keycloak default: aud="account" for browser tokens unless an
        # Audience mapper is configured on the client. The canonical
        # "this token was issued FOR client X" claim is azp (authorized
        # party). We validate signature + issuer + expiry strictly, then
        # check azp matches our client_id instead of strict audience.
        claims = jwt.decode(
            credentials.credentials,
            key,
            algorithms=[header["alg"]],
            issuer=str(settings.keycloak_realm_url),
            options={"verify_at_hash": False, "verify_aud": False},
        )
        azp = claims.get("azp")
        aud = claims.get("aud")
        # Accept if azp matches our client_id OR aud (list or string)
        # contains it (covers tokens issued with explicit audience mapper).
        if azp != settings.keycloak_client_id:
            aud_list = aud if isinstance(aud, list) else [aud] if aud else []
            if settings.keycloak_client_id not in aud_list:
                _log.warning(
                    "[acctops.auth] token rejected: azp=%r, aud=%r doesn't include %r",
                    azp, aud, settings.keycloak_client_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token not authorized for this client",
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
    return AuthContext(uc_uid=uc_uid, agent_id=agent_id, scopes=scope, claims=claims)
