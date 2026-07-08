#!/usr/bin/env python3
"""Grant a uc_uid an (owner-role) membership on a workspace.

Used by the Community Edition self-host bootstrap so the single local admin
(AO_LOCAL_ADMIN_UC_UID) can actually reach the provisioned workspace: RBAC
resolves access via a ``public.workspace_membership`` row keyed on the uc_uid's
``user_account``. This script creates that user_account (if new) and the
membership row (if new) as the ADMIN/owner role. Idempotent.

Usage:
    python -m scripts.grant_workspace_admin <workspace_uuid> <uc_uid> [--role owner]

Valid roles: viewer | accountant | admin | owner  (default: owner)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_admin_database_url  # noqa: E402

_VALID_ROLES = {"viewer", "accountant", "admin", "owner"}


async def grant_workspace_admin(
    workspace_id: uuid.UUID, uc_uid: str, role: str = "owner"
) -> str:
    """Ensure user_account + workspace_membership. Returns 'created'|'exists'."""
    if role not in _VALID_ROLES:
        raise SystemExit(f"Invalid role {role!r}. Valid: {sorted(_VALID_ROLES)}")
    engine = create_async_engine(get_admin_database_url(), pool_pre_ping=True)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as session:
            async with session.begin():
                user_id = (
                    await session.execute(
                        text("SELECT id FROM public.user_account WHERE uc_uid = :uc"),
                        {"uc": uc_uid},
                    )
                ).scalar()
                if user_id is None:
                    user_id = (
                        await session.execute(
                            text(
                                """
                                INSERT INTO public.user_account
                                    (id, uc_uid, is_active, created_at)
                                VALUES (gen_random_uuid(), :uc, true, now())
                                RETURNING id
                                """
                            ),
                            {"uc": uc_uid},
                        )
                    ).scalar_one()

                exists = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM public.workspace_membership "
                            "WHERE workspace_id = :wid AND user_id = :uid"
                        ),
                        {"wid": str(workspace_id), "uid": str(user_id)},
                    )
                ).first()
                if exists:
                    return "exists"
                await session.execute(
                    text(
                        """
                        INSERT INTO public.workspace_membership (
                            id, workspace_id, user_id, role, entity_scope,
                            invited_by_uc_uid, created_at
                        ) VALUES (
                            gen_random_uuid(), :wid, :uid,
                            CAST(:role AS workspace_role), NULL,
                            'selfhost-bootstrap', now()
                        )
                        """
                    ),
                    {"wid": str(workspace_id), "uid": str(user_id), "role": role},
                )
        return "created"
    finally:
        await engine.dispose()


async def _main() -> None:
    p = argparse.ArgumentParser(description="Grant a workspace membership.")
    p.add_argument("workspace_id", help="Workspace UUID")
    p.add_argument("uc_uid", help="Canonical user id (matches AO_LOCAL_ADMIN_UC_UID)")
    p.add_argument("--role", default="owner", help="Role (default: owner)")
    args = p.parse_args()
    outcome = await grant_workspace_admin(
        uuid.UUID(args.workspace_id), args.uc_uid, args.role
    )
    print(f"membership {outcome}: {args.uc_uid} -> {args.workspace_id} ({args.role})")


if __name__ == "__main__":
    asyncio.run(_main())
