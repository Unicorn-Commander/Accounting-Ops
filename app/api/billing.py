"""Billing & entitlement STATUS for a workspace (read-only).

Backs the web "Billing & Credits" surface. This endpoint answers a single
question the UI needs: *may this workspace run paid "smart" AI, and if not, how
does it fix that?* It reads the same three signals the smart-inference gate
reads (`app/billing/inference_gate.py`) — edition, entitlement claim, BYOK — and
returns them plus the Ops-Center deep-link. It NEVER mutates anything and NEVER
touches Lago/Stripe (Ops-Center owns the wallet; per-call spend is the gateway's
`max_budget`). See docs/INFERENCE-AND-BILLING.md.

Community-safe: the only enterprise-hold dependency (`app.ar.entitlement`) is
imported lazily inside an `if not settings.is_community` guard — mirroring
`app/api/invoices.py` — so Community boot never hard-imports it. BYOK status uses
`workspace_llm_keys.get_byok`, which is the community half.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends

from app.config import get_settings
from app.tenancy import WorkspaceContext, require_role, resolve_workspace

_log = logging.getLogger("acctops.api.billing")

router = APIRouter(tags=["billing"])


@router.get("/api/workspaces/{workspace_id}/billing")
async def get_billing_status(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Report whether this workspace can run paid smart inference + how to enable it.

    Shape (stable contract with web/lib/billing.ts):
        {
          "edition": "community" | "enterprise",
          "gate_enabled": bool,   # the smart-inference gate is actually enforcing
          "entitled": bool,       # has the accounting-ops entitlement claim
          "byok": bool,           # a workspace model key is configured
          "can_use_smart": bool,  # entitled OR byok OR gate-off OR community
          "upgrade_url": str | null,   # Ops-Center billing deep-link (enterprise only)
          "byok_setup_url": str        # in-app BYOK page
        }

    Role floor: viewer. Never raises on the entitlement/BYOK sub-lookups — a
    status endpoint degrades to the safe (most-restrictive) value instead.
    """
    require_role(workspace, "viewer")
    s = get_settings()
    wid = str(workspace.workspace_id)

    # BYOK — community-safe read (the mint half stays out of the import graph).
    byok = False
    try:
        from app.security.workspace_llm_keys import get_byok

        byok = (await get_byok(wid)) is not None
    except Exception:  # noqa: BLE001 — status must not 500 on a credential-store hiccup
        _log.debug("BYOK status lookup failed for workspace=%s", wid, exc_info=True)

    # gate_active(): the gate only enforces when enabled AND not community. Inlined
    # so this route never imports app.billing (enterprise-hold) in community.
    gate_enabled = bool(s.smart_gate_enabled and not s.is_community)

    # Entitlement — enterprise-only concept (cross-app claim). Lazy + edition-gated
    # exactly like app/api/invoices.py so community never imports app.ar.entitlement.
    entitled = False
    if not s.is_community:
        try:
            from app.ar.entitlement import (
                PRODUCT_ACCOUNTING_OPS,
                workspace_has_products,
            )
            from app.mcp_auth import current_auth

            auth = current_auth()
            claims = auth.claims if auth is not None else None
            entitled = bool(workspace_has_products(claims, PRODUCT_ACCOUNTING_OPS))
        except Exception:  # noqa: BLE001 — degrade to not-entitled, never 500
            _log.debug("entitlement status lookup failed for workspace=%s", wid, exc_info=True)

    # Access is allowed when the gate isn't enforcing, or the workspace is
    # entitled, or it brought its own key. (BYOK only bypasses when the deployment
    # honors it; mirror that so the UI never over-promises.)
    byok_bypasses = byok and s.smart_gate_byok_bypass
    can_use_smart = (not gate_enabled) or entitled or byok_bypasses

    return {
        "edition": "community" if s.is_community else "enterprise",
        "gate_enabled": gate_enabled,
        "entitled": entitled,
        "byok": byok,
        "can_use_smart": can_use_smart,
        "upgrade_url": (s.ops_center_billing_url or None) if not s.is_community else None,
        "byok_setup_url": "/settings/inference",
    }
