"""Resolve the UC platform org (``org_<uuid>``) a workspace belongs to.

The org is the UC billing customer (Lago ``external_id`` = ``org_<uuid>``) + the
Keycloak org. AO meters inference + reads entitlement against it.

Resolution precedence:
  1. **Token claim** (``org_id`` / ``organization`` / ``uc_org_id``) — the
     long-term mechanism: once the uchub realm emits an org claim, EVERY UC app
     gets the org for free with zero per-app work. Read FIRST so this is
     zero-rework when the platform fix lands. (No such claim is populated today.)
  2. **``public.workspace.uc_org_id``** (migration 0012) — the durable bridge an
     operator backfills per workspace until the claim exists.

FAIL-SAFE: any error → ``None``. Callers (metering, entitlement) MUST degrade to
today's behavior when the org is unknown — never crash a chat turn or a request.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import text

from app.db import SessionLocal

_log = logging.getLogger("acctops.platform.org_resolver")

# Claim names the uchub realm MAY emit once an org mapper is added (none are
# populated today — read in order, first non-empty wins).
_ORG_CLAIM_KEYS = ("org_id", "organization", "uc_org_id")


def _org_from_claims(claims: dict[str, Any] | None) -> str | None:
    if not claims:
        return None
    for key in _ORG_CLAIM_KEYS:
        v = claims.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        # Keycloak's native `organization` claim can be a dict (org-name → attrs)
        # or a list; take the first org id/name.
        if isinstance(v, dict) and v:
            first = next(iter(v.keys()))
            if isinstance(first, str) and first.strip():
                return first.strip()
        if isinstance(v, (list, tuple)) and v:
            f = v[0]
            if isinstance(f, str) and f.strip():
                return f.strip()
    return None


async def _org_from_column(workspace_id: str) -> str | None:
    try:
        wid = uuid.UUID(str(workspace_id))
    except (ValueError, TypeError):
        return None
    async with SessionLocal() as session:
        row = (
            await session.execute(
                text("SELECT uc_org_id FROM public.workspace WHERE id = :wid"),
                {"wid": str(wid)},
            )
        ).first()
    if row and row[0]:
        return str(row[0])
    return None


async def resolve_org_id(
    workspace_id: str, *, claims: dict[str, Any] | None = None
) -> str | None:
    """Return the workspace's UC org id (``org_<uuid>``), or ``None`` when unknown.

    Precedence: token claim → ``workspace.uc_org_id`` column. Never raises.
    """
    try:
        from_claim = _org_from_claims(claims)
        if from_claim:
            return from_claim
        return await _org_from_column(workspace_id)
    except Exception as e:  # noqa: BLE001 — resolution is best-effort
        _log.warning("org resolution failed (ws=%s): %s", workspace_id, e)
        return None
