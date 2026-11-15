# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""Flagged provisioning lifecycle and JIT membership; never changes book data.

Control-plane state is recorded only. There is no shared HTTP/MCP write gate
in the approved fence, so it must not be advertised as an enforced write lock.
"""
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import SessionLocal
from app.security.entitlements import OpsCenterEntitlementsClient
from app.security.member_removal import remove_person
from app.security.provisioning_token import require_provisioning_token

router = APIRouter(
    prefix="/api/v1/provisioning/workspaces", tags=["provisioning"],
    dependencies=[Depends(require_provisioning_token)],
)


class Owner(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    # Email is ignored (including absent/empty), never persisted or logged.


class WorkspaceCreate(BaseModel):
    org_id: str = Field(min_length=1, max_length=255)
    slug: str
    display_name: str = Field(min_length=1, max_length=120)
    plan_tier: str
    entitlements: list[str]
    owner: Owner
    timezone: str


class WorkspacePatch(BaseModel):
    state: Literal["active", "suspended", "archived"]


def workspace_status(row) -> dict:
    return {"workspace_id": str(row.id), "status": (
        row.control_plane_state if row.status == "active" else row.status
    )}


async def get_workspace(session, workspace_id: UUID):
    return (await session.execute(text("""
        SELECT id, slug, display_name, uc_org_id, status::text AS status, control_plane_state
        FROM public.workspace WHERE id = :wid
    """), {"wid": workspace_id})).first()


async def ensure_workspace(session, org_id: str, display_name: str, subject: str):
    from app.api.onboarding import _org_workspace_slug, _workspace_id_for_org
    from scripts.provision_workspace import provision_workspace

    wid = _workspace_id_for_org(org_id)
    # Both enabled first-sign-in and POST hold this transaction lock until the
    # shared provisioner has finished its independently committed registry work.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:wid, 219))"), {"wid": str(wid)}
    )
    row = await get_workspace(session, wid)
    if row:
        if row.uc_org_id != org_id:
            # No slug-based takeover; legacy identity requires exact recorded
            # subject administering this exact UUID, never email or username.
            if row.uc_org_id is not None or not (await session.execute(text("""
                SELECT 1 FROM public.workspace_membership m
                JOIN public.user_account u ON u.id = m.user_id
                WHERE m.workspace_id = :wid AND u.uc_uid = :subject
                  AND m.status = 'active' AND m.role IN ('owner', 'admin', 'client_owner')
            """), {"wid": wid, "subject": subject})).first():
                raise HTTPException(409, "Organization identity does not match workspace")
            await session.execute(
                text("UPDATE public.workspace SET uc_org_id = :org WHERE id = :wid"),
                {"org": org_id, "wid": wid},
            )
            row = await get_workspace(session, wid)
        return row, False
    slug = _org_workspace_slug(wid)
    while (await session.execute(
        text("SELECT 1 FROM public.workspace WHERE slug = :slug"), {"slug": slug}
    )).first():
        slug = f"org-{wid.hex}-{uuid4().hex}"
    await provision_workspace(
        engine=None, workspace_id=wid, display_name=display_name, slug=slug,
        uc_org_id=org_id, entities=None,
    )
    return await get_workspace(session, wid), True


@router.post("")
async def create_workspace(body: WorkspaceCreate, idempotency_key: str = Header()):
    if idempotency_key != f"{body.org_id}:accounting-ops":
        raise HTTPException(400, "Invalid Idempotency-Key")
    async with SessionLocal() as session, session.begin():
        row, _ = await ensure_workspace(session, body.org_id, body.display_name, body.owner.subject)
        return workspace_status(row)


@router.get("/{workspace_id}")
async def read_workspace(workspace_id: UUID):
    async with SessionLocal() as session:
        row = await get_workspace(session, workspace_id)
        if row is None:
            raise HTTPException(404, "Workspace not found")
        return workspace_status(row)


@router.patch("/{workspace_id}")
async def patch_workspace(workspace_id: UUID, body: WorkspacePatch):
    async with SessionLocal() as session, session.begin():
        row = (await session.execute(text("""
            UPDATE public.workspace SET control_plane_state = :state
            WHERE id = :wid RETURNING id
        """), {"wid": workspace_id, "state": body.state})).first()
        if row is None:
            raise HTTPException(404, "Workspace not found")
        return {"workspace_id": str(row.id), "status": body.state}


@router.delete("/{workspace_id}/members/{subject}", status_code=204)
async def delete_member(workspace_id: UUID, subject: str):
    receipt = await remove_person(workspace_id, subject=subject)
    # 204 has no body; counts are still available to the caller and in audit.
    return Response(status_code=204, headers={
        "X-Revoked-PAT-Count": str(receipt["revoked_pat_count"]),
        "X-Static-Service-Token-Count": str(receipt["static_service_token_count"]),
    })


async def ensure_jit_membership(auth, bearer, entitlement_client=None):
    from app.api.onboarding import (
        ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL,
        EntitledProvisioningResult,
        _org_workspace_display_name,
    )
    from app.tenancy import _ensure_user_account

    if not bearer or not auth.may_autoprovision or auth.claims.get("iss") == "ao-local":
        return EntitledProvisioningResult("disabled")
    uid = await _ensure_user_account(auth.uc_uid, autoprovision=True)
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT id FROM public.user_account WHERE id = :uid FOR UPDATE"), {"uid": uid}
        )
        # Fetch AFTER locking, with the person's bearer, never a group claim.
        # Removal obtains the same lock: an old in-flight answer cannot re-add.
        client = entitlement_client or OpsCenterEntitlementsClient()
        try:
            answer = await client.fetch(bearer)
        except Exception:
            return EntitledProvisioningResult(
                "unavailable", detail=ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL
            )
        if answer.status == "not_entitled":
            return EntitledProvisioningResult("not_entitled")
        if answer.status != "entitled" or not answer.org_id or not (answer.org_role or "").strip():
            return EntitledProvisioningResult(
                "unavailable", detail=ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL
            )
        row, created = await ensure_workspace(
            session, answer.org_id,
            _org_workspace_display_name(answer.org_id, answer.org_name), auth.uc_uid,
        )
        # Only roles with equivalent meaning are elevated. Other Ops-Center
        # roles, including future values, get Accounting-Ops' least privilege.
        role = {"owner": "owner", "admin": "admin"}.get(answer.org_role.strip().lower(), "viewer")
        await session.execute(text("""
            INSERT INTO public.workspace_membership (id, workspace_id, user_id, role, status)
            VALUES (gen_random_uuid(), :wid, :uid, CAST(:role AS workspace_role), 'active')
            ON CONFLICT ON CONSTRAINT uq_workspace_membership DO UPDATE
            SET role = EXCLUDED.role, status = 'active'
            WHERE workspace_membership.status != 'suspended'
        """), {"wid": row.id, "uid": uid, "role": role})
        return EntitledProvisioningResult(
            "provisioned" if created else "existing", workspace_id=row.id,
            slug=row.slug, display_name=row.display_name,
        )
