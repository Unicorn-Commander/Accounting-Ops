from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
try:
    from jose import JWTError, jwt
except ImportError:  # pragma: no cover - exercised only in undeclared local envs
    JWTError = Exception
    jwt = None

from app.config import get_settings

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    uc_uid: str
    agent_id: str | None
    scopes: tuple[str, ...]
    claims: dict[str, Any]


async def _jwks() -> dict[str, Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{settings.keycloak_realm_url}/protocol/openid-connect/certs")
        response.raise_for_status()
        return response.json()


async def validate_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> AuthContext:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    settings = get_settings()
    if jwt is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="python-jose is required for token validation",
        )
    try:
        header = jwt.get_unverified_header(credentials.credentials)
        key = next(k for k in (await _jwks())["keys"] if k["kid"] == header["kid"])
        claims = jwt.decode(
            credentials.credentials,
            key,
            algorithms=[header["alg"]],
            audience=settings.keycloak_client_id,
            issuer=str(settings.keycloak_realm_url),
            options={"verify_at_hash": False},
        )
    except (JWTError, StopIteration, httpx.HTTPError, KeyError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token") from exc

    uc_uid = claims.get("uc_uid") or claims.get("sub")
    if not uc_uid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token lacks uc_uid/sub")

    act = claims.get("act") or {}
    agent_id = act.get("sub") or act.get("client_id") if isinstance(act, dict) else None
    scope = tuple(str(claims.get("scope", "")).split())
    return AuthContext(uc_uid=uc_uid, agent_id=agent_id, scopes=scope, claims=claims)


async def auth_middleware(request: Request, call_next):
    response = await call_next(request)
    return response
