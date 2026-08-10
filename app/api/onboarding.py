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

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal
from app.tenancy import _ensure_user_account

router = APIRouter(tags=["onboarding"])

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
                        SELECT w.id::text AS id, w.slug AS slug
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


@router.post("/api/onboarding/provision-first-workspace")
async def provision_first_workspace(
    body: ProvisionFirstWorkspaceRequest,
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

    # Same machinery the admin create_workspace tool uses, minus the admin gate
    # (intentional — the first workspace is self-serve). engine=None -> the admin
    # engine (ADMIN_DATABASE_URL); the runtime role cannot CREATE SCHEMA.
    from scripts.provision_workspace import provision_workspace as _provision

    workspace_id = uuid.uuid4()
    schema, _ = await _provision(
        engine=None,
        workspace_id=workspace_id,
        display_name=body.display_name.strip(),
        slug=None,
        hipaa_protected=body.hipaa_protected,
        isolation_tier=body.isolation_tier,
        entities=None,
    )

    # Make the caller the owner of the new workspace.
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO public.workspace_membership
                        (id, user_id, workspace_id, role, created_at)
                    VALUES (gen_random_uuid(), :user_id, :wid, 'owner', now())
                    ON CONFLICT ON CONSTRAINT uq_workspace_membership DO UPDATE
                    SET role = 'owner'
                    """
                ),
                {"user_id": str(user_id), "wid": str(workspace_id)},
            )

    # Read back the slug provision derived from the display_name.
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT slug FROM public.workspace WHERE id = :wid"),
                {"wid": str(workspace_id)},
            )
        ).first()
    slug = row[0] if row else None

    return {
        "workspace_id": str(workspace_id),
        "slug": slug,
        "display_name": body.display_name.strip(),
        "schema": schema,
        "status": "active",
        "already_existed": False,
    }
