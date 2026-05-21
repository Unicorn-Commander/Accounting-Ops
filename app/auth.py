"""Keycloak OAuth 2.1 + OIDC bearer token validation with JWKS caching.

The `act` claim (RFC 8693) carries the acting agent identity when an agent
calls on behalf of a delegating user. uc_uid is the canonical user identifier
across the Unicorn ecosystem (preferred over `preferred_username` per
the OIDC-user-id canary lesson).
"""
from __future__ import annotations

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
    """Fetch the JWKS from Keycloak. Cached for _JWKS_CACHE_TTL_SEC seconds."""
    now = time.time()
    if (
        _jwks_cache["keys"] is not None
        and (now - _jwks_cache["fetched_at"]) < _JWKS_CACHE_TTL_SEC
    ):
        return _jwks_cache["keys"]
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5) as client:
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


async def validate_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
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
        claims = jwt.decode(
            credentials.credentials,
            key,
            algorithms=[header["alg"]],
            audience=settings.keycloak_client_id,
            issuer=str(settings.keycloak_realm_url),
            options={"verify_at_hash": False},
        )
    except HTTPException:
        raise
    except (JWTError, httpx.HTTPError, KeyError) as exc:
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
