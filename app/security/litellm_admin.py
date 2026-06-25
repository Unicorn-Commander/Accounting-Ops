"""Scoped-admin client for the UC LiteLLM gateway.

Mints per-workspace VIRTUAL keys using a SCOPED admin key (NOT the gateway
master key — see settings.llm_gateway_admin_key). A virtual key is what AO uses
for a workspace's frontier inference; the gateway meters its usage to Lago
(event ``ai_api_call``) keyed on the virtual key's ``user_id`` (the workspace's
Lago customer). The ``max_budget`` is the hard loss-stop: the gateway rejects
once a key's upstream spend exceeds its budget. See docs/INFERENCE-AND-BILLING.md.

Security: this module NEVER logs key material — not the admin key, not the
minted key, not the gateway response body (which can echo a key).
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

_log = logging.getLogger("acctops.litellm_admin")

# The AO-scoped models a workspace key is allowed to reach. A minted key can ONLY
# call these — never the gateway's fleet wildcards (least privilege).
DEFAULT_ALLOWED_MODELS: tuple[str, ...] = (
    "accounting-ops-sonnet",
    "accounting-ops-opus",
)


class LiteLLMAdminError(RuntimeError):
    """A gateway admin call failed. Message NEVER contains key material."""


async def mint_virtual_key(
    *,
    customer_id: str,
    max_budget: float | None = None,
    budget_duration: str | None = None,
    models: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_MODELS,
    rpm_limit: int | None = None,
    tpm_limit: int | None = None,
    metadata: dict | None = None,
    timeout_s: float = 20.0,
) -> str:
    """Mint a workspace-scoped virtual key on the gateway. Returns the ``sk-…`` key.

    Raises LiteLLMAdminError when minting is unconfigured (no scoped admin key)
    or the gateway rejects the request.
    """
    settings = get_settings()
    base = settings.llm_gateway_base_url.rstrip("/")
    admin_key = settings.llm_gateway_admin_key
    if not admin_key:
        raise LiteLLMAdminError(
            "LLM_GATEWAY_ADMIN_KEY is not configured; cannot mint virtual keys"
        )

    body: dict = {"user_id": customer_id, "models": list(models)}
    if max_budget is not None:
        body["max_budget"] = max_budget
    if budget_duration is not None:
        # LiteLLM resets the key's spend against max_budget every duration
        # (e.g. "30d", "1mo"). Omit ⇒ max_budget is a one-time cumulative cap.
        body["budget_duration"] = budget_duration
    if rpm_limit is not None:
        body["rpm_limit"] = rpm_limit
    if tpm_limit is not None:
        body["tpm_limit"] = tpm_limit
    if metadata:
        body["metadata"] = metadata

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base}/key/generate",
                headers={
                    "Authorization": f"Bearer {admin_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.HTTPError as e:
        # Don't surface the exception repr (could include the URL w/ a token).
        raise LiteLLMAdminError(
            f"gateway transport error: {type(e).__name__}"
        ) from None

    if resp.status_code // 100 != 2:
        # Status only — never the body (it can echo key material).
        raise LiteLLMAdminError(
            f"gateway /key/generate returned HTTP {resp.status_code}"
        )
    try:
        key = resp.json().get("key")
    except Exception:
        raise LiteLLMAdminError("gateway /key/generate returned non-JSON") from None
    if not key:
        raise LiteLLMAdminError("gateway /key/generate response missing 'key'")
    _log.info("minted virtual key for customer_id=%s (budget=%s)", customer_id, max_budget)
    return key
