"""Work-product creation service — the single source of truth for building +
storing a tax work product.

Both the MCP tools (``tax_compute_1040`` / ``tax_fill_w4p`` / ``tax_prepare_filing``)
and the REST create endpoint (``POST .../work-products``) go through these
helpers, so the security-critical pieces — the anti-fabrication provenance guard,
the income→engine→inputs mapping, the W-4P worksheet→inputs mapping, and the
``work_product`` INSERT — exist in exactly one place and cannot drift between the
agent path and the browser path.

Nothing here authorizes or resolves a workspace; callers (a tool via
``authorize_workspace`` / a route via ``resolve_workspace`` + ``require_role``)
own that and pass an already-scoped ``workspace_session`` in.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.tax.compute_1040 import compute_1040
from app.tax.w4p_worksheet import (
    W4PFilingStatus,
    W4PWorksheetInput,
    compute_w4p_worksheet,
)

# W-4P filing-status vocabulary (the worksheet's own enum values).
W4P_VALID_STATUS = {s.value for s in W4PFilingStatus}


# ---------------------------------------------------------------------------
# Anti-fabrication: normalize a caller-supplied {field: {value, source_ref}} map
# ---------------------------------------------------------------------------


def normalize_provenance_fields(fields: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Validate + normalize a fill-form field map (the ``tax_prepare_filing`` rule).

    Every non-blank value MUST carry a ``source_ref`` — we refuse to store a
    figure without one (mirrors the render-time guard so a bad work product can
    never be stored, not just refused at render). Blank values are dropped
    (conservative blank-fill). Returns ``{key: {value, source_ref, origin}}``.
    """
    inputs: dict[str, dict[str, str]] = {}
    for key, rec in (fields or {}).items():
        if not isinstance(rec, dict):
            raise ValueError(
                f"field {key!r} must be an object {{value, source_ref, origin?}}"
            )
        value = rec.get("value")
        if value is None or str(value).strip() == "":
            continue  # conservative blank-fill — omit blanks
        if not rec.get("source_ref"):
            raise ValueError(
                f"field {key!r} has a value but no source_ref — never fabricate a "
                "figure; tag every value with where it came from"
            )
        inputs[key] = {
            "value": str(value),
            "source_ref": str(rec["source_ref"]),
            "origin": str(rec.get("origin") or "user_entered"),
        }
    return inputs


# ---------------------------------------------------------------------------
# The shared INSERT
# ---------------------------------------------------------------------------


async def insert_work_product(
    session,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    kind: str,
    tax_year: int | None,
    title: str,
    inputs: dict[str, Any],
    provenance: dict[str, Any],
    source_extraction_id: str | None = None,
    creator: str | None = None,
) -> str:
    """Insert one ``work_product`` row (status='draft') and return its id::text.

    Runs inside the caller's already-scoped ``workspace_session`` (RLS bound).
    """
    row = (
        await session.execute(
            text(
                """
                INSERT INTO work_product (
                    workspace_id, entity_id, kind, tax_year, title, status,
                    inputs, provenance, source_extraction_id, created_by_uc_uid
                ) VALUES (
                    :wid, :eid, :kind, :ty, :title, 'draft',
                    CAST(:inputs AS jsonb), CAST(:prov AS jsonb), :src, :creator
                )
                RETURNING CAST(id AS text) AS id
                """
            ),
            {
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "kind": kind,
                "ty": tax_year,
                "title": title,
                "inputs": json.dumps(inputs),
                "prov": json.dumps(provenance),
                "src": source_extraction_id,
                "creator": creator,
            },
        )
    ).mappings().first()
    return row["id"]


# ---------------------------------------------------------------------------
# Computed Form 1040 (the calculator path)
# ---------------------------------------------------------------------------

# The income/deduction/payment levers a 1040 computation accepts (whole dollars).
COMPUTE_1040_AMOUNT_FIELDS = (
    "wages", "taxable_interest", "ordinary_dividends", "qualified_dividends",
    "ira_taxable", "pensions_taxable", "social_security_taxable",
    "capital_gain_loss", "other_income", "adjustments", "itemized_deduction",
    "qbi_deduction", "nonrefundable_credits", "withholding", "estimated_payments",
)


