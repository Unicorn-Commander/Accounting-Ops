"""Resolve a Unicorn Commander ``uc_`` API key via Ops-Center validate-key.

Customer agents mint a key on unicorncommander.ai (scopes include
``mcp:accounting-ops`` or ``mcp:*``) and present it as
``Authorization: Bearer uc_…`` against Accounting-Ops ``/mcp``.

We deliberately do NOT re-implement bcrypt/hash lookup against Ops-Center's
``user_api_keys`` table. Identity, permissions, and (via a follow-up meter
call) billing all go through the same gateway path the LLM routes use:

    POST {OPS_CENTER_BASE_URL}/api/v1/auth/validate-key
      body: {"key": "uc_…", "required_permission": "mcp:accounting-ops"}

On success we get ``user.id`` (Keycloak sub == AO ``uc_uid``) and the key's
permissions list. On failure we return None so authenticate_token can 401.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

_log = logging.getLogger("acctops.auth.uc_api_key")

UC_KEY_PREFIX = "uc_"
APP_MCP_PERMISSION = "mcp:accounting-ops"
VALIDATE_PATH = "/api/v1/auth/validate-key"
METER_PATH = "/api/v1/mcp/meter"

# Short timeout: auth is on the hot path for every MCP request.
_VALIDATE_TIMEOUT_SEC = 5.0
_METER_TIMEOUT_SEC = 3.0


@dataclass(frozen=True)
class UcKeyIdentity:
    """Resolved UC API key identity for AuthContext construction."""

    uc_uid: str
    permissions: tuple[str, ...]
    key_id: str | None
    org_id: str | None
    subscription_tier: str | None
    raw: dict[str, Any]


def looks_like_uc_key(token: str) -> bool:
    """Cheap pre-check: wire format is ``uc_`` + hex."""
    return bool(token) and token.startswith(UC_KEY_PREFIX) and len(token) > 10


def _validate_url() -> str:
    settings = get_settings()
    base = (settings.ops_center_base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("OPS_CENTER_BASE_URL is not configured")
    # Prefer api.unicorncommander.ai when the base is the SPA host
    # (validate-key lives on the API surface; the SPA host also routes /api/*).
    return f"{base}{VALIDATE_PATH}"


async def resolve_uc_api_key(token: str) -> UcKeyIdentity | None:
    """Validate a uc_ key against Ops-Center. None on any failure."""
    if not looks_like_uc_key(token):
        return None

    url = _validate_url()
    payload = {
        "key": token,
        "required_permission": APP_MCP_PERMISSION,
    }
    try:
        async with httpx.AsyncClient(
            timeout=_VALIDATE_TIMEOUT_SEC,
            headers={
                "User-Agent": "accounting-ops/0.1 (+https://accounting-ops.unicorncommander.ai)",
                "Content-Type": "application/json",
            },
        ) as client:
            resp = await client.post(url, json=payload)
    except Exception as exc:
        _log.warning("uc_ key validate-key transport failed: %s", exc.__class__.__name__)
        return None

    if resp.status_code != 200:
        _log.info("uc_ key validate-key HTTP %s", resp.status_code)
        return None

    try:
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, dict) or not data.get("valid"):
        reason = data.get("reason") if isinstance(data, dict) else None
        _log.info("uc_ key rejected by gateway (reason=%s)", reason)
        return None

    user = data.get("user") or {}
    uc_uid = user.get("id")
    if not uc_uid or not isinstance(uc_uid, str):
        _log.warning("uc_ key validate-key returned valid without user.id")
        return None

    perms_raw = user.get("permissions") or []
    if isinstance(perms_raw, str):
        perms: list[str] = [perms_raw]
    elif isinstance(perms_raw, list):
        perms = [str(p) for p in perms_raw]
    else:
        perms = []

    org = user.get("organization") or {}
    org_id = org.get("id") if isinstance(org, dict) else None

    return UcKeyIdentity(
        uc_uid=uc_uid,
        permissions=tuple(perms),
        key_id=str(user["key_id"]) if user.get("key_id") else None,
        org_id=str(org_id) if org_id else None,
        subscription_tier=user.get("subscription_tier"),
        raw=data,
    )


async def meter_uc_mcp_call(
    *,
    token: str,
    tool: str,
    cost_usd: float | None = None,
) -> dict[str, Any] | None:
    """Best-effort debit of the shared credit ledger for one MCP tool call.

    Never raises — returns None on failure so the MCP path stays available
    if the gateway is briefly unreachable. Callers may log the result.
    """
    if not looks_like_uc_key(token):
        return None
    settings = get_settings()
    base = (settings.ops_center_base_url or "").rstrip("/")
    if not base:
        return None
    url = f"{base}{METER_PATH}"
    body: dict[str, Any] = {
        "app": "accounting-ops",
        "tool": tool,
    }
    if cost_usd is not None:
        body["cost_usd"] = cost_usd
    try:
        async with httpx.AsyncClient(
            timeout=_METER_TIMEOUT_SEC,
            headers={
                "User-Agent": "accounting-ops/0.1 (+https://accounting-ops.unicorncommander.ai)",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        ) as client:
            resp = await client.post(url, json=body)
        if resp.status_code >= 400:
            _log.info(
                "uc_ mcp meter HTTP %s: %s",
                resp.status_code,
                (resp.text or "")[:200],
            )
            return None
        return resp.json()
    except Exception as exc:
        _log.debug("uc_ mcp meter failed: %s", exc.__class__.__name__)
        return None


__all__ = (
    "UcKeyIdentity",
    "looks_like_uc_key",
    "resolve_uc_api_key",
    "meter_uc_mcp_call",
    "APP_MCP_PERMISSION",
    "UC_KEY_PREFIX",
)
