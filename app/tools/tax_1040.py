"""MCP tool: tax_compute_1040 — compute a client's Form 1040 and store it.

The CALCULATOR path: given a client's income figures (each tagged with where it
came from — a W-2/1099 document or an explicit entry), run the vendored
deterministic engine and store a DRAFT 1040 WorkProduct whose AGI / taxable
income / tax / total tax are COMPUTED (not typed, not fabricated). The filled PDF
is re-rendered on demand from the stored computed lines.

Distinct from ``tax_prepare_filing`` (which fills figures the caller already has)
and ``tax_fill_w4p`` (pension withholding). Role floor: accountant.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.tax.work_product_service import build_1040_computed, insert_work_product
from app.workspace_session import workspace_session


async def tax_compute_1040(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    filing_status: str,
    wages: float | int = 0,
    taxable_interest: float | int = 0,
    ordinary_dividends: float | int = 0,
    qualified_dividends: float | int = 0,
    ira_taxable: float | int = 0,
    pensions_taxable: float | int = 0,
    social_security_taxable: float | int = 0,
    capital_gain_loss: float | int = 0,
    other_income: float | int = 0,
    adjustments: float | int = 0,
    itemized_deduction: float | int = 0,
    qbi_deduction: float | int = 0,
    nonrefundable_credits: float | int = 0,
    withholding: float | int = 0,
    estimated_payments: float | int = 0,
    income_source: str = "client documents",
) -> dict[str, Any]:
    """Compute a Form 1040 from the client's income figures and store a DRAFT.

    Args:
        workspace_id, entity_id: scope (the entity is the taxpayer/client).
        tax_year: 2025 or 2026 (raises on an unsupported year).
        filing_status: single|mfj|mfs|hoh|qss.
        wages..estimated_payments: WHOLE-DOLLAR income/deduction/payment figures.
            Provide only what the client has; omitted = 0 (not fabricated).
        income_source: a short provenance label for the supplied figures (e.g.
            "W-2 + 1099-INT", a document set) — recorded as each line's source_ref.

    Returns:
        {"ok": True, "work_product_id", "kind": "1040", "tax_year", "status":
         "draft", "download_path", "computed": {agi,taxable_income,total_tax,
         balance_due,...}, "assumptions", "_draft_notice"}.

    The computed lines (AGI/taxable income/tax/total tax) come from the vendored
    deterministic engine (Rev. Proc. 2024-40 / 2025-32 / OBBBA). DRAFT until
    reviewed or self-certified. Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="bookkeeper")  # noqa: F841

    built = build_1040_computed(
        tax_year,
        filing_status,
        {
            "wages": wages,
            "taxable_interest": taxable_interest,
            "ordinary_dividends": ordinary_dividends,
            "qualified_dividends": qualified_dividends,
            "ira_taxable": ira_taxable,
            "pensions_taxable": pensions_taxable,
            "social_security_taxable": social_security_taxable,
            "capital_gain_loss": capital_gain_loss,
            "other_income": other_income,
            "adjustments": adjustments,
            "itemized_deduction": itemized_deduction,
            "qbi_deduction": qbi_deduction,
            "nonrefundable_credits": nonrefundable_credits,
            "withholding": withholding,
            "estimated_payments": estimated_payments,
        },
        income_source=income_source,
    )  # raises on bad year/status

    auth = current_auth()
    creator = (auth.agent_id or auth.uc_uid) if auth else None

    async with workspace_session(SessionLocal, workspace_id) as session:
        wp_id = await insert_work_product(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            kind="1040",
            tax_year=tax_year,
            title=built["title"],
            inputs=built["inputs"],
            provenance=built["provenance"],
            creator=creator,
        )
    c = built["computed"]
    return {
        "ok": True,
        "work_product_id": wp_id,
        "kind": "1040",
        "tax_year": tax_year,
        "status": "draft",
        "download_path": (
            f"/api/workspaces/{workspace_id}/entities/{entity_id}"
            f"/work-products/{wp_id}/document.pdf"
        ),
        "computed": c,
        "assumptions": built["assumptions"],
        "_draft_notice": (
            f"DRAFT — computed Form 1040 for {tax_year}: AGI ${c['agi']}, taxable "
            f"income ${c['taxable_income']}, total tax ${c['total_tax']}, "
            f"balance due ${c['balance_due']}. Figures computed by the deterministic "
            "engine; review, enter SSN(s), and sign before filing. Carries a DRAFT "
            "watermark until reviewed or self-certified."
        ),
    }
