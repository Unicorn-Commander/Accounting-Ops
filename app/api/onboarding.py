"""Self-service onboarding for brand-new users (zero workspace memberships).

A user who has just signed in via SSO has a ``public.user_account`` row but no
workspace and no membership, so every workspace-scoped page 403s / shows an
empty state. This router lets such a user provision their FIRST workspace and
become its ``owner`` — the one provisioning path that does NOT require an
existing admin/owner role.

  POST /api/onboarding/provision-first-workspace
      body: {display_name, hipaa_protected?, isolation_tier?}
      ->   {workspace_id, slug, display_name, status, already_existed}

Guard: the caller must have ZERO active memberships. Creating ADDITIONAL
workspaces stays admin-gated (``app/tools/setup_assistant.create_workspace``).
Once the user owns a workspace they are an ``owner`` there, so connecting banks
and everything else works (owner > the ``admin`` floor on credentials).

The frontend ``/welcome`` flow calls this once on first run, sets the active
workspace cookie, then hands off to the existing ``/setup`` Setup Assistant
(entity -> chart of accounts -> connect bank).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal
from app.security.entitlements import (
    EntitlementResult,
    OpsCenterEntitlementsClient,
    bearer_from_header,
    entitled_provisioning_enabled,
)
from app.tenancy import _ensure_user_account

router = APIRouter(tags=["onboarding"])
_log = logging.getLogger("acctops.onboarding")

ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL = (
    "Accounting-Ops could not verify your entitlement with Ops-Center; "
    "no workspace was created. Retry later."
)


@dataclass(frozen=True)
class EntitledProvisioningResult:
    """Outcome shared by the browser and MCP first-call paths."""

    state: str
    workspace_id: uuid.UUID | None = None
    slug: str | None = None
    display_name: str | None = None
    detail: str | None = None


_VALID_TIERS = {"shared", "premium", "enterprise"}


class ProvisionFirstWorkspaceRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    hipaa_protected: bool = False
    isolation_tier: str = "shared"


async def _first_active_workspace(user_id: uuid.UUID) -> dict[str, Any] | None:
    """Return the caller's first active workspace (id, slug) if they have one."""
    async with SessionLocal() as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT w.id::text AS id,
                               w.slug AS slug,
                               w.display_name AS display_name
                        FROM public.workspace_membership m
                        JOIN public.workspace w ON w.id = m.workspace_id
                        WHERE m.user_id = :uid AND w.status = 'active'
                        ORDER BY w.display_name
                        LIMIT 1
                        """
                    ),
                    {"uid": str(user_id)},
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


async def _backfill_user_profile(user_id: uuid.UUID, claims: dict[str, Any]) -> None:
    """Best-effort fill of display_name + email from token claims.

    Never raises — a cosmetic profile backfill must not fail onboarding.
    """
    email = (claims.get("email") or "").strip() or None
    name = (claims.get("name") or claims.get("given_name") or "").strip() or None
    if not email and not name:
        return
    try:
        async with SessionLocal() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        UPDATE public.user_account
                        SET email = COALESCE(NULLIF(email, ''), :email),
                            display_name = COALESCE(NULLIF(display_name, ''), :name)
                        WHERE id = :uid
                        """
                    ),
                    {"uid": str(user_id), "email": email, "name": name},
                )
    except Exception:
        pass


def _workspace_id_for_org(org_id: str) -> uuid.UUID:
    """Use the Ops-Center UUID directly, with a stable UUID5 fallback."""
    try:
        return uuid.UUID(org_id)
    except (ValueError, AttributeError):
        return uuid.uuid5(uuid.NAMESPACE_URL, f"accounting-ops:ops-center-org:{org_id}")


def _org_workspace_slug(workspace_id: uuid.UUID) -> str:
    """Give each entitled org a collision-resistant, deterministic slug."""
    return f"org-{workspace_id.hex[:32]}"


