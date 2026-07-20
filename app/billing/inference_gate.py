"""Credit/entitlement gate for AO's *smart* (paid, cloud / 3rd-party) inference.

What this gates
===============
Only the PAID cloud "smart" functions call this: the CFO Agent (Opus), the tax
specialists, CFO→specialist delegation, and premium chat models. The free,
deterministic accounting brain — Tier-1 vendor-rule categorization, Sentinel, and
every journal/report/compute path (P&L, balance sheet, journal posting, chain
verify, tax_compute_1040) — NEVER calls this and is never gated. Local-model
inference ($0) is likewise not hard-gated.

The rule (what the maintainer asked for)
========================================
A workspace may run a smart function iff it either:

  * supplies its **own** model key (BYOK) — it pays its own provider, so no
    credits are required (``smart_gate_byok_bypass``, default on); OR
  * is **entitled** to Accounting-Ops (the ``accounting-ops`` SKU on the
    caller's token claim — the same signal ``app/ar/entitlement.py`` reads).

Otherwise the smart function is **blocked** with an upgrade CTA. Per-call spend
(credits/budget) is NOT enforced here — that is the LiteLLM gateway's job (the
per-workspace virtual key's ``max_budget`` + Lago metering). This module decides
*access*, the gateway decides *spend*. Keeping the two separate is deliberate:
AO must never fork the Ops-Center wallet.

Scope / safety
==============
Inert by default. It only enforces when ALL of:
  * ``AO_EDITION`` != community (Community self-host is BYO-model — never gated), and
  * ``AO_SMART_GATE_ENABLED=1`` (off on the dogfood ring; on for hosted prod).

So enabling it is a single prod-only env flag; the dogfood ring and the
open-source Community build are completely unaffected.
"""
from __future__ import annotations

import logging

from app.ar.entitlement import (
    PRODUCT_ACCOUNTING_OPS,
    EntitlementError,
    workspace_has_products,
)
from app.config import get_settings

_log = logging.getLogger("acctops.billing.inference_gate")


class SmartInferenceGateError(EntitlementError):
    """Raised when a paid smart function is invoked by a workspace that is
    neither entitled nor BYOK.

    Subclasses :class:`EntitlementError` (→ ``PermissionError``) so any handler
    that doesn't special-case it still fails **closed** with a clean 403. The
    HTTP/MCP layer SHOULD special-case it to return **402 Payment Required** with
    the ``upgrade_url`` so the UI can render a "get credits / bring your own key"
    call to action.
    """

    #: HTTP status a dedicated handler should map this to (semantically "buy credits").
    http_status = 402

    def __init__(
        self,
        message: str,
        *,
        upgrade_url: str | None = None,
        byok_available: bool = True,
    ) -> None:
        super().__init__(message)
        self.upgrade_url = upgrade_url
        self.byok_available = byok_available


def gate_active() -> bool:
    """True when the gate would actually enforce (hosted + enabled, not community)."""
    s = get_settings()
    return s.smart_gate_enabled and not s.is_community


async def check_smart_inference_access(
    *,
    workspace_id: str,
    claims: dict | None,
    what: str = "This premium AI agent",
) -> None:
    """Authorize a paid/cloud smart-inference invocation for ``workspace_id``.

    Returns ``None`` when access is allowed; raises :class:`SmartInferenceGateError`
    when the workspace must add credits or a BYOK key first.

    Order (fail-closed): community/flag-off → allow · BYOK present → allow ·
    entitled to accounting-ops → allow · else → raise.

    Args:
        workspace_id: the workspace whose access is being checked.
        claims: the caller's token claims (``AuthContext.claims``); the
            ``accounting-ops`` entitlement SKU is read from here.
        what: human label for the blocked capability, used in the error message.
    """
    s = get_settings()

    # 1. Community self-host is BYO-model — never gated. And the gate is off by
    #    default (dogfood ring). Either ⇒ allow, with zero I/O.
    if s.is_community or not s.smart_gate_enabled:
        return

    # 2. BYOK bypass — a workspace that brought its own key pays its own provider,
    #    so it needs no credits. Import lazily (community-safe) and fail toward the
    #    entitlement check on any lookup error — never fail OPEN.
    if s.smart_gate_byok_bypass:
        try:
            from app.security.workspace_llm_keys import get_byok

            if await get_byok(workspace_id) is not None:
                return
        except Exception:  # noqa: BLE001 — degrade to the entitlement check, not open
            _log.debug(
                "BYOK lookup failed for workspace=%s; falling through to the "
                "entitlement check (not failing open).",
                workspace_id,
            )

    # 3. Entitled to Accounting-Ops (token claim) ⇒ allow.
    if workspace_has_products(claims, PRODUCT_ACCOUNTING_OPS):
        return

    # 4. Otherwise block with an upgrade CTA.
    _log.info(
        "smart-inference gate BLOCKED workspace=%s (no entitlement, no BYOK)",
        workspace_id,
    )
    raise SmartInferenceGateError(
        f"{what} needs Accounting-Ops credits. Add credits to continue, or bring "
        f"your own model key (BYOK) to run it on your own account. Deterministic "
        f"bookkeeping and reports remain available for free.",
        upgrade_url=s.ops_center_billing_url or None,
        byok_available=True,
    )


__all__ = (
    "SmartInferenceGateError",
    "check_smart_inference_access",
    "gate_active",
)
