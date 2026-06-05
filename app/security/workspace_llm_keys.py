"""Per-workspace LLM key resolution for the chat/agent inference path.

Reuses the AES-256-GCM tenant_credentials store. Two credential types:
  - connector ``litellm_virtual_key`` (label ``platform``): the workspace's
    minted UC-gateway virtual key (``sk-…``). Lazy-minted on first use.
  - connector ``byo_llm_key`` (label = provider): a customer's OWN provider key,
    stored as JSON ``{"key","provider","base_url"}`` so provider + optional
    endpoint travel with it.

Called by agents/chat/inference_config.py. Functions return ``None`` on absence;
the resolver wraps calls in try/except and falls back to today's behavior, so a
DB/gateway hiccup can never break live chat.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.db import SessionLocal
from app.security.credentials import get_credential_plaintext, store_credential
from app.security.litellm_admin import DEFAULT_ALLOWED_MODELS, mint_virtual_key

_log = logging.getLogger("acctops.workspace_llm_keys")

CONNECTOR_VIRTUAL = "litellm_virtual_key"
CONNECTOR_BYOK = "byo_llm_key"
_VIRTUAL_LABEL = "platform"


async def resolve_billing_customer_id(
    workspace_id: str, *, claims: dict[str, Any] | None = None
) -> str | None:
    """The Lago customer id a workspace's usage meters under — the virtual key's
    ``user_id`` and the gateway's ``ai_api_call`` event ``user``.

    This is the **UC platform org** (``org_<uuid>``), resolved via
    ``app.platform.org_resolver``: usage must meter to the org that actually owns
    the UC subscription, NOT a per-workspace synthetic. Returns ``None`` when the
    workspace has no resolved org yet (unbackfilled + no org claim) — the caller
    then declines to mint rather than create a key billed to a customer that
    doesn't exist in Lago.
    """
    from app.platform.org_resolver import resolve_org_id

    return await resolve_org_id(str(workspace_id), claims=claims)


async def get_byok(workspace_id: str) -> tuple[str, str, str | None] | None:
    """Return ``(api_key, provider, base_url|None)`` if the workspace has a BYOK
    credential, else ``None``."""
    wid = uuid.UUID(str(workspace_id))
    async with SessionLocal() as session:
        raw = await get_credential_plaintext(
            session, wid, CONNECTOR_BYOK, requesting_service="chat_inference"
        )
        await session.commit()
    if not raw:
        return None
    try:
        blob = json.loads(raw)
        key = blob.get("key")
        if not key:
            return None
        return (key, blob.get("provider") or "unknown", blob.get("base_url") or None)
    except (json.JSONDecodeError, AttributeError, TypeError):
        # Legacy/plain-string value = treat the whole thing as the key.
        return (raw, "unknown", None)


async def get_or_mint_virtual_key(
    workspace_id: str,
    *,
    max_budget: float | None = None,
    claims: dict[str, Any] | None = None,
) -> str | None:
    """Return the workspace's gateway virtual key, lazily minting + storing it on
    first use. Returns ``None`` when minting is unconfigured (no scoped admin
    key) and no key exists yet — the resolver then falls back to the platform
    key / OpenRouter.
    """
    wid = uuid.UUID(str(workspace_id))
    async with SessionLocal() as session:
        existing = await get_credential_plaintext(
            session,
            wid,
            CONNECTOR_VIRTUAL,
            label=_VIRTUAL_LABEL,
            requesting_service="chat_inference",
        )
        await session.commit()
    if existing:
        return existing

    # No per-workspace key yet. Minting requires a scoped admin key; when it is
    # not configured (e.g. the internal ring routes everyone through a single
    # platform virtual key), return quietly so the resolver falls back to the
    # platform key — no per-turn mint attempt and no per-turn warning.
    from app.config import get_settings

    if not get_settings().llm_gateway_admin_key:
        return None

    # Mint AGAINST THE ORG, never a per-workspace synthetic — usage has to meter
    # to the UC customer that owns the subscription. No resolved org ⇒ no mint;
    # the resolver falls back to the platform key, so chat still works while the
    # org bridge is unbackfilled (rather than spawning an orphan Lago customer).
    customer_id = await resolve_billing_customer_id(workspace_id, claims=claims)
    if not customer_id:
        _log.info(
            "virtual-key mint skipped: workspace %s has no resolved UC org", workspace_id
        )
        return None

    try:
        key = await mint_virtual_key(
            customer_id=customer_id,
            max_budget=max_budget,
            models=DEFAULT_ALLOWED_MODELS,
            metadata={
                "workspace_id": str(workspace_id),
                "org_id": customer_id,
                "source": "accounting-ops",
            },
        )
    except Exception as e:  # noqa: BLE001 — minting is best-effort
        _log.warning("virtual-key mint skipped for workspace %s: %s", workspace_id, e)
        return None

    # ON CONFLICT (workspace_id, connector, label) makes a concurrent double-mint
    # idempotent (last write wins; a duplicate mint just wastes one key).
    async with SessionLocal() as session:
        await store_credential(
            session, wid, CONNECTOR_VIRTUAL, _VIRTUAL_LABEL, key, created_by_uc_uid=None
        )
        await session.commit()
    return key
