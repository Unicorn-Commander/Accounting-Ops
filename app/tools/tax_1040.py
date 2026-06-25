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

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.tax.compute_1040 import compute_1040
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
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")  # noqa: F841

    def _ln(v: Any) -> dict[str, Any]:
        return {"value": v, "source_ref": income_source}

    income = {
        "wages": _ln(wages),
        "taxable_interest": _ln(taxable_interest),
        "ordinary_dividends": _ln(ordinary_dividends),
        "qualified_dividends": _ln(qualified_dividends),
        "ira_taxable": _ln(ira_taxable),
        "pensions_taxable": _ln(pensions_taxable),
        "social_security_taxable": _ln(social_security_taxable),
        "capital_gain_loss": _ln(capital_gain_loss),
        "schedule_1_income": _ln(other_income),
        "adjustments": _ln(adjustments),
        "itemized_deduction": _ln(itemized_deduction),
        "qbi_deduction": _ln(qbi_deduction),
        "nonrefundable_credits": _ln(nonrefundable_credits),
        "withholding": _ln(withholding),
        "estimated_payments": _ln(estimated_payments),
        "filing_status": {"value": filing_status, "source_ref": "user_entered"},
    }

    out = compute_1040(tax_year, filing_status, income)  # raises on bad year/status

    auth = current_auth()
    creator = (auth.agent_id or auth.uc_uid) if auth else None
    title = f"Form 1040 {tax_year} ({filing_status.upper()})"

    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO work_product (
                        workspace_id, entity_id, kind, tax_year, title, status,
                        inputs, provenance, created_by_uc_uid
                    ) VALUES (
                        :wid, :eid, '1040', :ty, :title, 'draft',
                        CAST(:inputs AS jsonb), CAST(:prov AS jsonb), :creator
                    )
                    RETURNING CAST(id AS text) AS id
                    """
                ),
                {
                    "wid": str(workspace_id),
                    "eid": str(entity_id),
                    "ty": tax_year,
                    "title": title,
                    "inputs": json.dumps(out["inputs"]),
                    "prov": json.dumps(out["provenance"]),
                    "creator": creator,
                },
            )
        ).mappings().first()

    wp_id = row["id"]
    c = out["provenance"]["computed"]
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
        "assumptions": out["provenance"]["assumptions"],
        "_draft_notice": (
            f"DRAFT — computed Form 1040 for {tax_year}: AGI ${c['agi']}, taxable "
            f"income ${c['taxable_income']}, total tax ${c['total_tax']}, "
            f"balance due ${c['balance_due']}. Figures computed by the deterministic "
            "engine; review, enter SSN(s), and sign before filing. Carries a DRAFT "
            "watermark until reviewed or self-certified."
        ),
    }
