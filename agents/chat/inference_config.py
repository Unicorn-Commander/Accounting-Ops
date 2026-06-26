"""Dedicated inference-config resolver for the CHAT / CFO / specialist path.

Returns the (base_url, api_key, model) triple for one chat turn, honoring:

  KEY precedence:    per-workspace BYOK  >  platform / UC-credit virtual key
                     >  OpenRouter (today's behavior)
  MODEL precedence:  requested (body.model)  >  persona gateway alias (only when
                     routed through the gateway)  >  today's default

DEFAULT-SAFETY INVARIANT
========================
When ``route_chat_through_gateway`` is OFF (the production default) AND the
workspace has no BYOK / virtual key, this returns the EXACT triple the chat
runner computes on its own today:
    base_url = OPENROUTER_BASE_URL  (or the openrouter.ai default)
    api_key  = OPENROUTER_API_KEY
    model    = requested_model or CHAT_DEFAULT_MODEL (or the sonnet-4.5 default)
So wiring this into chat.py is indistinguishable from passing nothing — live
chat is unchanged until an operator sets AO_ROUTE_CHAT_THROUGH_GATEWAY=1 (or a
workspace stores a key).

This module DELIBERATELY never reads ``LLM_API_KEY`` — that is the LOCAL
categorizer's key (used with LLM_ENDPOINT_URL against the local Qwen) and it
takes precedence in agents/_shared/api_keys.py::resolve_llm_api_key. Reusing it
here would entangle the two paths. A unit test source-greps this file to enforce
the rule permanently.

FAIL-SAFE: any exception in the BYOK / virtual-key reads is caught and the
resolver falls through to the OpenRouter-default branch — a gateway/DB hiccup
can never break live chat.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.config import get_settings

_log = logging.getLogger("acctops.chat.inference_config")

# Mirror agents/chat/runner.py's current module defaults so the OFF path is
# byte-identical. (runner.py: DEFAULT_OPENROUTER_URL, DEFAULT_MODEL.)
_OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-4.5"


@dataclass(frozen=True)
class ResolvedInference:
    base_url: str
    api_key: str
    model: str
    # "byok" | "platform_virtual" | "openrouter_default" — for logging/tests.
    # NEVER carries key material.
    source: str


def _openrouter_base() -> str:
    return os.environ.get("OPENROUTER_BASE_URL", _OPENROUTER_DEFAULT_BASE_URL)


def _default_model() -> str:
    return os.environ.get("CHAT_DEFAULT_MODEL", _OPENROUTER_DEFAULT_MODEL)


def _is_hard_persona(persona) -> bool:
    """True for the deep-reasoning personas (CFO + tax specialists/researchers).

    Keyed on the persona ``tier`` ("strategic" | "specialist" = hard;
    "operational" = routine, e.g. setup / product-guide). Tolerant of a missing
    tier / None persona → not hard (routine)."""
    return getattr(persona, "tier", None) in ("strategic", "specialist")


def _gateway_model_for(persona, is_consult: bool) -> str:
    """Gateway alias for this turn: consults stay on the cheap Sonnet tier; else
    the persona's alias (CFO → opus); Sonnet fallback."""
    from agents.chat.persona_registry import GATEWAY_MODEL_SONNET

    if is_consult:
        return GATEWAY_MODEL_SONNET
    alias = getattr(persona, "chat_model", None) if persona is not None else None
    return alias or GATEWAY_MODEL_SONNET


