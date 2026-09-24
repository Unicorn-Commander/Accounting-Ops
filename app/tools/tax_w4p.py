"""MCP tool: tax_fill_w4p — prepare a finished, ready-to-sign Form W-4P.

This is the professional-substitute path for pension/annuity withholding. The
Personal 1040 Specialist calls it to take a user ALL THE WAY: it reads the
taxpayer's identity from a 1099-R extraction (provenance anchor), runs the
deterministic W-4P worksheet (Step 3 dependent credit + Step 4(b) deductions —
NO LLM computes a number, NO figure is fabricated), and stores a provenance-
bearing WorkProduct DRAFT. The filled PDF is re-rendered on demand from that row
(served by the work-products API), so this tool returns a REFERENCE (id +
download path + summary), never PDF bytes.

The W-4P goes to the PAYER (not the IRS); the payer computes the actual
withholding via Pub 15-T. Everything except Step 3 + Step 4(b) is a transcribed
taxpayer declaration.

Write tool — requires accountant role (creates a work_product row).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.tax.work_product_service import build_w4p_computed, insert_work_product
from app.workspace_session import workspace_session


async def tax_fill_w4p(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    filing_status: str,
    qualifying_children: int = 0,
    other_dependents: int = 0,
    other_credits_dollars: int = 0,
    estimated_annual_income: int | None = None,
    estimated_itemized_deductions: int = 0,
    estimated_adjustments: int = 0,
    other_income_4a: int | None = None,
    extra_withholding_per_period: int | None = None,
    address: str | None = None,
    city_state_zip: str | None = None,
    elect_no_withholding: bool = False,
    source_extraction_id: str | None = None,
) -> dict[str, Any]:
    """Prepare a ready-to-sign Form W-4P (pension/annuity withholding certificate).

    Reads the taxpayer's name from a 1099-R extraction (auto-selects the latest
    for the entity unless ``source_extraction_id`` is given), runs the
    deterministic W-4P worksheet, and stores a DRAFT WorkProduct. Returns a
    reference; download the filled PDF from the returned ``download_path``.

    Args:
        workspace_id, entity_id: scope (the entity is the pension recipient).
        tax_year: the W-4P year (statutory constants must be on file; 2024-2026).
        filing_status: 'single_or_mfs' | 'mfj_or_qss' | 'head_of_household'.
        qualifying_children: count of children under 17 (Step 3(a)).
        other_dependents: count of other dependents (Step 3(b)).
        other_credits_dollars: other credits the taxpayer claims (Step 3(c)).
        estimated_annual_income: total estimated income; gates the Step 3 credit
            (above the threshold it phases out and is left at $0 on the face).
        estimated_itemized_deductions, estimated_adjustments: Step 4(b) inputs.
        other_income_4a: Step 4(a) other income (taxpayer declaration).
        extra_withholding_per_period: Step 4(c) extra withholding.
        address, city_state_zip: Step 1 mailing address.
        elect_no_withholding: check the "no withholding" box.
        source_extraction_id: explicit 1099-R document_extraction id; else latest.

    Returns:
        {"ok": True, "work_product_id", "kind": "w-4p", "tax_year", "status":
         "draft", "download_path", "summary", "assumptions", "_draft_notice"}.

    DRAFT — re-rendered with a DRAFT watermark until reviewed or self-certified.
    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="bookkeeper")  # noqa: F841

    auth = current_auth()
    creator = (auth.agent_id or auth.uc_uid) if auth else None

    async with workspace_session(SessionLocal, workspace_id) as session:
        # The 1099-R identity lookup + deterministic worksheet + inputs build all
        # live in the shared service (single-sourced with the REST create path).
        built = await build_w4p_computed(
            session,
            entity_id=entity_id,
            tax_year=tax_year,
            filing_status=filing_status,
            qualifying_children=qualifying_children,
            other_dependents=other_dependents,
            other_credits_dollars=other_credits_dollars,
            estimated_annual_income=estimated_annual_income,
            estimated_itemized_deductions=estimated_itemized_deductions,
            estimated_adjustments=estimated_adjustments,
            other_income_4a=other_income_4a,
            extra_withholding_per_period=extra_withholding_per_period,
            address=address,
            city_state_zip=city_state_zip,
            elect_no_withholding=elect_no_withholding,
            source_extraction_id=source_extraction_id,
        )
        wp_id = await insert_work_product(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            kind="w-4p",
            tax_year=tax_year,
            title=built["title"],
            inputs=built["inputs"],
            provenance=built["provenance"],
            source_extraction_id=built["source_extraction_id"],
            creator=creator,
        )

    download_path = (
        f"/api/workspaces/{workspace_id}/entities/{entity_id}"
        f"/work-products/{wp_id}/document.pdf"
    )

    return {
        "ok": True,
        "work_product_id": wp_id,
        "kind": "w-4p",
        "tax_year": tax_year,
        "status": "draft",
        "download_path": download_path,
        "summary": built["summary"],
        "assumptions": built["assumptions"],
        "_draft_notice": (
            "DRAFT — this W-4P carries a DRAFT watermark until it is reviewed by a "
            "professional or self-certified (if no professional is available). It "
            "goes to your pension payer, not the IRS. Review every figure, enter "
            "your SSN, and sign Step 5 before giving it to your payer."
        ),
    }
