# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""One transactional removal policy for control-plane and local member deletion."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.auth import _load_service_tokens
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role


async def remove_person(
    workspace_id: UUID,
    *,
    subject: str | None = None,
    user_id: UUID | None = None,
    actor: WorkspaceContext | None = None,
) -> dict:
    """End membership, revoke scoped PATs, and record a receipt atomically.

    The user row lock serializes removals across workspaces and fresh JIT
    entitlement reads. Local callers must have a non-revoked membership target
    before any changes or credential counts; only the control plane may clean
    credentials left behind by an earlier local removal.
    """
    if actor is not None:
        require_role(actor, "admin")
    async with SessionLocal() as session, session.begin():
        if not (
            await session.execute(
                text("SELECT 1 FROM public.workspace WHERE id = :wid"), {"wid": workspace_id}
            )
        ).first():
            raise HTTPException(404, "Workspace not found")
        person = (
            await session.execute(
                text(
                    "SELECT id, uc_uid FROM public.user_account WHERE "
                    + ("id = :person" if user_id is not None else "uc_uid = :person")
                    + " FOR UPDATE"
                ),
                {"person": user_id if user_id is not None else subject},
            )
        ).first()
        if person is None:
            if actor is not None:
                raise HTTPException(404, "No such member")
            return {"revoked_pat_count": 0, "static_service_token_count": 0}
        params = {"wid": workspace_id, "uid": person.id}
        member = (
            await session.execute(
                text("""
            SELECT role::text AS role, entity_scope, status
            FROM public.workspace_membership
            WHERE workspace_id = :wid AND user_id = :uid FOR UPDATE
        """),
                params,
            )
        ).first()
        if actor is not None:
            if member is None or member.status == "revoked":
                raise HTTPException(404, "No such member")
            if person.uc_uid == actor.user_uc_uid:
                raise HTTPException(400, "Cannot remove yourself")
            if member.role in {"owner", "admin", "client_owner"}:
                require_role(actor, "owner")
            if member.role in {"owner", "client_owner"}:
                # Serialize local last-owner checks without blocking CP removal.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:wid, 218))"),
                    {"wid": str(workspace_id)},
                )
                owners = (
                    await session.execute(
                        text("""
                    SELECT count(*) FROM public.workspace_membership
                    WHERE workspace_id = :wid AND role IN ('owner', 'client_owner')
                      AND status != 'revoked'
                """),
                        {"wid": workspace_id},
                    )
                ).scalar_one()
                if owners <= 1:
                    raise HTTPException(400, "Cannot remove the last owner")
        # A tombstone uses existing UPDATE grants, preserves pause semantics and
        # remains excluded by the unmodified active-membership authorization.
        await session.execute(
            text("""
            UPDATE public.workspace_membership SET status = 'revoked'
            WHERE workspace_id = :wid AND user_id = :uid AND status != 'revoked'
        """),
            params,
        )
        revoked = (
            await session.execute(
                text("""
            UPDATE public.personal_access_token SET revoked_at = clock_timestamp()
            WHERE owner_user_id = :uid AND revoked_at IS NULL
              AND (
                workspace_scope = ARRAY[CAST(:wid AS uuid)]
                OR ((workspace_scope IS NULL OR cardinality(workspace_scope) != 1)
                    AND NOT EXISTS (
                        SELECT 1 FROM public.workspace_membership
                        WHERE user_id = :uid AND status = 'active'
                    ))
              )
            RETURNING id
        """),
                params,
            )
        ).all()
        static_count = sum(
            entry.get("uc_uid") == person.uc_uid for entry in _load_service_tokens().values()
        )
        receipt = {"revoked_pat_count": len(revoked), "static_service_token_count": static_count}
        await session.execute(
            text("SELECT set_config('app.current_workspace', :wid, true)"),
            {"wid": str(workspace_id)},
        )
        # Repeated removal intentionally advances occurred_at (rule 13), without
        # revoking a credential twice. No owner email or bearer is recorded.
        await session.execute(
            text("""
            INSERT INTO public.membership_audit (
                workspace_id, actor_uc_uid, action, target_user_id, old_role,
                revoked_pat_count, static_service_token_count, occurred_at
            ) VALUES (:wid, :actor, 'removal', :uid, :role, :revoked_pat_count,
                      :static_service_token_count, clock_timestamp())
        """),
            {
                **params,
                **receipt,
                "actor": actor.user_uc_uid if actor else "ops-center",
                "role": member.role if member else None,
            },
        )
        return receipt