async def resolve_chat_inference(
    *,
    workspace_id: str,
    persona=None,
    requested_model: str | None = None,
    is_consult: bool = False,
    claims: dict[str, Any] | None = None,
) -> ResolvedInference:
    """Resolve (base_url, api_key, model) for a chat/agent turn. Never raises."""
    settings = get_settings()
    default_model = _default_model()
    gw_model = _gateway_model_for(persona, is_consult)

    # Cloud-on-demand: a turn "wants cloud" when it explicitly names a model that
    # is NOT the local one. On a local-default instance this is the ONLY thing
    # that leaves local — routine turns (no model, or the local model) stay on the
    # free local qwen; an explicit frontier model escalates to the cloud tiers
    # (BYOK → gateway → OpenRouter). Also fixes a latent bug: without this, an
    # explicit cloud model on a local-preferring workspace was sent to the local
    # router (404) or silently ignored.
    local_available = bool(settings.chat_local_base_url)
    wants_cloud = bool(requested_model) and requested_model != settings.chat_local_model
    # Persona auto-escalation: when enabled, a "hard" persona (CFO + tax
    # specialists/researchers, tier strategic|specialist) escalates to cloud even
    # without an explicit model — routine/operational personas stay local. The
    # cloud tiers below then pick the right model per backend (the persona's
    # gateway alias on the gateway, the OpenRouter default on a BYOK-direct key).
    if settings.chat_escalate_hard_personas and _is_hard_persona(persona):
        wants_cloud = True

    # ---- 0) INSTANCE-LOCAL FAST PATH -------------------------------------
    # A local-default box answers routine turns locally even when the workspace
    # has a BYOK key: BYOK becomes the ON-DEMAND cloud target (tier 1 below), not
    # the default. Gated entirely on chat_local_default, so a normal BYOK customer
    # (flag off) is unaffected — BYOK still always wins for them.
    if settings.chat_local_default and local_available and not wants_cloud:
        return ResolvedInference(
            settings.chat_local_base_url,
            settings.chat_local_api_key or "local",
            requested_model or settings.chat_local_model,
            "local",
        )

    # ---- 1) BYOK — highest precedence (the on-demand cloud key) ----------
    byok = None
    try:
        from app.security.workspace_llm_keys import get_byok

        byok = await get_byok(workspace_id)
    except Exception as e:  # noqa: BLE001 — fail-safe
        _log.warning("BYOK lookup failed (ws=%s): %s", workspace_id, e)
        byok = None
    if byok:
        key, _provider, byok_base = byok
        if byok_base:
            # Customer pinned their own endpoint → direct; can't assume the
            # gateway aliases exist there, so use requested/today's model.
            return ResolvedInference(byok_base, key, requested_model or default_model, "byok")
        if settings.route_chat_through_gateway:
            # No explicit endpoint → route their key through the gateway so it's
            # still metered/observable; use the gateway alias.
            return ResolvedInference(
                settings.llm_gateway_base_url, key, requested_model or gw_model, "byok"
            )
        # Gateway off → honor their key against the OpenRouter base + today's model.
        return ResolvedInference(_openrouter_base(), key, requested_model or default_model, "byok")

    # ---- 1.5) LOCAL-first — sovereign qwen --------------------------------
    # After BYOK (a customer's explicit endpoint always wins) but ahead of the
    # platform/OpenRouter tiers. Gated on a configured local endpoint AND either
    # the INSTANCE-WIDE chat_local_default (a fully self-hosted, local-only box)
    # OR the per-workspace EngagementPolicy.prefer_local_inference flag. With both
    # off it is a no-op (preserves the default-safety invariant). When the
    # instance default is on we skip the per-workspace DB read entirely — faster
    # and robust even if the policy store is unavailable. Fail-safe: any error
    # reading the flag falls through to the cloud tiers.
    # (Skipped when wants_cloud: an explicit non-local model escalates past local
    # to the cloud tiers — the instance fast path above already handled the
    # chat_local_default + routine case.)
    if local_available and not wants_cloud:
        prefer_local = settings.chat_local_default
        if not prefer_local:
            try:
                from app.engagement.policy import get_prefer_local

                prefer_local = await get_prefer_local(workspace_id)
            except Exception as e:  # noqa: BLE001 — fail-safe
                _log.warning("prefer_local lookup failed (ws=%s): %s", workspace_id, e)
                prefer_local = False
        if prefer_local:
            return ResolvedInference(
                settings.chat_local_base_url,
                settings.chat_local_api_key or "local",
                requested_model or settings.chat_local_model,
                "local",
            )

    # ---- 2) platform / UC-credit virtual key (only when routing ON) ------
    if settings.route_chat_through_gateway:
        platform_key = None
        try:
            from app.security.workspace_llm_keys import get_or_mint_virtual_key

            platform_key = await get_or_mint_virtual_key(workspace_id, claims=claims)
        except Exception as e:  # noqa: BLE001 — fail-safe
            _log.warning("virtual-key resolution failed (ws=%s): %s", workspace_id, e)
            platform_key = None
        platform_key = platform_key or (settings.llm_platform_virtual_key or None)
        if platform_key:
            return ResolvedInference(
                settings.llm_gateway_base_url,
                platform_key,
                requested_model or gw_model,
                "platform_virtual",
            )
        # Routing on but no key available → fall through (default-safe).

    # ---- 3) DEFAULT-SAFE: today's OpenRouter-direct behavior -------------
    return ResolvedInference(
        _openrouter_base(),
        os.environ.get("OPENROUTER_API_KEY", ""),
        requested_model or default_model,
        "openrouter_default",
    )