def build_1040_computed(
    tax_year: int,
    filing_status: str,
    amounts: dict[str, Any],
    income_source: str = "client documents",
) -> dict[str, Any]:
    """Run the deterministic engine and return the storable 1040 payload.

    ``amounts`` maps any of ``COMPUTE_1040_AMOUNT_FIELDS`` to whole-dollar
    figures (missing = 0, not fabricated). Returns ``{inputs, provenance, title,
    computed, assumptions}`` — ``inputs``/``provenance`` go straight to
    ``insert_work_product``; ``computed`` is the engine summary for the caller's
    response. Raises ValueError on an unsupported year/status.
    """
    def _ln(v: Any) -> dict[str, Any]:
        return {"value": v, "source_ref": income_source}

    income = {
        "wages": _ln(amounts.get("wages", 0)),
        "taxable_interest": _ln(amounts.get("taxable_interest", 0)),
        "ordinary_dividends": _ln(amounts.get("ordinary_dividends", 0)),
        "qualified_dividends": _ln(amounts.get("qualified_dividends", 0)),
        "ira_taxable": _ln(amounts.get("ira_taxable", 0)),
        "pensions_taxable": _ln(amounts.get("pensions_taxable", 0)),
        "social_security_taxable": _ln(amounts.get("social_security_taxable", 0)),
        "capital_gain_loss": _ln(amounts.get("capital_gain_loss", 0)),
        "schedule_1_income": _ln(amounts.get("other_income", 0)),
        "adjustments": _ln(amounts.get("adjustments", 0)),
        "itemized_deduction": _ln(amounts.get("itemized_deduction", 0)),
        "qbi_deduction": _ln(amounts.get("qbi_deduction", 0)),
        "nonrefundable_credits": _ln(amounts.get("nonrefundable_credits", 0)),
        "withholding": _ln(amounts.get("withholding", 0)),
        "estimated_payments": _ln(amounts.get("estimated_payments", 0)),
        "filing_status": {"value": filing_status, "source_ref": "user_entered"},
    }

    out = compute_1040(tax_year, filing_status, income)  # raises on bad year/status
    return {
        "inputs": out["inputs"],
        "provenance": out["provenance"],
        "title": f"Form 1040 {tax_year} ({filing_status.upper()})",
        "computed": out["provenance"]["computed"],
        "assumptions": out["provenance"]["assumptions"],
    }


# ---------------------------------------------------------------------------
# Computed Form W-4P (pension/annuity withholding)
# ---------------------------------------------------------------------------


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    """Split a transcribed recipient name into (first+MI, last). Best-effort."""
    if not full or not full.strip():
        return None, None
    parts = full.strip().split()
    if len(parts) == 1:
        return None, parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _w4p_inp(value: Any, source_ref: str, origin: str) -> dict[str, Any]:
    return {"value": str(value), "source_ref": source_ref, "origin": origin}


