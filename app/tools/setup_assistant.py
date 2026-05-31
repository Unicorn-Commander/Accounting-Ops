"""Setup-Assistant-specific MCP tools.

These tools are exposed only to the ``setup-assistant`` chat persona. They
power the conversational provisioning flow:

  - ``suggest_connectors``   — recommend bank/processor/payroll connectors
                               based on entity kind + industry.
  - ``mark_intake_complete`` — signal that the assistant has finished the
                               intake; the UI listens for this tool call to
                               redirect from /setup -> /dashboard.
  - ``create_workspace``     — provision a brand-new workspace. Admin-only.
                               Used when Aaron (or any admin) is setting up a
                               second/third workspace via chat.

These are intentionally small, focused tools. The heavy lifting
(``create_entity_with_coa``) lives in its own module.
"""
from __future__ import annotations

import re
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth


# ---------------------------------------------------------------------------
# suggest_connectors — entity-kind + industry → connector suggestions
# ---------------------------------------------------------------------------

_CONNECTOR_PROFILES: dict[str, dict[str, Any]] = {
    "tech_startup_c_corp": {
        "connectors": ["simplefin_bank", "stripe", "github_enterprise"],
        "invites": ["mosin_cpa"],
        "notes": "Tech C-Corp: SimpleFIN for bank, Stripe for revenue, GitHub for code → R&D documentation. Invite Mosin as CPA.",
    },
    "tech_startup_s_corp": {
        "connectors": ["simplefin_bank", "stripe", "gusto_payroll"],
        "invites": ["mosin_cpa"],
        "notes": "S-Corp owner-operators NEED payroll (Gusto/ADP) to satisfy reasonable-compensation rules.",
    },
    "consulting_services": {
        "connectors": ["simplefin_bank", "stripe"],
        "invites": ["mosin_cpa"],
        "notes": "Services business: bank + Stripe (if accepting card payments). 1099 tracker built-in.",
    },
    "ecommerce": {
        "connectors": ["simplefin_bank", "shopify", "stripe", "amazon_seller"],
        "invites": ["mosin_cpa"],
        "notes": "E-commerce: bank + commerce platform + processor. Sales tax tracking is critical — Avalara/TaxJar later.",
    },
    "brick_and_mortar_retail": {
        "connectors": ["simplefin_bank", "square"],
        "invites": ["mosin_cpa"],
        "notes": "Brick-and-mortar retail: bank + Square POS. Inventory tracking required for accrual basis.",
    },
    "personal": {
        "connectors": ["simplefin_bank", "simplefin_brokerage"],
        "invites": [],
        "notes": "Personal: bank + brokerage. Plaid Income for W-2 wages (later). No CPA invite by default.",
    },
    "nonprofit": {
        "connectors": ["simplefin_bank", "stripe"],
        "invites": ["mosin_cpa", "audit_committee_chair"],
        "notes": "Nonprofit: bank + Stripe for donations. Strongly recommend Mosin + a board/audit member as viewer.",
    },
    "real_estate_holding": {
        "connectors": ["simplefin_bank"],
        "invites": ["mosin_cpa", "property_manager"],
        "notes": "Real estate LLC: bank only typically; rent flows in/out via management. Add property manager as viewer.",
    },
    "default": {
        "connectors": ["simplefin_bank"],
        "invites": ["mosin_cpa"],
        "notes": "Generic profile: connect a bank, invite a CPA. Add a payment processor if you accept card payments.",
    },
}


def _connector_key(kind: str, industry: str | None) -> str:
    """Pick the connector profile key from entity kind + industry."""
    k = (kind or "").strip().lower()
    i = (industry or "").strip().lower()
    if i in ("tech_saas", "ai_infrastructure"):
        if k == "c_corp":
            return "tech_startup_c_corp"
        if k == "s_corp":
            return "tech_startup_s_corp"
    if i in ("consulting_services", "professional_services"):
        return "consulting_services"
    if i == "ecommerce":
        return "ecommerce"
    if i in ("trades_construction", "brick_and_mortar"):
        return "brick_and_mortar_retail"
    if k in ("personal", "personal_1040"):
        return "personal"
    if k in ("nonprofit", "non_profit"):
        return "nonprofit"
    if i == "real_estate":
        return "real_estate_holding"
    return "default"


