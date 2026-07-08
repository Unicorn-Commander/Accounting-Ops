"""BYOK (bring-your-own-key) inference-settings endpoints.

A purely ADDITIVE settings surface that lets a workspace owner store, view the
status of, or remove their OWN LLM provider key. This is the write/read API the
``web/app/settings/inference`` page talks to; the *resolution* side
(``app/security/workspace_llm_keys.py``: ``get_byok``) reads exactly the shape
this endpoint stores, so the chat/agent inference path picks the key up with no
changes here.

Endpoints (all under /api/workspaces/{workspace_id}/...):

  GET    inference/byok   — status only: {configured, provider, key_last4}.
                            NEVER returns the plaintext key. Role floor: viewer.
  POST   inference/byok   — store/replace a BYOK key. Role floor: owner/admin.
  DELETE inference/byok   — remove the BYOK key (revert to UC platform credits).
                            Role floor: owner.

Storage contract (the resolver's contract — do not change without changing
``workspace_llm_keys.get_byok``):

  connector = "byo_llm_key"
  label     = provider           (e.g. "anthropic" | "openrouter")
  value     = json.dumps({"key": <api_key>, "provider": <provider>,
                          "base_url": <base_url|null>})

Security: the plaintext key is never returned and never logged. The only thing
this module ever surfaces about a stored key is its last 4 characters.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.db import SessionLocal
from app.security.credentials import (
    delete_credential,
    get_credential_plaintext,
    list_credentials,
    store_credential,
)
from app.tenancy import WorkspaceContext, require_role, resolve_workspace

_log = logging.getLogger("acctops.inference_settings")

router = APIRouter(tags=["inference"])

# The single connector the resolver (workspace_llm_keys.get_byok) reads. Kept as
# a local literal — NOT imported from workspace_llm_keys — so this additive
# module never reaches into the do-not-touch resolver module. The value MUST
# stay in lockstep with workspace_llm_keys.CONNECTOR_BYOK.
CONNECTOR_BYOK = "byo_llm_key"

# Providers a customer may bring a key for. anthropic = direct Anthropic API;
# openrouter = an OpenRouter key (breadth). Anything else is rejected — we never
# store a key for an unvalidated provider.
ALLOWED_PROVIDERS = ("anthropic", "openrouter")


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class ByokStatus(BaseModel):
    """What the UI reads. Never carries the key itself."""

    configured: bool
    provider: str | None = None
    key_last4: str | None = None
    base_url: str | None = None


class ByokCreate(BaseModel):
    provider: str = Field(..., description="anthropic | openrouter")
    api_key: str = Field(..., min_length=8, max_length=512)
    base_url: str | None = Field(default=None, max_length=512)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _last4(secret: str) -> str:
    """Last 4 chars of a key (or the whole thing if shorter). Never the key."""
    s = secret or ""
    return s[-4:] if len(s) >= 4 else s


async def _read_status(workspace_id: UUID) -> ByokStatus:
    """Decrypt the stored BYOK blob just far enough to surface status.

    Opens its own bare ``SessionLocal`` session (the credentials store sets the
    workspace GUC itself) and commits. Returns an unconfigured status when no
    BYOK credential exists OR when a stored value can't be parsed into a key —
    we never raise to the caller for a malformed/absent key, and we never echo
    the key back.
    """
    async with SessionLocal() as session:
        raw = await get_credential_plaintext(
            session,
            workspace_id,
            CONNECTOR_BYOK,
            requesting_service="inference_settings",
        )
        await session.commit()

    if not raw:
        return ByokStatus(configured=False)

    try:
        blob = json.loads(raw)
        key = blob.get("key")
        if not key:
            return ByokStatus(configured=False)
        return ByokStatus(
            configured=True,
            provider=blob.get("provider") or "unknown",
            key_last4=_last4(str(key)),
            base_url=blob.get("base_url") or None,
        )
    except (json.JSONDecodeError, AttributeError, TypeError):
        # Legacy/plain-string value: treat the whole thing as the key, mirroring
        # workspace_llm_keys.get_byok's tolerance. Still no plaintext leaves here.
        return ByokStatus(
            configured=True, provider="unknown", key_last4=_last4(str(raw))
        )


# ---------------------------------------------------------------------------
# GET — status only
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/inference/byok")
async def get_byok_status(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> ByokStatus:
    """Return whether this workspace has a BYOK key + its provider/last-4.

    Never returns the plaintext key. Role floor: viewer (anyone who can see the
    workspace can see whether it's on its own key or on UC credits).
    """
    require_role(workspace, "viewer")
    return await _read_status(workspace.workspace_id)


# ---------------------------------------------------------------------------
# POST — store / replace
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/inference/byok")
async def set_byok(
    workspace_id: UUID,
    body: ByokCreate,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> ByokStatus:
    """Store (or replace) the workspace's own LLM provider key.

    Validates the provider, then persists the resolver's exact JSON shape under
    connector ``byo_llm_key`` (label = provider). store_credential is idempotent
    on (workspace, connector, label), so re-saving overwrites in place.

    Role floor: admin (owner/admin/client_owner/platform_admin). The key is
    never logged; we return status only.
    """
    require_role(workspace, "admin")

    provider = (body.provider or "").strip().lower()
    if provider not in ALLOWED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported provider '{body.provider}'. "
                f"Choose one of: {', '.join(ALLOWED_PROVIDERS)}."
            ),
        )

    api_key = (body.api_key or "").strip()
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="api_key is required"
        )
    base_url = (body.base_url or "").strip() or None

    # The EXACT shape workspace_llm_keys.get_byok parses.
    value = json.dumps({"key": api_key, "provider": provider, "base_url": base_url})

    async with SessionLocal() as session:
        await store_credential(
            session,
            workspace.workspace_id,
            CONNECTOR_BYOK,
            provider,
            value,
            created_by_uc_uid=workspace.user_uc_uid,
        )
        await session.commit()

    # Log only non-sensitive facts.
    _log.info(
        "BYOK key stored for workspace %s (provider=%s)",
        workspace.workspace_id,
        provider,
    )

    return ByokStatus(
        configured=True,
        provider=provider,
        key_last4=_last4(api_key),
        base_url=base_url,
    )


# ---------------------------------------------------------------------------
# DELETE — remove (revert to UC platform credits)
# ---------------------------------------------------------------------------


@router.delete("/api/workspaces/{workspace_id}/inference/byok")
async def delete_byok(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Remove the workspace's BYOK key, reverting to UC platform credits.

    Finds the ``byo_llm_key`` credential(s) for this workspace and deletes them.
    Idempotent: removing when nothing is configured returns ``removed: false``
    rather than erroring. Role floor: owner.
    """
    require_role(workspace, "owner")

    removed_any = False
    async with SessionLocal() as session:
        creds = await list_credentials(session, workspace.workspace_id)
        for c in creds:
            if c.connector == CONNECTOR_BYOK:
                ok = await delete_credential(
                    session,
                    workspace.workspace_id,
                    c.id,
                    actor_uc_uid=workspace.user_uc_uid,
                )
                removed_any = removed_any or ok
        await session.commit()

    if removed_any:
        _log.info("BYOK key removed for workspace %s", workspace.workspace_id)

    return {"ok": True, "removed": removed_any}


__all__ = ("router",)
