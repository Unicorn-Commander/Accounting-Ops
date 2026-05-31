"""HTTP endpoints for Personal Access Token self-service.

Routes (all under ``/api/me/personal-access-tokens`` — same shape as the
existing per-user endpoints in app.api.workspaces):

    GET    /api/me/personal-access-tokens
    POST   /api/me/personal-access-tokens
    POST   /api/me/personal-access-tokens/{id}/revoke
    DELETE /api/me/personal-access-tokens/{id}

CALLER AUTH MATRIX
------------------
All four routes require KEYCLOAK auth (a real user session). PATs themselves
are blocked from minting more PATs — chicken-and-egg defense in depth. A PAT
calling here gets 403 even if the route would otherwise succeed.

The mint-blocking is enforced via ``_require_keycloak`` which rejects any
AuthContext carrying ``pat_id`` in its claims (set by the PAT auth path) or
``scopes == ('service',)`` (set by service tokens). Browsers signed in via
the next-auth → Keycloak loop in web/lib/auth.ts carry neither.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal
from app.security.pat import (
    DEFAULT_TTL_DAYS,
    MAX_ACTIVE_PATS_PER_USER,
    MAX_TTL_DAYS,
    PATRow,
    count_active_pats,
    create_pat,
    delete_pat,
    list_pats,
    resolve_ttl,
    revoke_pat,
    validate_scopes,
)

router = APIRouter(tags=["personal-access-tokens"])

# Canonical scope catalog. Must stay in sync with /.well-known/mcp.json scopes.
_ALL_SCOPES: tuple[str, ...] = (
    "accounting:read",
    "accounting:write",
    "accounting:admin",
)

# Role → scopes the role can grant. Higher roles add to lower-role grants.
# A role implicitly grants every scope listed for itself + every lower role.
# Mirrors the 3-persona expansion in app.tenancy._ROLE_RANK (2026-05-30):
# new roles (client_owner, cpa, bookkeeper, client_user, platform_admin) all
# get explicit entries so the mint UI doesn't silently grant zero scopes when
# the user only holds a newer role.
_ROLE_GRANTS: dict[str, set[str]] = {
    "viewer": {"accounting:read"},
    "client_user": {"accounting:read"},
    "bookkeeper": {"accounting:read"},
    "accountant": {"accounting:read", "accounting:write"},
    "cpa": {"accounting:read", "accounting:write"},
    "admin": {"accounting:read", "accounting:write", "accounting:admin"},
    "owner": {"accounting:read", "accounting:write", "accounting:admin"},
    "client_owner": {"accounting:read", "accounting:write", "accounting:admin"},
    "platform_admin": {
        "accounting:read",
        "accounting:write",
        "accounting:admin",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_keycloak(auth: AuthContext) -> None:
    """Reject service tokens + PATs.

    A PAT cannot mint another PAT (chicken-and-egg). A service token is a
    background-worker bearer — also forbidden from minting human-scoped
    credentials. Only an interactive Keycloak session may use these routes.
    """
    if auth.claims.get("pat_id"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Personal access tokens cannot manage personal access tokens.",
        )
    if auth.scopes == ("service",):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service tokens cannot manage personal access tokens.",
        )


async def _ensure_user_account(uc_uid: str) -> UUID:
    """Mirror of app.tenancy._ensure_user_account so the PAT router can stand
    alone without circular imports through tenancy.
    """
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text("SELECT id FROM public.user_account WHERE uc_uid = :uc"),
                    {"uc": uc_uid},
                )
            ).first()
            if row:
                return row.id
            result = await session.execute(
                text(
                    """
                    INSERT INTO public.user_account (id, uc_uid, is_active, created_at)
                    VALUES (gen_random_uuid(), :uc, true, now())
                    RETURNING id
                    """
                ),
                {"uc": uc_uid},
            )
            return result.scalar_one()


async def _owner_max_scopes(owner_user_id: UUID) -> set[str]:
    """Return the union of scopes the user can grant — derived from the
    highest role they hold across any active workspace membership.

    Justification: a PAT lets ANY of the user's workspaces be accessed (modulo
    the workspace_scope), and entity-level RBAC is still enforced downstream.
    The PAT's scope set just caps WHAT KIND of operation can happen anywhere
    the token reaches. So we take the union of role-grants across memberships
    (the user can write somewhere → admit accounting:write in the catalog).
    """
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT role::text AS role
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :uid AND w.status = 'active'
                        """
                    ),
                    {"uid": str(owner_user_id)},
                )
            ).all()
    out: set[str] = set()
    for row in rows:
        out |= _ROLE_GRANTS.get(row.role, set())
    return out