async def build_w4p_computed(
    session,
    *,
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
    first_name_mi: str | None = None,
    last_name: str | None = None,
    address: str | None = None,
    city_state_zip: str | None = None,
    elect_no_withholding: bool = False,
    source_extraction_id: str | None = None,
) -> dict[str, Any]:
    """Run the W-4P worksheet + build the storable payload.

    Identity (name) is sourced, in order: an explicit ``first_name_mi``/
    ``last_name`` (user_entered), else the named/latest 1099-R extraction for the
    entity (extracted). Returns ``{inputs, provenance, title, source_extraction_id,
    summary, assumptions, worksheet}``. Raises ValueError on a bad status/year.
    """
    status = (filing_status or "").strip().lower()
    if status not in W4P_VALID_STATUS:
        raise ValueError(
            f"filing_status must be one of {sorted(W4P_VALID_STATUS)}; got {filing_status!r}"
        )

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

    # Resolve identity. Explicit name wins; otherwise read a 1099-R extraction.
    src_ref: str | None = None
    first = (first_name_mi or "").strip() or None
    last = (last_name or "").strip() or None
    name_origin = "user_entered"
    if not (first or last):
        if source_extraction_id:
            de = (
                await session.execute(
                    text(
                        "SELECT id::text, recipient_name FROM document_extraction "
                        "WHERE id = :id AND entity_id = :eid"
                    ),
                    {"id": source_extraction_id, "eid": str(entity_id)},
                )
            ).mappings().first()
        else:
            de = (
                await session.execute(
                    text(
                        "SELECT id::text, recipient_name FROM document_extraction "
                        "WHERE entity_id = :eid AND doc_type = '1099-R' "
                        "AND status = 'extracted' "
                        "ORDER BY COALESCE(extracted_at, created_at) DESC LIMIT 1"
                    ),
                    {"eid": str(entity_id)},
                )
            ).mappings().first()
        if de:
            src_ref = de["id"]
            first, last = _split_name(de["recipient_name"])
            name_origin = "extracted"
    elif source_extraction_id:
        # Explicit name supplied AND an anchor doc id given — keep the anchor.
        src_ref = source_extraction_id

    inputs: dict[str, Any] = {}
    if first:
        inputs["first_name_mi"] = _w4p_inp(first, src_ref or "user_entered", name_origin)
    if last:
        inputs["last_name"] = _w4p_inp(last, src_ref or "user_entered", name_origin)
    if address:
        inputs["address"] = _w4p_inp(address, "user_entered", "user_entered")
    if city_state_zip:
        inputs["city_state_zip"] = _w4p_inp(city_state_zip, "user_entered", "user_entered")
    inputs["filing_status"] = _w4p_inp(status, "user_entered", "user_entered")
    if elect_no_withholding:
        inputs["no_withholding"] = _w4p_inp("true", "user_entered", "user_entered")

    # Step 3 (computed)
    if ws.step3a_child_credit:
        inputs["step3a_child"] = _w4p_inp(ws.step3a_child_credit, "worksheet", "computed")
    if ws.step3b_other_dep_credit:
        inputs["step3b_other"] = _w4p_inp(ws.step3b_other_dep_credit, "worksheet", "computed")
    if other_credits_dollars:
        inputs["step3c_credits"] = _w4p_inp(max(0, other_credits_dollars), "user_entered", "user_entered")
    if ws.step3_total:
        inputs["step3_total"] = _w4p_inp(ws.step3_total, "worksheet", "computed")

    # Step 4
    if other_income_4a:
        inputs["step4a_income"] = _w4p_inp(max(0, other_income_4a), "user_entered", "user_entered")
    if ws.step4b_deductions:
        inputs["step4b_deductions"] = _w4p_inp(ws.step4b_deductions, "worksheet", "computed")
    if extra_withholding_per_period:
        inputs["step4c_extra"] = _w4p_inp(max(0, extra_withholding_per_period), "user_entered", "user_entered")

    provenance = {
        "form": "w-4p",
        "tax_year": tax_year,
        "worksheet_engine": "app.tax.w4p_worksheet",
        "standard_deduction": ws.standard_deduction,
        "assumptions": ws.assumptions,
        "source_extraction_id": src_ref,
        "ssn_note": "SSN is intentionally left blank for the taxpayer to enter by hand.",
    }
    title = f"Form W-4P {tax_year}" + (f" — {first or ''} {last or ''}".rstrip())
    summary = (
        f"Prepared a DRAFT Form W-4P ({tax_year}, {status}). "
        f"Step 3 dependent credit ${ws.step3_total:,}; "
        f"Step 4(b) deductions ${ws.step4b_deductions:,}. "
        + ("Identity sourced from the 1099-R. " if (src_ref and name_origin == "extracted") else
           ("Name supplied by the preparer. " if (first or last) else "No 1099-R found; name left blank. "))
        + "SSN and signature are left for the taxpayer."
    )
    return {
        "inputs": inputs,
        "provenance": provenance,
        "title": title.strip(),
        "source_extraction_id": src_ref,
        "summary": summary,
        "assumptions": ws.assumptions,
        "worksheet": ws,
    }
