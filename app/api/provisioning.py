# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""Flagged provisioning lifecycle and JIT membership; never changes book data.

Control-plane state is recorded only. There is no shared HTTP/MCP write gate
in the approved fence, so it must not be advertised as an enforced write lock.
"""

import os
import time
from dataclasses import dataclass
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
    prefix="/api/v1/provisioning/workspaces",
    tags=["provisioning"],
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
    return {
        "workspace_id": str(row.id),
        "status": (row.control_plane_state if row.status == "active" else row.status),
    }


async def get_workspace(session, workspace_id: UUID):
    return (
        await session.execute(
            text("""
        SELECT id, slug, display_name, uc_org_id, status::text AS status, control_plane_state
        FROM public.workspace WHERE id = :wid
    """),
            {"wid": workspace_id},
        )
    ).first()


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
            if (
                row.uc_org_id is not None
                or not (
                    await session.execute(
                        text("""
                SELECT 1 FROM public.workspace_membership m
                JOIN public.user_account u ON u.id = m.user_id
                WHERE m.workspace_id = :wid AND u.uc_uid = :subject
                  AND m.status = 'active' AND m.role IN ('owner', 'admin', 'client_owner')
            """),
                        {"wid": wid, "subject": subject},
                    )
                ).first()
            ):
                raise HTTPException(409, "Organization identity does not match workspace")
            await session.execute(
                text("UPDATE public.workspace SET uc_org_id = :org WHERE id = :wid"),
                {"org": org_id, "wid": wid},
            )
            row = await get_workspace(session, wid)
        return row, False
    slug = _org_workspace_slug(wid)
    while (
        await session.execute(
            text("SELECT 1 FROM public.workspace WHERE slug = :slug"), {"slug": slug}
        )
    ).first():
        slug = f"org-{wid.hex}-{uuid4().hex}"
    await provision_workspace(
        engine=None,
        workspace_id=wid,
        display_name=display_name,
        slug=slug,
        uc_org_id=org_id,
        entities=None,
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
        row = (
            await session.execute(
                text("""
            UPDATE public.workspace SET control_plane_state = :state
            WHERE id = :wid RETURNING id
        """),
                {"wid": workspace_id, "state": body.state},
            )
        ).first()
        if row is None:
            raise HTTPException(404, "Workspace not found")
        return {"workspace_id": str(row.id), "status": body.state}


@router.delete("/{workspace_id}/members/{subject}", status_code=204)
async def delete_member(workspace_id: UUID, subject: str):
    receipt = await remove_person(workspace_id, subject=subject)
    # 204 has no body; counts are still available to the caller and in audit.
    return Response(
        status_code=204,
        headers={
            "X-Revoked-PAT-Count": str(receipt["revoked_pat_count"]),
            "X-Static-Service-Token-Count": str(receipt["static_service_token_count"]),
        },
    )


# --- membership at sign-in (contract rule 13) --------------------------------
#
# Every MCP request and every load of the workspace list passes through
# ensure_jit_membership. Asking Ops-Center on each of them would put its latency,
# and its outages, in front of the books, so an answer is remembered per person
# for a short time, in this process only. What is remembered never CHANGES a
# membership: any change is made from an answer fetched under the person's row
# lock, which a removal also takes, so a remembered answer cannot bring a removed
# person back and an answer already in flight cannot undo a later removal.

ANSWER_FRESH_SECONDS = 60.0
"""How long an entitlements answer is acted on before Ops-Center is asked again."""

RETRY_AFTER_FAILURE_SECONDS = 10.0
"""How long a lookup that failed is not repeated for the same person."""

_ANSWER_LIMIT = 10_000
_BOOKS_ROLES = frozenset({"viewer", "client_user", "bookkeeper", "accountant", "cpa"})
_clock = time.monotonic


@dataclass(frozen=True)
class _Answer:
    kind: Literal["entitled", "not_entitled", "unavailable"]
    until: float
    org_id: str = ""
    org_role: str = ""


_answers: dict[str, _Answer] = {}


def forget_answers() -> None:
    """Drop every remembered answer (tests, and nothing else needs it)."""
    _answers.clear()


def _remember(subject: str, kind: str, seconds: float, org_id: str = "", org_role: str = "") -> None:
    now = _clock()
    if len(_answers) >= _ANSWER_LIMIT:
        for key in [key for key, answer in _answers.items() if answer.until <= now]:
            del _answers[key]
        if len(_answers) >= _ANSWER_LIMIT:
            _answers.clear()
    _answers[subject] = _Answer(kind, now + seconds, org_id, org_role)


def members_start_read_only() -> bool:
    """Whether an ordinary member of the organization gets a read-only seat by themselves.

    Off unless PROVISIONING_MEMBERS_READ says so. These are a company's books:
    the people the control plane names as owner or admin get in at sign-in, and
    everyone else is let in by an owner here (an invitation, or a role an owner
    set), not by being on the organization's member list.
    """
    return os.getenv("PROVISIONING_MEMBERS_READ", "").strip().lower() in {"1", "true", "yes", "on"}


def role_after(answer_role: str, stored_role: str | None, stored_status: str | None) -> str | None:
    """The role a control-plane answer leaves a person with here; None is no seat.

    The control plane decides who ADMINISTERS: its owner and admin are ours, and
    any other answer (manager, member, billing_admin, a role this app does not
    know) says "not an administrator", so an administrator it no longer names is
    taken down to the least privilege. Which bookkeeping role an ordinary member
    has is this workspace's own business: an owner here may have made them an
    accountant, and that survives the next answer. A person with no seat, or a
    removed one, starts read-only when members_start_read_only() and otherwise
    gets nothing from the answer alone.
    """
    decided = {"owner": "owner", "admin": "admin"}.get((answer_role or "").strip().lower())
    if decided:
        return decided
    if stored_status == "active":
        return stored_role if stored_role in _BOOKS_ROLES else "viewer"
    return "viewer" if members_start_read_only() else None


async def _seat(session, workspace_id: UUID, user_id):
    """The workspace and this person's membership row in it; None when there is no such workspace."""
    return (
        await session.execute(
            text("""
            SELECT w.id, w.slug, w.display_name, m.role::text AS role, m.status
            FROM public.workspace w
            LEFT JOIN public.workspace_membership m
              ON m.workspace_id = w.id AND m.user_id = :uid
            WHERE w.id = :wid
        """),
            {"wid": workspace_id, "uid": user_id},
        )
    ).first()