def _org_workspace_display_name(org_id: str, org_name: str | None) -> str:
    """Use the control-plane name, or the specified stable fallback."""
    name = (org_name or "").strip()
    return name[:120] if name else f"Workspace {org_id[:8]}"


async def _upsert_workspace_membership(
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    role: str,
    *,
    preserve_existing_role: bool,
    activate_membership: bool = True,
) -> None:
    """Attach a user to a workspace without allowing a concurrent demotion."""
    if preserve_existing_role:
        role_sql = """
            role = CASE
                WHEN role IN ('owner', 'client_owner', 'admin') THEN role
                WHEN EXCLUDED.role = 'owner' THEN EXCLUDED.role
                ELSE role
            END
        """
    else:
        role_sql = "role = EXCLUDED.role"
    update_sql = (
        f"status = 'active', {role_sql}" if activate_membership else role_sql
    )

    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    f"""
                    INSERT INTO public.workspace_membership
                        (id, user_id, workspace_id, role, created_at)
                    VALUES (
                        gen_random_uuid(), :user_id, :wid,
                        CAST(:role AS workspace_role), now()
                    )
                    ON CONFLICT ON CONSTRAINT uq_workspace_membership DO UPDATE
                    SET {update_sql}
                    """
                ),
                {"user_id": str(user_id), "wid": str(workspace_id), "role": role},
            )


async def _provision_workspace_with_membership(
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    display_name: str,
    role: str,
    slug: str | None = None,
    uc_org_id: str | None = None,
    preserve_existing_role: bool = False,
    activate_membership: bool = True,
    hipaa_protected: bool = False,
    isolation_tier: str = "shared",
) -> tuple[str, str | None]:
    """Run the one workspace provisioning path used by /welcome and entitlements."""
    # The shared provisioner is intentionally the only place that writes the
    # workspace registry and optional tenant skeleton. /welcome currently passes
    # entities=None, so entitled first-call provisioning does the same.
    from scripts.provision_workspace import provision_workspace as _provision

    schema, _ = await _provision(
        engine=None,
        workspace_id=workspace_id,
        display_name=display_name,
        slug=slug,
        hipaa_protected=hipaa_protected,
        isolation_tier=isolation_tier,
        entities=None,
        uc_org_id=uc_org_id,
    )

    await _upsert_workspace_membership(
        user_id,
        workspace_id,
        role,
        preserve_existing_role=preserve_existing_role,
        activate_membership=activate_membership,
    )

    final_slug = slug
    if final_slug is None:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    text("SELECT slug FROM public.workspace WHERE id = :wid"),
                    {"wid": str(workspace_id)},
                )
            ).first()
        final_slug = row[0] if row else None
    return schema, final_slug


def _entitled_provisioning_is_eligible(
    auth: AuthContext, bearer: str | None
) -> bool:
    """Only a normal remotely authenticated user may use the uchub gate."""
    if not entitled_provisioning_enabled() or not bearer or not auth.may_autoprovision:
        return False
    # A local self-host bearer has no Ops-Center organization to query. The flag
    # must not turn that independent auth mode into a billing dependency.
    return auth.claims.get("iss") != "ao-local"


