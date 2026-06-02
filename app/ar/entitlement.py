"""Subscription-entitlement gate for the cross-app (PO-originated) invoice path.

Principle (from the AR/Invoicing brief):
  * STANDALONE AO invoicing is NEVER gated. A workspace that pays for Accounting-
    Ops can invoice freely with no PO. ``assert_entitled_for_invoice`` is simply
    not called on that path.
  * The PROJECT-OPS cross-app path (an invoice created FROM a PO) is gated: the
    workspace's subscription must include BOTH Project-Ops AND Accounting-Ops.
    This is checked against the SHARED subscription claims (Lago/Keycloak) that
    travel on the caller's token — NOT a hardcoded workspace allowlist.

Where the claim comes from (and the Lago wiring seam)
=====================================================
The authoritative product/entitlement signal is a claim on the caller's bearer.
Keycloak (uchub realm) can map a Lago-synced subscription into the token; Brigade
federates the same claim when it cross-calls. We read it from
``AuthContext.claims`` so the moment Keycloak/Lago starts emitting it, the gate
goes live with NO code change here.

We accept any of these claim shapes (first match wins) so we're forgiving of how
the claim eventually lands:

  1. ``entitlements``: a list/space-string of product codes, e.g.
     ``["accounting-ops", "project-ops"]`` or ``"accounting-ops project-ops"``.
  2. ``products``: same idea under a different key.
  3. ``subscription.products`` (nested): ``{"subscription": {"products": [...]}}``.
  4. ``resource_access.<client>.roles`` style is NOT used here (those are RBAC
     roles, handled by ``require_role``) — entitlements are product SKUs.

MVP behavior / escape hatch
===========================
Until Lago emits the claim, an env flag controls the default so we don't block a
legitimately-entitled workspace before the billing integration is live:

  ``AO_CROSS_APP_ENTITLEMENT_MODE`` (default ``"claim"``):
    - ``"claim"``  : enforce strictly against the token claim (production target).
    - ``"open"``   : allow the cross-app path for any authenticated caller
                     (pre-Lago bootstrap — documented, opt-in, logs a warning).

This is a thin, well-documented stub: swap the ``_read_entitlements`` body for a
real Lago lookup later and every caller keeps working.
"""
from __future__ import annotations

import logging
import os
from typing import Any

_log = logging.getLogger("acctops.ar.entitlement")

# Canonical product codes. Kept here so the cross-app caller + any future Lago
# sync agree on the SKU strings.
PRODUCT_ACCOUNTING_OPS = "accounting-ops"
PRODUCT_PROJECT_OPS = "project-ops"

# Accept a few common spellings/aliases the upstream claim might use.
_ALIASES: dict[str, str] = {
    "accounting-ops": PRODUCT_ACCOUNTING_OPS,
    "accounting_ops": PRODUCT_ACCOUNTING_OPS,
    "accountingops": PRODUCT_ACCOUNTING_OPS,
    "ao": PRODUCT_ACCOUNTING_OPS,
    "project-ops": PRODUCT_PROJECT_OPS,
    "project_ops": PRODUCT_PROJECT_OPS,
    "projectops": PRODUCT_PROJECT_OPS,
    "po": PRODUCT_PROJECT_OPS,
}


class EntitlementError(PermissionError):
    """Raised when the cross-app invoice path is used without the required
    subscription. Subclasses ``PermissionError`` so the MCP/HTTP layers translate
    it into a clean 403/tool-error exactly like an authz denial."""


def _normalize(code: str) -> str:
    key = (code or "").strip().lower()
    return _ALIASES.get(key, key)