async def _without_an_answer(session, user_id):
    """Ops-Center gave no verdict: whoever is already in carries on, a newcomer is told to try again."""
    from app.api.onboarding import (
        ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL,
        EntitledProvisioningResult,
    )

    row = (
        await session.execute(
            text("""
            SELECT w.id, w.slug, w.display_name
            FROM public.workspace_membership m
            JOIN public.workspace w ON w.id = m.workspace_id
            WHERE m.user_id = :uid AND m.status = 'active' AND w.status = 'active'
            ORDER BY m.is_default DESC, m.created_at LIMIT 1
        """),
            {"uid": user_id},
        )
    ).first()
    if row is None:
        return EntitledProvisioningResult(
            "unavailable", detail=ENTITLED_PROVISIONING_UNAVAILABLE_DETAIL
        )
    return EntitledProvisioningResult(
        "existing", workspace_id=row.id, slug=row.slug, display_name=row.display_name
    )


async def _from_memory(session, user_id, remembered: _Answer):
    """What a remembered answer gives without changing anything; None when a fresh one is needed."""
    from app.api.onboarding import EntitledProvisioningResult, _workspace_id_for_org

    if remembered.kind == "not_entitled":
        return EntitledProvisioningResult("not_entitled")
    if remembered.kind == "unavailable":
        return await _without_an_answer(session, user_id)
    seat = await _seat(session, _workspace_id_for_org(remembered.org_id), user_id)
    if seat is None:
        # No workspace yet. An answer that gives no seat makes none either.
        if role_after(remembered.org_role, None, None) is None:
            return EntitledProvisioningResult("not_entitled")
        return None
    here = EntitledProvisioningResult(
        "existing", workspace_id=seat.id, slug=seat.slug, display_name=seat.display_name
    )
    if seat.status == "suspended":
        return here
    wanted = role_after(remembered.org_role, seat.role, seat.status)
    if wanted is None and seat.status != "active":
        return EntitledProvisioningResult("not_entitled")
    if wanted is not None and seat.status == "active" and seat.role == wanted:
        return here
    # The stored membership no longer matches what that answer gave (a removal,
    # a role an administrator lost or gained): ask again, under the lock.
    return None


