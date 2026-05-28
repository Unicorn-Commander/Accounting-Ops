"""Workspace + entity + role resolution.

The chain on every authenticated request:
  1. validate_bearer_token  -> AuthContext (uc_uid, agent_id, scopes, claims)
  2. resolve_workspace      -> WorkspaceContext (workspace_id, role)
  3. resolve_entity         -> EntityContext (workspace + entity_id)

Workspace + entity ids come from the URL path (preferred), HTTP headers
(fallback), or JWT claims (fallback). RBAC is enforced via workspace_membership
lookup; entity_scope on the membership row, if set, restricts which entities
the role applies to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: UUID
    workspace_slug: str
    role: str  # workspace_role enum: owner | admin | accountant | viewer
    user_uc_uid: str
    agent_id: str | None
    entity_scope: tuple[UUID, ...] | None  # None = role applies to all entities
    hipaa_protected: bool


@dataclass(frozen=True)
class EntityContext:
    workspace: WorkspaceContext
    entity_id: UUID
    entity_slug: str


# Role hierarchy: owner > admin > accountant > viewer. Higher roles inherit
# permissions of lower roles. Use require_role(ctx, "accountant") for write ops.
_ROLE_RANK = {"viewer": 0, "accountant": 1, "admin": 2, "owner": 3}


def role_at_least(role: str, minimum: str) -> bool:
    """True if `role` has at least `minimum`-level permissions."""
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK.get(minimum, 99)


def require_role(ctx: WorkspaceContext, minimum: str) -> None:
    """Raise 403 if the workspace role doesn't meet the minimum."""
    if not role_at_least(ctx.role, minimum):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"role '{ctx.role}' insufficient (needs '{minimum}')",
        )


