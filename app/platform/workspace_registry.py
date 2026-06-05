"""Resolve a uc-registry tenant id to the internal AO workspace id.

Cross-app federation tokens (Brigade → Customer-Ops, etc.) carry the
**uc-registry** tenant id as their ``workspace_id`` claim. AO's workspaces have
their own internal ids; ``public.workspace.uc_registry_id`` (migration 0013) is
the durable bridge. For a verified federation token the caller maps the registry
id to the internal workspace BEFORE membership resolution + RLS binding, so AO's
existing, proven membership authz applies unchanged — there is **no** authz
bypass (the delegated user must still be a member of the resolved workspace).

FAIL-SAFE: an unmapped/unknown id is returned UNCHANGED (the caller's membership
lookup then 403s); any DB error returns the input unchanged — never widen access
on error.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal

_log = logging.getLogger("acctops.platform.workspace_registry")


def _trusted_issuer() -> str:
    # Mirrors app/security/brigade_jwt._brigade_trusted_issuer (single source of
    # truth is the env var; default is the prod Brigade issuer).
    return os.getenv("BRIGADE_TRUSTED_ISSUER", "https://brigade.unicorncommander.ai")


def is_federation_token(auth: Any) -> bool:
    """True iff this AuthContext is a verified Brigade federation token.

    Only Brigade-issued tokens carry the trusted issuer in their claims; an
    internal Keycloak user/agent token or an internal service token never does.
    Used to gate the registry→internal workspace mapping to federation calls only
    — so the normal hot path pays no extra DB query.
    """
    try:
        claims = getattr(auth, "claims", None) or {}
        return claims.get("iss") == _trusted_issuer()
    except Exception:  # noqa: BLE001 — defensive; unknown shape => not federation
        return False


def federation_claim_workspace_id(auth: Any) -> str | None:
    """The ``workspace_id`` the federation token is scoped to (registry id), if any."""
    try:
        claims = getattr(auth, "claims", None) or {}
        wid = claims.get("workspace_id")
        return str(wid) if wid else None
    except Exception:  # noqa: BLE001
        return None


async def resolve_internal_workspace_id(workspace_id: UUID) -> UUID:
    """Map a uc-registry tenant id to the internal AO workspace id.

    Returns the internal ``public.workspace.id`` whose ``uc_registry_id`` equals
    ``workspace_id`` (active only); else returns ``workspace_id`` UNCHANGED (it is
    already an internal id, or unknown — the caller's membership lookup will 403).
    Never raises.
    """
    try:
        async with SessionLocal() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM public.workspace "
                        "WHERE uc_registry_id = :wid AND status = 'active' LIMIT 1"
                    ),
                    {"wid": str(workspace_id)},
                )
            ).first()
        if row and row[0]:
            return row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))
    except Exception as e:  # noqa: BLE001 — best-effort, fail-safe (never widen)
        _log.warning(
            "registry workspace resolution failed (wid=%s): %s", workspace_id, e
        )
    return workspace_id


__all__ = (
    "is_federation_token",
    "federation_claim_workspace_id",
    "resolve_internal_workspace_id",
)