def _coerce_codes(value: Any) -> set[str]:
    """Coerce a claim value (list OR space/comma string) into a normalized set."""
    out: set[str] = set()
    if value is None:
        return out
    if isinstance(value, str):
        # space- or comma-separated
        parts = value.replace(",", " ").split()
        for p in parts:
            out.add(_normalize(p))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, str):
                out.add(_normalize(item))
            elif isinstance(item, dict):
                # e.g. {"code": "project-ops", "active": true}
                code = item.get("code") or item.get("product") or item.get("sku")
                active = item.get("active", True)
                if code and active:
                    out.add(_normalize(str(code)))
    return out


def _read_entitlements(claims: dict[str, Any] | None) -> set[str]:
    """Extract the set of product codes the caller's subscription includes.

    THIS is the single Lago/Keycloak seam — replace the body with a real billing
    lookup later; callers don't change. Today it reads the token claim.
    """
    if not claims:
        return set()
    # 1. flat "entitlements"
    codes = _coerce_codes(claims.get("entitlements"))
    if codes:
        return codes
    # 2. flat "products"
    codes = _coerce_codes(claims.get("products"))
    if codes:
        return codes
    # 3. nested subscription.products
    sub = claims.get("subscription")
    if isinstance(sub, dict):
        codes = _coerce_codes(sub.get("products") or sub.get("entitlements"))
        if codes:
            return codes
    return set()


def _mode() -> str:
    return (os.getenv("AO_CROSS_APP_ENTITLEMENT_MODE") or "claim").strip().lower()


def workspace_has_products(
    claims: dict[str, Any] | None, *required: str
) -> bool:
    """Return True if the caller's subscription includes every ``required`` product.

    Pure (no I/O) so it's trivially unit-testable. ``required`` are canonical
    product codes (use the PRODUCT_* constants).
    """
    have = _read_entitlements(claims)
    want = {_normalize(r) for r in required}
    return want.issubset(have)


def assert_entitled_for_invoice(
    claims: dict[str, Any] | None,
    *,
    external_source: str,
    workspace_id: Any = None,
) -> None:
    """Gate the cross-app (PO-originated) invoice-create path.

    Standalone AO invoicing (``external_source`` not the cross-app PO origin) is
    NOT gated and returns immediately. For the cross-app path the workspace's
    subscription must include BOTH Project-Ops and Accounting-Ops.

    Args:
        claims: the caller's token claims (``AuthContext.claims``). The shared
            subscription entitlement claim is read from here.
        external_source: the invoice's origin label. The Project-Ops cross-app
            path uses ``"project-ops"``; anything else is treated as standalone
            and is NOT gated.
        workspace_id: optional, for log context only.

    Raises:
        EntitlementError: cross-app path without BOTH products on the subscription.
    """
    # Only the Project-Ops cross-app origin is gated. Standalone (manual, stripe,
    # etc.) AO invoicing is a paid-for native capability and must never be gated.
    if _normalize(external_source) != PRODUCT_PROJECT_OPS:
        return

    mode = _mode()
    if mode == "open":
        # Pre-Lago bootstrap: allow but log loudly so operators can see the gate
        # is running open until the billing claim is wired up.
        _log.warning(
            "AR cross-app entitlement gate is OPEN (AO_CROSS_APP_ENTITLEMENT_MODE=open) "
            "— allowing PO-originated invoice for workspace=%s WITHOUT a subscription "
            "claim check. Set the mode to 'claim' once Lago emits entitlements.",
            workspace_id,
        )
        return

    if not workspace_has_products(claims, PRODUCT_PROJECT_OPS, PRODUCT_ACCOUNTING_OPS):
        raise EntitlementError(
            "Creating an invoice from a Project-Ops PO requires a subscription that "
            "includes BOTH Project-Ops and Accounting-Ops. The caller's token does "
            "not carry both entitlements. (Standalone Accounting-Ops invoicing is "
            "not gated — omit the project-ops origin to invoice directly.)"
        )


__all__ = (
    "EntitlementError",
    "PRODUCT_ACCOUNTING_OPS",
    "PRODUCT_PROJECT_OPS",
    "assert_entitled_for_invoice",
    "workspace_has_products",
)