async def suggest_connectors(
    workspace_id: UUID,
    entity_id: UUID,
    kind: str,
    industry: str | None = None,
) -> dict[str, Any]:
    """Suggest data connectors + invitations for a freshly-provisioned entity.

    Read-only — does not configure anything. The agent uses the result to
    walk the user through /connectors and /members in plain English.

    Args:
        workspace_id: workspace UUID.
        entity_id: entity UUID.
        kind: entity kind (c_corp, s_corp, llc, etc).
        industry: optional industry tag (tech_saas, consulting_services,
            ecommerce, real_estate, etc).

    Returns:
        {
          "connectors": ["simplefin_bank", "stripe", ...],
          "invites":    ["mosin_cpa"],
          "profile_key": "tech_startup_c_corp",
          "notes": "..."
        }

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    key = _connector_key(kind, industry)
    profile = _CONNECTOR_PROFILES.get(key, _CONNECTOR_PROFILES["default"])
    return {
        "profile_key": key,
        "connectors": list(profile["connectors"]),
        "invites": list(profile["invites"]),
        "notes": profile["notes"],
    }


# ---------------------------------------------------------------------------
# mark_intake_complete — signal the UI to leave /setup
# ---------------------------------------------------------------------------

async def mark_intake_complete(
    workspace_id: UUID,
    entity_id: UUID,
    summary: str,
    next_steps: list[str] | None = None,
) -> dict[str, Any]:
    """Mark intake complete; the /setup page redirects to /dashboard on this call.

    This tool intentionally only RECORDS the completion event (decision_audit)
    so the conversation history shows a clean handoff. The web UI listens for
    a tool call named ``mark_intake_complete`` in the SSE stream and does the
    redirect client-side.

    Args:
        workspace_id: workspace UUID.
        entity_id: the entity that was just provisioned.
        summary: 1-2 sentence recap shown to the user.
        next_steps: optional list of recommended next actions
            (e.g. ["Connect SimpleFIN", "Invite Mosin"]).

    Returns: ``{"status": "complete", "summary": ..., "next_steps": [...]}``.

    Role floor: viewer (this is purely a signal, not a state change).
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    return {
        "status": "complete",
        "summary": summary,
        "next_steps": next_steps or [],
        "redirect_to": "/dashboard",
    }


# ---------------------------------------------------------------------------
# create_workspace — admin-only provisioning via chat
# ---------------------------------------------------------------------------


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:96] or "workspace"


async def create_workspace(
    display_name: str,
    slug: str | None = None,
    hipaa_protected: bool = False,
    isolation_tier: str = "shared",
) -> dict[str, Any]:
    """Provision a brand-new workspace from the chat surface.

    ADMIN-ONLY. Only platform_admin / owner-level accounts on any existing
    workspace can call this. The caller is automatically added as ``owner``
    of the new workspace.

    NOTE: This tool wraps ``scripts.provision_workspace.provision_workspace``
    which runs ``CREATE SCHEMA`` + ``alembic upgrade`` + workspace registry
    INSERT under the admin DB role. Provisioning takes 5-15 seconds.

    Args:
        display_name: Human-readable name (e.g. "Aaron + Shafen Shared").
        slug: Optional URL slug. Auto-derived from display_name.
        hipaa_protected: Mark this workspace as HIPAA-isolated.
        isolation_tier: One of "shared" | "premium" | "enterprise".

    Returns:
        ``{"workspace_id": "<uuid>", "slug": "...", "display_name": "...",
           "schema": "...", "status": "active"}``

    Role floor: owner on any workspace OR platform_admin.
    """
    auth = current_auth()
    if auth is None:
        raise PermissionError("No authenticated caller for this MCP request")

    # Verify the caller has ANY owner+ role somewhere
    from app.api.workspaces import _ensure_user_account
    user_id = await _ensure_user_account(auth.uc_uid)
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        """
                        SELECT 1 FROM public.workspace_membership
                        WHERE user_id = :user_id
                          AND role IN ('owner', 'client_owner', 'platform_admin', 'admin')
                        LIMIT 1
                        """
                    ),
                    {"user_id": user_id},
                )
            ).first()
            if not row:
                raise PermissionError(
                    "create_workspace requires admin/owner on at least one "
                    "existing workspace"
                )

    # Provision
    from scripts.provision_workspace import provision_workspace
    wid = uuid.uuid4()
    final_slug = (slug or _slugify(display_name))
    schema, _ = await provision_workspace(
        engine=None,
        workspace_id=wid,
        display_name=display_name,
        slug=final_slug,
        hipaa_protected=hipaa_protected,
        isolation_tier=isolation_tier,
        entities=None,
    )

    # Add caller as owner
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO public.workspace_membership
                        (id, user_id, workspace_id, role, status, created_at)
                    VALUES (gen_random_uuid(), :user_id, :wid, 'owner', 'active', now())
                    ON CONFLICT ON CONSTRAINT uq_workspace_membership DO UPDATE
                    SET role = 'owner', status = 'active'
                    """
                ),
                {"user_id": user_id, "wid": str(wid)},
            )

    return {
        "workspace_id": str(wid),
        "slug": final_slug,
        "display_name": display_name,
        "schema": schema,
        "status": "active",
        "isolation_tier": isolation_tier,
        "hipaa_protected": hipaa_protected,
    }