async def ensure_entitled_workspace(
    auth: AuthContext,
    bearer: str | None,
    *,
    entitlement_client: OpsCenterEntitlementsClient | None = None,
) -> EntitledProvisioningResult:
    """Provision an entitled caller's org workspace once, if they have none.

    The stable workspace id plus the provisioner's idempotent registry insert and
    membership upsert make concurrent first calls converge on one workspace.
    """
    if not _entitled_provisioning_is_eligible(auth, bearer):
        return EntitledProvisioningResult("disabled")

    user_id = await _ensure_user_account(auth.uc_uid, autoprovision=auth.may_autoprovision)
    existing = await _first_active_workspace(user_id)
    if existing is not None:
        return EntitledProvisioningResult(
            "existing",
            workspace_id=uuid.UUID(str(existing["id"])),
            slug=existing.get("slug"),
            display_name=existing.get("display_name"),
        )

    client = entitlement_client or OpsCenterEntitlementsClient()
    try:
        entitlement: EntitlementResult = await client.fetch(bearer)
    except Exception as exc:  # noqa: BLE001 - provisioning is fail closed
        _log.warning(
            "entitled workspace entitlement lookup failed (%s)",
            exc.__class__.__name__,
        )
        return EntitledProvisioningResult(
            "unavailable", detail=ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL
        )

    if entitlement.status == "not_entitled":
        return EntitledProvisioningResult("not_entitled")
    if entitlement.status != "entitled" or not entitlement.org_id:
        return EntitledProvisioningResult(
            "unavailable", detail=ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL
        )

    await _backfill_user_profile(user_id, auth.claims)
    workspace_id = _workspace_id_for_org(entitlement.org_id)
    display_name = _org_workspace_display_name(entitlement.org_id, entitlement.org_name)
    role = (
        "owner"
        if (entitlement.org_role or "").strip().lower() == "owner"
        else "accountant"
    )
    await _provision_workspace_with_membership(
        user_id=user_id,
        workspace_id=workspace_id,
        display_name=display_name,
        slug=_org_workspace_slug(workspace_id),
        role=role,
        uc_org_id=entitlement.org_id,
        preserve_existing_role=True,
        activate_membership=True,
    )
    return EntitledProvisioningResult(
        "provisioned",
        workspace_id=workspace_id,
        slug=_org_workspace_slug(workspace_id),
        display_name=display_name,
    )


@router.post("/api/onboarding/provision-first-workspace")
async def provision_first_workspace(
    body: ProvisionFirstWorkspaceRequest,
    request: Request,
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Provision the caller's FIRST workspace and make them its owner.

    Safe under double-submit: if the caller already has an active workspace this
    returns that workspace (``already_existed=true``) instead of creating a
    second one — so a stale client or a retry lands them in their existing
    workspace, never a duplicate, and never a 403 dead-end.

    NOTE: provisioning runs ``CREATE SCHEMA`` + an alembic upgrade under the
    admin DB role (~5-15s, off the event loop via ``asyncio.to_thread``). For
    the first-workspace, low-concurrency case this is fine to await inline; the
    client shows a "setting up your workspace" state.
    """
    if body.isolation_tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"isolation_tier must be one of {sorted(_VALID_TIERS)}",
        )

    entitled = await ensure_entitled_workspace(
        auth,
        bearer_from_header(request.headers.get("authorization")),
    )
    if entitled.state == "unavailable":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=entitled.detail or ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL,
        )
    if entitled.state == "provisioned":
        return {
            "workspace_id": str(entitled.workspace_id),
            "slug": entitled.slug,
            "display_name": entitled.display_name,
            "status": "active",
            "already_existed": False,
        }

    user_id = await _ensure_user_account(
        auth.uc_uid, autoprovision=auth.may_autoprovision
    )

    # This path is ONLY for a user's first workspace. If they already have one,
    # hand it back rather than minting a duplicate (idempotent under retry).
    existing = await _first_active_workspace(user_id)
    if existing is not None:
        return {
            "workspace_id": existing["id"],
            "slug": existing["slug"],
            "display_name": body.display_name.strip(),
            "status": "active",
            "already_existed": True,
        }

    await _backfill_user_profile(user_id, auth.claims)

    workspace_id = uuid.uuid4()
    schema, slug = await _provision_workspace_with_membership(
        workspace_id=workspace_id,
        user_id=user_id,
        display_name=body.display_name.strip(),
        role="owner",
        activate_membership=False,
        hipaa_protected=body.hipaa_protected,
        isolation_tier=body.isolation_tier,
    )

    return {
        "workspace_id": str(workspace_id),
        "slug": slug,
        "display_name": body.display_name.strip(),
        "schema": schema,
        "status": "active",
        "already_existed": False,
    }
