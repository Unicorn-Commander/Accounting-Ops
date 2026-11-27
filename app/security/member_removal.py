# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""One transactional removal policy for control-plane and local member deletion."""

import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.auth import _load_service_tokens
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role


async def cancel_elevated_invitations_created_by(
    session, workspace_id: UUID, creator_uc_uid: str
) -> list:
    """Cancel pending admin/owner invitations this person sent (AO-ROLE-1b).

    An elevated invitation carries its creator's authority. Once the creator is
    removed or no longer an owner, the link must not still grant the role.
    Returns the cancelled invitation rows (``.id``) for the caller's audit.
    """
    await session.execute(
        text("SELECT set_config('app.current_workspace', :wid, true)"),
        {"wid": str(workspace_id)},
    )
    return (
        await session.execute(
            text("""
        UPDATE public.workspace_invitation SET cancelled_at = clock_timestamp()
        WHERE workspace_id = :wid AND accepted_at IS NULL AND cancelled_at IS NULL
          AND created_by_uc_uid = :creator
          AND role IN ('owner', 'client_owner', 'admin')
        RETURNING id
    """),
            {"wid": workspace_id, "creator": creator_uc_uid},
        )
    ).all()


async def acquire_last_owner_lock(session, workspace_id: UUID) -> None:
    """Serialize local last-owner checks for one workspace in this transaction.

    Keyed on (workspace id, 218) — the same key remove_person uses, so a
    removal racing an owner demotion blocks behind one or the other instead
    of both passing the last-owner count.

    Ordering rule (AO-RACE-2b): every caller acquires THIS advisory lock
    before taking any FOR UPDATE row lock on user_account or
    workspace_membership for the workspace it guards. Both last-owner paths
    (remove_person and the demotion guard in members.update_member) then take
    the lock in the same order, so a demotion racing a removal of the same
    owner serializes on the lock instead of deadlocking on the row locks.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:wid, 218))"),
        {"wid": str(workspace_id)},
    )


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

    A removal also ends what could undo it: an invitation sent to this person
    for this workspace and not yet accepted is cancelled, so an old link does
    not bring them back. The owner's next invitation starts the membership again.
    A paused owner cannot act, so only active owners count as "the last owner".
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
        if actor is not None:
            # AO-RACE-2b: the owner branch below FOR UPDATEs the membership
            # row; take the shared last-owner lock FIRST (see the ordering
            # rule on acquire_last_owner_lock) so a racing owner demotion in
            # update_member, which locks in the same order, serializes on the
            # advisory lock instead of deadlocking on the row locks. The
            # target role is unknown until the row reads, so a local owner
            # removal takes the lock unconditionally; non-owner removals and
            # control-plane removals (actor is None) pay the uncontended lock
            # or none at all.
            await acquire_last_owner_lock(session, workspace_id)
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
                # The lock is already held from before the row reads above.
                owners = (
                    await session.execute(
                        text("""
                    SELECT count(*) FROM public.workspace_membership
                    WHERE workspace_id = :wid AND role IN ('owner', 'client_owner')
                      AND status = 'active'
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
                workspace_id, actor_uc_uid, action, target_user_id, old_role, old_entity_scope,
                revoked_pat_count, static_service_token_count, occurred_at
            ) VALUES (:wid, :actor, 'removal', :uid, :role, CAST(:scope AS jsonb), :revoked_pat_count,
                      :static_service_token_count, clock_timestamp())
        """),
            {
                **params,
                **receipt,
                "actor": actor.user_uc_uid if actor else "ops-center",
                "role": member.role if member else None,
                "scope": (
                    json.dumps(member.entity_scope)
                    if member and member.entity_scope is not None else None
                ),
            },
        )
        # Invitation emails are lower-cased on write. A person with no email on
        # file has nothing to match; acceptance needs a verified email anyway.
        cancelled = (
            await session.execute(
                text("""
            UPDATE public.workspace_invitation SET cancelled_at = clock_timestamp()
            WHERE workspace_id = :wid AND accepted_at IS NULL AND cancelled_at IS NULL
              AND email = (SELECT lower(email) FROM public.user_account WHERE id = :uid)
            RETURNING id
        """),
                params,
            )
        ).all()
        # Elevated invitations this person created lose their authority too.
        cancelled = list(cancelled) + list(
            await cancel_elevated_invitations_created_by(session, workspace_id, person.uc_uid)
        )
        for invitation in cancelled:
            await session.execute(
                text("""
                INSERT INTO public.membership_audit (
                    workspace_id, actor_uc_uid, action, target_user_id, invitation_id, occurred_at
                ) VALUES (:wid, :actor, 'invitation_cancel', :uid, :invitation, clock_timestamp())
            """),
                {
                    **params,
                    "actor": actor.user_uc_uid if actor else "ops-center",
                    "invitation": invitation.id,
                },
            )
        return receipt