async def _ensure_user_account(uc_uid: str) -> UUID:
    """Auto-provision a public.user_account row on first sight of a uc_uid.

    Returns the user_account.id. Idempotent.
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


async def _resolve_membership(
    user_uc_uid: str, workspace_id: UUID
) -> tuple[str, tuple[UUID, ...] | None, bool, str]:
    """Look up the (role, entity_scope, hipaa_protected, workspace_slug) for this user+workspace.

    Returns (role, entity_scope, hipaa_protected, slug) or raises 403/404.
    """
    user_id = await _ensure_user_account(user_uc_uid)
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        SELECT m.role, m.entity_scope, w.hipaa_protected, w.slug
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :user_id
                          AND m.workspace_id = :workspace_id
                          AND w.status = 'active'
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id, "workspace_id": workspace_id},
                )
            ).first()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="No active membership for this workspace",
                )
            role, entity_scope_json, hipaa, slug = row
            entity_scope: tuple[UUID, ...] | None = None
            if entity_scope_json:
                try:
                    entity_scope = tuple(UUID(e) for e in entity_scope_json)
                except Exception:
                    entity_scope = None
            return role, entity_scope, bool(hipaa), slug


def _extract_workspace_id(
    request: Request, header: str | None, claims: dict
) -> UUID:
    raw = request.path_params.get("workspace_id") or header or claims.get("workspace_id")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace is required (path /api/workspaces/{workspace_id}/..., X-Workspace-Id header, or workspace_id claim)",
        )
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="workspace_id must be a UUID",
        ) from exc


def _extract_entity_id(
    request: Request, header: str | None, claims: dict
) -> UUID:
    raw = request.path_params.get("entity_id") or header or claims.get("entity_id")
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="entity is required (path /entities/{entity_id}/..., X-Entity-Id header, or entity_id claim)",
        )
    try:
        return UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="entity_id must be a UUID",
        ) from exc


async def resolve_workspace(
    request: Request,
    auth: AuthContext = Depends(validate_bearer_token),
    workspace_id_header: str | None = Header(default=None, alias="X-Workspace-Id"),
) -> WorkspaceContext:
    """FastAPI dependency: resolve and authorize the user for the workspace.

    For service tokens carrying H2 scope constraints, the membership lookup
    runs first (the service token impersonates a real user), then the scope
    is intersected: workspace_ids gates the workspace; entity_ids narrows
    entity_scope; role_floor caps the effective role. A scope mismatch
    raises 403 — independent of what the impersonated user is allowed.
    """
    workspace_id = _extract_workspace_id(request, workspace_id_header, auth.claims)
    role, entity_scope, hipaa, slug = await _resolve_membership(auth.uc_uid, workspace_id)

    # ---- H2: apply service-token scope, if any ----
    if auth.service_scope is not None:
        ss = auth.service_scope
        # 1. Workspace gate. If the token declared a workspace allow-list and
        #    this workspace isn't in it, 403 regardless of membership.
        if ss.workspace_ids is not None and workspace_id not in ss.workspace_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Service token is not scoped to this workspace.",
            )
        # 2. Entity scope intersection. The service-token entity_ids further
        #    restricts the membership entity_scope (None on either side = all).
        if ss.entity_ids is not None:
            if entity_scope is None:
                # Membership had no constraint → take the token's restriction.
                entity_scope = tuple(ss.entity_ids)
            else:
                intersection = tuple(e for e in entity_scope if e in ss.entity_ids)
                if not intersection:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Service token entity scope has no overlap with membership.",
                    )
                entity_scope = intersection
        # 3. Role cap. min(membership_role, role_floor) in _ROLE_ORDER.
        if ss.role_floor is not None:
            role = _cap_role(role, ss.role_floor)

    return WorkspaceContext(
        workspace_id=workspace_id,
        workspace_slug=slug,
        role=role,
        user_uc_uid=auth.uc_uid,
        agent_id=auth.agent_id,
        entity_scope=entity_scope,
        hipaa_protected=hipaa,
    )


# Role hierarchy mirrored from app.auth._ROLE_ORDER. Lowest-priv first.
_ROLE_ORDER = ("viewer", "accountant", "admin", "owner")


def _cap_role(actual: str, ceiling: str) -> str:
    """Return min(actual, ceiling) along the role hierarchy.

    If either role is unknown, fall back to the safer (lower) one: an unknown
    value should never grant more reach than declared.
    """
    try:
        a_idx = _ROLE_ORDER.index(actual)
    except ValueError:
        return ceiling  # unknown actual → use ceiling (safer)
    try:
        c_idx = _ROLE_ORDER.index(ceiling)
    except ValueError:
        return actual  # unknown ceiling → fall back to actual
    return actual if a_idx <= c_idx else ceiling


async def resolve_entity(
    request: Request,
    workspace: WorkspaceContext = Depends(resolve_workspace),
    entity_id_header: str | None = Header(default=None, alias="X-Entity-Id"),
) -> EntityContext:
    """FastAPI dependency: resolve workspace + entity, enforce entity_scope if set."""
    entity_id = _extract_entity_id(request, entity_id_header, {})
    if workspace.entity_scope is not None and entity_id not in workspace.entity_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Entity outside this membership's scope",
        )
    # We can't fetch entity_slug from public schema; need workspace schema. Skip
    # the slug lookup for hot-path performance — tools that need it can query
    # within their workspace_session.
    return EntityContext(workspace=workspace, entity_id=entity_id, entity_slug="")


# ---------------------------------------------------------------------------
# Backwards-compatible legacy types. Phase 1 cleanup will drop these.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantContext:
    """Deprecated. Use WorkspaceContext + EntityContext."""

    tenant_id: UUID
    user_uc_uid: str
    agent_id: str | None


async def resolve_tenant(
    request: Request,
    auth: AuthContext = Depends(validate_bearer_token),
    tenant_id_header: str | None = Header(default=None, alias="X-Tenant-ID"),
) -> TenantContext:
    """Deprecated. Provided so existing code keeps compiling during the refactor."""
    raw_tenant_id = (
        request.path_params.get("tenant_id")
        or tenant_id_header
        or auth.claims.get("tenant_id")
        or auth.claims.get("workspace_id")
    )
    if not raw_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant is required (use /api/workspaces/{workspace_id}/ — tenant is deprecated)",
        )
    try:
        return TenantContext(
            tenant_id=UUID(str(raw_tenant_id)),
            user_uc_uid=auth.uc_uid,
            agent_id=auth.agent_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant must be a UUID"
        ) from exc


__all__: Iterable[str] = (
    "WorkspaceContext",
    "EntityContext",
    "TenantContext",  # deprecated
    "resolve_workspace",
    "resolve_entity",
    "resolve_tenant",  # deprecated
    "role_at_least",
    "require_role",
)