async def _validate_workspace_scope(
    owner_user_id: UUID, requested: list[str] | None
) -> tuple[UUID, ...] | None:
    """Verify the user is a member of every workspace in ``requested``.

    Returns the tuple of UUIDs on success, ``None`` when ``requested`` is
    ``None`` (= all workspaces). Empty list is rejected (deny-all PATs are
    pointless and create surprise 403s).
    """
    if requested is None:
        return None
    if not requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace_scope must contain at least one workspace UUID "
            "(or be omitted for unrestricted scope).",
        )
    try:
        wanted = [UUID(str(v)) for v in requested]
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"workspace_scope entries must be UUIDs: {exc}",
        ) from exc

    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT m.workspace_id
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :uid
                          AND w.status = 'active'
                          AND m.workspace_id = ANY(:ws)
                        """
                    ),
                    {"uid": str(owner_user_id), "ws": [str(w) for w in wanted]},
                )
            ).all()
    member_of = {
        (r.workspace_id if isinstance(r.workspace_id, UUID) else UUID(str(r.workspace_id)))
        for r in rows
    }
    missing = [w for w in wanted if w not in member_of]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "workspace_scope contains workspaces the caller is not a "
                f"member of: {[str(m) for m in missing]}"
            ),
        )
    return tuple(wanted)


def _public_pat(p: PATRow) -> dict[str, Any]:
    """Public-facing PAT shape (no secret material)."""
    if p.revoked_at is not None:
        state = "revoked"
    elif p.is_expired:
        state = "expired"
    else:
        state = "active"
    return {
        "id": str(p.id),
        "name": p.name,
        "prefix": p.secret_prefix,
        "scopes": list(p.scopes),
        "workspace_scope": (
            [str(w) for w in p.workspace_scope] if p.workspace_scope else None
        ),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "last_used_at": p.last_used_at.isoformat() if p.last_used_at else None,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        "revoked_at": p.revoked_at.isoformat() if p.revoked_at else None,
        "state": state,
    }


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreatePATRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: list[str] | None = Field(
        default=None,
        description=(
            "Subset of the owner's allowed scopes. Omit for the full set the "
            "owner's role permits."
        ),
    )
    workspace_scope: list[str] | None = Field(
        default=None,
        description=(
            "UUIDs of workspaces the PAT can reach. Omit for all workspaces "
            "the owner is a member of."
        ),
    )
    ttl_days: int | None = Field(
        default=DEFAULT_TTL_DAYS,
        ge=0,
        le=MAX_TTL_DAYS,
        description=(
            f"Lifetime in days. 0 = never expires. Default = {DEFAULT_TTL_DAYS}, "
            f"max = {MAX_TTL_DAYS}."
        ),
    )


class CreatePATResponse(BaseModel):
    id: str
    name: str
    secret: str  # raw — shown ONCE
    prefix: str
    scopes: list[str]
    workspace_scope: list[str] | None
    expires_at: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/me/personal-access-tokens")
async def list_my_pats(
    auth: AuthContext = Depends(validate_bearer_token),
) -> list[dict[str, Any]]:
    _require_keycloak(auth)
    owner_user_id = await _ensure_user_account(auth.uc_uid)
    async with SessionLocal() as session:
        async with session.begin():
            rows = await list_pats(session, owner_user_id)
    return [_public_pat(r) for r in rows]


@router.post(
    "/api/me/personal-access-tokens", status_code=status.HTTP_201_CREATED
)
async def mint_pat(
    body: CreatePATRequest,
    auth: AuthContext = Depends(validate_bearer_token),
) -> CreatePATResponse:
    _require_keycloak(auth)
    owner_user_id = await _ensure_user_account(auth.uc_uid)

    # Cap on active tokens (DoS / mess-protection).
    async with SessionLocal() as session:
        async with session.begin():
            active = await count_active_pats(session, owner_user_id)
    if active >= MAX_ACTIVE_PATS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Active personal-access-token limit reached "
                f"({MAX_ACTIVE_PATS_PER_USER}). Revoke an existing token "
                "before minting another."
            ),
        )

    # Scope intersection.
    owner_allowed = await _owner_max_scopes(owner_user_id)
    if not owner_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No workspace memberships — cannot mint a PAT.",
        )
    try:
        scopes = validate_scopes(body.scopes, owner_allowed)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc

    # Workspace scope (None = all).
    workspace_scope = await _validate_workspace_scope(
        owner_user_id, body.workspace_scope
    )

    # TTL.
    try:
        expires_at = resolve_ttl(body.ttl_days)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # Mint.
    async with SessionLocal() as session:
        async with session.begin():
            minted = await create_pat(
                session,
                owner_user_id=owner_user_id,
                name=body.name.strip(),
                scopes=scopes,
                workspace_scope=workspace_scope,
                expires_at=expires_at,
            )
    return CreatePATResponse(
        id=str(minted.id),
        name=minted.name,
        secret=minted.secret,
        prefix=minted.prefix,
        scopes=list(minted.scopes),
        workspace_scope=(
            [str(w) for w in minted.workspace_scope]
            if minted.workspace_scope
            else None
        ),
        expires_at=minted.expires_at.isoformat() if minted.expires_at else None,
        created_at=minted.created_at.isoformat() if minted.created_at else None,
    )


@router.post("/api/me/personal-access-tokens/{pat_id}/revoke")
async def revoke_my_pat(
    pat_id: UUID,
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    _require_keycloak(auth)
    owner_user_id = await _ensure_user_account(auth.uc_uid)
    async with SessionLocal() as session:
        async with session.begin():
            ok = await revoke_pat(
                session, owner_user_id=owner_user_id, pat_id=pat_id
            )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return {"id": str(pat_id), "revoked_at": datetime.utcnow().isoformat() + "Z"}


@router.delete(
    "/api/me/personal-access-tokens/{pat_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_my_pat(
    pat_id: UUID,
    auth: AuthContext = Depends(validate_bearer_token),
) -> None:
    """Hard-delete a PAT. Owner-only (admin-only-of-anyone's-tokens is a
    larger change deferred to a follow-up if customer support needs it).
    Prefer ``/revoke`` for normal lifecycle.
    """
    _require_keycloak(auth)
    owner_user_id = await _ensure_user_account(auth.uc_uid)
    async with SessionLocal() as session:
        async with session.begin():
            ok = await delete_pat(
                session, owner_user_id=owner_user_id, pat_id=pat_id
            )
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@router.get("/api/me/personal-access-tokens/_meta")
async def pat_meta(
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Return the scope/TTL/limit catalog + the caller's allowed scope set.

    Powers the create modal on /settings/pats so the UI doesn't hard-code
    these constants. Cheap O(memberships) query; result is per-request.
    """
    _require_keycloak(auth)
    owner_user_id = await _ensure_user_account(auth.uc_uid)
    owner_allowed = await _owner_max_scopes(owner_user_id)

    # The list of workspaces the user belongs to so the UI can render the
    # workspace_scope multi-select without hitting another endpoint.
    async with SessionLocal() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT
                            w.id::text   AS id,
                            w.slug       AS slug,
                            w.display_name AS display_name,
                            m.role::text AS role
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :uid AND w.status = 'active'
                        ORDER BY w.display_name
                        """
                    ),
                    {"uid": str(owner_user_id)},
                )
            ).mappings().all()

    return {
        "all_scopes": list(_ALL_SCOPES),
        "owner_allowed_scopes": sorted(owner_allowed),
        "ttl_choices_days": [30, 60, 90, 365, 0],  # 0 = never
        "default_ttl_days": DEFAULT_TTL_DAYS,
        "max_ttl_days": MAX_TTL_DAYS,
        "max_active_pats": MAX_ACTIVE_PATS_PER_USER,
        "workspaces": [dict(r) for r in rows],
    }