async def ensure_jit_membership(auth, bearer, entitlement_client=None):
    from app.api.onboarding import (
        EntitledProvisioningResult,
        _org_workspace_display_name,
        _workspace_id_for_org,
    )
    from app.tenancy import _ensure_user_account

    if not bearer or not auth.may_autoprovision or auth.claims.get("iss") == "ao-local":
        return EntitledProvisioningResult("disabled")
    uid = await _ensure_user_account(auth.uc_uid, autoprovision=True)

    # 1. Nothing to ask yet, and nothing to change: no lock, no call to Ops-Center.
    remembered = _answers.get(auth.uc_uid)
    if remembered is not None and _clock() < remembered.until:
        if remembered.kind == "not_entitled":
            return EntitledProvisioningResult("not_entitled")
        async with SessionLocal() as session:
            result = await _from_memory(session, uid, remembered)
        if result is not None:
            return result

    # 2. A fresh answer, fetched AFTER the person's row is locked and with the
    # person's own bearer, never a group claim. Removal takes the same row, so an
    # answer given before a removal cannot be used after it.
    async with SessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT id FROM public.user_account WHERE id = :uid FOR NO KEY UPDATE"),
            {"uid": uid},
        )
        latest = _answers.get(auth.uc_uid)
        if latest is not None and latest is not remembered and _clock() < latest.until:
            # Another request of this person asked while this one waited for the row.
            result = await _from_memory(session, uid, latest)
            if result is not None:
                return result
        client = entitlement_client or OpsCenterEntitlementsClient()
        try:
            answer = await client.fetch(bearer)
        except Exception:
            answer = None
        if answer is not None and answer.status == "not_entitled":
            _remember(auth.uc_uid, "not_entitled", ANSWER_FRESH_SECONDS)
            return EntitledProvisioningResult("not_entitled")
        if (
            answer is None
            or answer.status != "entitled"
            or not answer.org_id
            or not (answer.org_role or "").strip()
        ):
            _remember(auth.uc_uid, "unavailable", RETRY_AFTER_FAILURE_SECONDS)
            return await _without_an_answer(session, uid)
        _remember(auth.uc_uid, "entitled", ANSWER_FRESH_SECONDS, answer.org_id, answer.org_role)
        wid = _workspace_id_for_org(answer.org_id)
        # The row an owner here may be changing at this moment (a role, an invitation): hold it, then read.
        await session.execute(
            text("""
            SELECT 1 FROM public.workspace_membership
            WHERE workspace_id = :wid AND user_id = :uid FOR UPDATE
        """),
            {"wid": wid, "uid": uid},
        )
        seat = await _seat(session, wid, uid)
        if seat is not None and seat.status == "suspended":
            # Paused by an owner here: sign-in never resumes it (rule 4).
            return EntitledProvisioningResult(
                "existing", workspace_id=seat.id, slug=seat.slug, display_name=seat.display_name
            )
        role = role_after(
            answer.org_role, seat.role if seat else None, seat.status if seat else None
        )
        if role is None:
            # In the organization, with no seat in its books: nothing is created.
            return EntitledProvisioningResult("not_entitled")
        row, created = await ensure_workspace(
            session,
            answer.org_id,
            _org_workspace_display_name(answer.org_id, answer.org_name),
            auth.uc_uid,
        )
        await session.execute(
            text("""
            INSERT INTO public.workspace_membership (id, workspace_id, user_id, role, status)
            VALUES (gen_random_uuid(), :wid, :uid, CAST(:role AS workspace_role), 'active')
            ON CONFLICT ON CONSTRAINT uq_workspace_membership DO UPDATE
            SET role = EXCLUDED.role, status = 'active'
            WHERE workspace_membership.status != 'suspended'
              AND (workspace_membership.status != 'active'
                   OR workspace_membership.role != EXCLUDED.role)
        """),
            {"wid": row.id, "uid": uid, "role": role},
        )
        return EntitledProvisioningResult(
            "provisioned" if created else "existing",
            workspace_id=row.id,
            slug=row.slug,
            display_name=row.display_name,
        )
