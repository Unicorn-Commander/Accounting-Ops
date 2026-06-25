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

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.tax.w4p_worksheet import (
    W4PFilingStatus,
    W4PWorksheetInput,
    compute_w4p_worksheet,
)
from app.workspace_session import workspace_session

_VALID_STATUS = {s.value for s in W4PFilingStatus}


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    """Split a transcribed recipient name into (first+MI, last). Best-effort."""
    if not full or not full.strip():
        return None, None
    parts = full.strip().split()
    if len(parts) == 1:
        return None, parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _inp(value: Any, source_ref: str, origin: str) -> dict[str, Any]:
    return {"value": str(value), "source_ref": source_ref, "origin": origin}


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
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")  # noqa: F841

    status = (filing_status or "").strip().lower()
    if status not in _VALID_STATUS:
        raise ValueError(
            f"filing_status must be one of {sorted(_VALID_STATUS)}; got {filing_status!r}"
        )

    # Worksheet (deterministic; raises on an unsupported tax year).
    ws = compute_w4p_worksheet(
        W4PWorksheetInput(
            tax_year=tax_year,
            filing_status=W4PFilingStatus(status),
            qualifying_children=max(0, qualifying_children),
            other_dependents=max(0, other_dependents),
            other_credits=max(0, other_credits_dollars),
            estimated_annual_income=estimated_annual_income,
            estimated_itemized_deductions=max(0, estimated_itemized_deductions),
            estimated_adjustments=max(0, estimated_adjustments),
        )
    )

    auth = current_auth()
    creator = (auth.agent_id or auth.uc_uid) if auth else None

    async with workspace_session(SessionLocal, workspace_id) as session:
        # Resolve the 1099-R source (identity provenance).
        if source_extraction_id:
            de = (
                await session.execute(
                    text(
                        "SELECT id::text, recipient_name, status FROM document_extraction "
                        "WHERE id = :id AND entity_id = :eid"
                    ),
                    {"id": source_extraction_id, "eid": str(entity_id)},
                )
            ).mappings().first()
        else:
            de = (
                await session.execute(
                    text(
                        "SELECT id::text, recipient_name, status FROM document_extraction "
                        "WHERE entity_id = :eid AND doc_type = '1099-R' "
                        "AND status = 'extracted' "
                        "ORDER BY COALESCE(extracted_at, created_at) DESC LIMIT 1"
                    ),
                    {"eid": str(entity_id)},
                )
            ).mappings().first()

        source_ref = de["id"] if de else None
        first, last = _split_name(de["recipient_name"] if de else None)

        # Build the provenance-bearing inputs map. Only fields with a meaningful
        # value are written (conservative blank-fill); every written value carries
        # a source_ref so the render-time anti-fabrication guard passes.
        inputs: dict[str, Any] = {}
        if first:
            inputs["first_name_mi"] = _inp(first, source_ref or "user_entered",
                                           "extracted" if source_ref else "user_entered")
        if last:
            inputs["last_name"] = _inp(last, source_ref or "user_entered",
                                       "extracted" if source_ref else "user_entered")
        if address:
            inputs["address"] = _inp(address, "user_entered", "user_entered")
        if city_state_zip:
            inputs["city_state_zip"] = _inp(city_state_zip, "user_entered", "user_entered")
        inputs["filing_status"] = _inp(status, "user_entered", "user_entered")
        if elect_no_withholding:
            inputs["no_withholding"] = _inp("true", "user_entered", "user_entered")

        # Step 3 (computed)
        if ws.step3a_child_credit:
            inputs["step3a_child"] = _inp(ws.step3a_child_credit, "worksheet", "computed")
        if ws.step3b_other_dep_credit:
            inputs["step3b_other"] = _inp(ws.step3b_other_dep_credit, "worksheet", "computed")
        if other_credits_dollars:
            inputs["step3c_credits"] = _inp(max(0, other_credits_dollars), "user_entered", "user_entered")
        if ws.step3_total:
            inputs["step3_total"] = _inp(ws.step3_total, "worksheet", "computed")

        # Step 4
        if other_income_4a:
            inputs["step4a_income"] = _inp(max(0, other_income_4a), "user_entered", "user_entered")
        if ws.step4b_deductions:
            inputs["step4b_deductions"] = _inp(ws.step4b_deductions, "worksheet", "computed")
        if extra_withholding_per_period:
            inputs["step4c_extra"] = _inp(max(0, extra_withholding_per_period), "user_entered", "user_entered")

        provenance = {
            "form": "w-4p",
            "tax_year": tax_year,
            "worksheet_engine": "app.tax.w4p_worksheet",
            "standard_deduction": ws.standard_deduction,
            "assumptions": ws.assumptions,
            "source_extraction_id": source_ref,
            "ssn_note": "SSN is intentionally left blank for the taxpayer to enter by hand.",
        }
        title = f"Form W-4P {tax_year}" + (f" — {first or ''} {last or ''}".rstrip())

        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO work_product (
                        workspace_id, entity_id, kind, tax_year, title, status,
                        inputs, provenance, source_extraction_id, created_by_uc_uid
                    ) VALUES (
                        :wid, :eid, 'w-4p', :ty, :title, 'draft',
                        CAST(:inputs AS jsonb), CAST(:prov AS jsonb),
                        :src, :creator
                    )
                    RETURNING CAST(id AS text) AS id
                    """
                ),
                {
                    "wid": str(workspace_id),
                    "eid": str(entity_id),
                    "ty": tax_year,
                    "title": title.strip(),
                    "inputs": json.dumps(inputs),
                    "prov": json.dumps(provenance),
                    "src": source_ref,
                    "creator": creator,
                },
            )
        ).mappings().first()

    wp_id = row["id"]
    download_path = (
        f"/api/workspaces/{workspace_id}/entities/{entity_id}"
        f"/work-products/{wp_id}/document.pdf"
    )
    summary = (
        f"Prepared a DRAFT Form W-4P ({tax_year}, {status}). "
        f"Step 3 dependent credit ${ws.step3_total:,}; "
        f"Step 4(b) deductions ${ws.step4b_deductions:,}. "
        + ("Identity sourced from the 1099-R. " if source_ref else "No 1099-R found; name left blank. ")
        + "SSN and signature are left for the taxpayer."
    )

    return {
        "ok": True,
        "work_product_id": wp_id,
        "kind": "w-4p",
        "tax_year": tax_year,
        "status": "draft",
        "download_path": download_path,
        "summary": summary,
        "assumptions": ws.assumptions,
        "_draft_notice": (
            "DRAFT — this W-4P carries a DRAFT watermark until it is reviewed by a "
            "professional or self-certified (if no professional is available). It "
            "goes to your pension payer, not the IRS. Review every figure, enter "
            "your SSN, and sign Step 5 before giving it to your payer."
        ),
    }
