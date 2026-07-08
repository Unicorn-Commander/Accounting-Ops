"""Test endpoint for the Brigade JWT verifier.

POST /api/test/brigade-jwt-verify
Body: {"token": "<jwt>"}

Returns the resolved claims dict on success (200) or a structured error (400).
Gated to admin-only via the caller's own authenticated bearer — the Brigade
team uses this to confirm the verifier is wired correctly without needing
production data access.

Why a dedicated endpoint and not just "send a Brigade JWT to /api/me":
  - This endpoint accepts the candidate JWT in the request BODY while the
    caller's authentication is the standard Keycloak/PAT bearer in the header.
    That way the Brigade operator can test verification with their OWN session
    without conflating the two tokens.
  - Returns the full resolved claims dict so the operator sees exactly what we
    parsed (no guessing at why a JWT was rejected).
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth import AuthContext, validate_bearer_token
from app.security.brigade_jwt import verify_brigade_jwt

router = APIRouter(prefix="/api/test", tags=["test"])


class _VerifyRequest(BaseModel):
    token: str


def _require_admin_scope(auth: AuthContext) -> None:
    """The test endpoint is admin-only.

    We can't lean on workspace_membership here (this endpoint is workspace-less),
    so we use the realm-level ``admin`` scope — the same gate used by other admin
    paths. Service tokens (whose scopes are ``("service",)``) are also accepted —
    they're internal infrastructure callers.
    """
    if "admin" in auth.scopes or "service" in auth.scopes:
        return
    # Optional last-resort allowlist: a comma-separated list of realm-level
    # uc_uids in ACCTOPS_ADMIN_UC_UIDS that should always be able to run this
    # diagnostic endpoint. Empty by default (no personal identifiers shipped).
    _allowlist = {
        u.strip()
        for u in os.getenv("ACCTOPS_ADMIN_UC_UIDS", "").split(",")
        if u.strip()
    }
    if auth.uc_uid in _allowlist:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="admin scope required for /api/test/brigade-jwt-verify",
    )


@router.post("/brigade-jwt-verify")
async def post_brigade_jwt_verify(
    body: _VerifyRequest,
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Run the Brigade JWT verifier against a candidate token.

    Returns:
        On success (signature good, claims pass all gates):
            {
              "ok": true,
              "claims": {...the parsed JWT payload...},
              "resolved_user": "<uc_uid>"
            }

        On failure (any reason):
            HTTP 400 with {"detail": "invalid Brigade JWT"}

    Admin-only.
    """
    _require_admin_scope(auth)

    claims = await verify_brigade_jwt(body.token)
    if claims is None:
        # Intentionally vague — the operator can look at the server log for
        # the specific rejection reason (the verifier logs it).
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid Brigade JWT (see server log 'acctops.brigade_jwt' for reason)",
        )
    return {
        "ok": True,
        "claims": claims,
        "resolved_user": claims.get("uc_uid") or claims.get("sub"),
    }


__all__ = ("router",)
