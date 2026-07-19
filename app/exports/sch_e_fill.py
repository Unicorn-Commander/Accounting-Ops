"""Schedule E (Form 1040) — Supplemental Income and Loss — form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute the schedule and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

Schedule E reports supplemental income/loss from rental real estate and
royalties (Part I), partnerships and S corporations (Part II), estates and
trusts (Part III), REMICs (Part IV), and the Part V summary. The single sheet
attaches to the taxpayer's Form 1040.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another. A logical key with no AcroForm
mapping is simply skipped (conservative — never invent a field).

Taxpayer-completed fields
-------------------------
The taxpayer's SSN is intentionally NOT filled by this renderer (it is not even
exposed as a logical key) — it is an identity item the taxpayer enters on the
final form. Schedule E has no signature line of its own; it is signed by way of
the attached, signed Form 1040.

XFA-hybrid form
---------------
Schedule E is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like the W-4P / Form 1040), so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that sets
/V, BAKES each value in as a flattened, visible overlay, drops the XFA layer, and
stamps the status watermark), NOT the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.sch_e_fill import render_sch_e

    pdf_bytes, content_type = render_sch_e(
        tax_year=2024,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name_shown":      {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "property_a_address": {"value": "123 Rental Way, Greenville SC", "source_ref": "intake#1", "origin": "user_entered"},
            "property_a_type": {"value": "1", "source_ref": "intake#1", "origin": "user_entered"},
            "rents_3_a":       {"value": "24000", "source_ref": "lease#1", "origin": "document"},
            "mortgage_interest_12_a": {"value": "8200", "source_ref": "1098#1", "origin": "document"},
            "line_26_total_rre_royalty": {"value": "15800", "source_ref": "calc#1", "origin": "computed"},
            "line_a_1099_required": {"value": "no", "source_ref": "intake#1", "origin": "user_entered"},
            "property_a_qjv":  {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
        },
    )
"""

from __future__ import annotations

from pathlib import Path

from app.exports.form_prefill import (
    IRS_FORMS_DIR,
    WatermarkMode,
    _render_acroform_pdf,
)
from app.exports.sch_e_field_map import (
    SCHEDULE_E_FIELDS,
    SCHEDULE_E_CHECKBOX_FIELDS,
    SCHEDULE_E_YESNO_FIELDS,
)

# Values (case-insensitive) that mark a single boolean checkbox as "checked".
_TRUTHY = frozenset({"true", "1", "yes", "y", "x", "checked", "on"})


def _sch_e_form_path(tax_year: int | None) -> Path:
    """Resolve the Schedule E source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1040se_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1040se.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1040se_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1040se.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_sch_e(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Schedule E from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``f1040se_{tax_year}.pdf`` then ``f1040se.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``SCHEDULE_E_FIELDS`` (e.g.
        ``"rents_3_a"``, ``"mortgage_interest_12_a"``, ``"line_26_total_rre_royalty"``)
        OR one of the special checkbox keys:

          - A single-checkbox key from ``SCHEDULE_E_CHECKBOX_FIELDS`` (e.g.
            ``"property_a_qjv"``, ``"partnership_a_not_at_risk"``) — the box is
            checked when the value is truthy ("true"/"1"/"yes"/"x"/…).
          - A yes/no-question key from ``SCHEDULE_E_YESNO_FIELDS`` (e.g.
            ``"line_a_1099_required"``, ``"line_27_prior_year_loss"``) — the
            value ``"yes"`` or ``"no"`` selects the matching box; any other value
            is ignored (conservative — no box set).

        Provenance (``source_ref``) is validated upstream; this renderer does
        not re-validate but trusts the caller. A blank/empty value is skipped.

        Note: ``name_shown`` is stamped on BOTH the page-1 and page-2 headers
        (the preparer writes the name on both pages); a caller may still set
        ``name_shown_p2`` explicitly to override the page-2 value. The taxpayer's
        SSN is never filled here.

    Raises
    ------
    FileNotFoundError
        If no Schedule E source PDF is present in ``data/irs_forms/``.
    """
    source_path = _sch_e_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Schedule E PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}

    for key, payload in inputs.items():
        # Each input payload is a dict with at least a "value"; provenance was
        # validated upstream. A missing/blank value means "leave it blank".
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value) == "":
            continue
        value = str(value)

        # Yes/No question: value selects the matching box; unknown answers are
        # ignored (conservative — never guess the taxpayer's answer).
        yesno = SCHEDULE_E_YESNO_FIELDS.get(key)
        if yesno is not None:
            mapped = yesno.get(value.strip().lower())
            if mapped:
                fname, on_state = mapped
                field_values[fname] = on_state
            continue

        # Single boolean checkbox: check only when the value is truthy.
        checkbox = SCHEDULE_E_CHECKBOX_FIELDS.get(key)
        if checkbox is not None:
            if value.strip().lower() in _TRUTHY:
                fname, on_state = checkbox
                field_values[fname] = on_state
            continue

        # Name is written on both page headers (preparer fills both pages).
        if key == "name_shown":
            field_values[SCHEDULE_E_FIELDS["name_shown"]] = value
            if "name_shown_p2" not in inputs:
                field_values[SCHEDULE_E_FIELDS["name_shown_p2"]] = value
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = SCHEDULE_E_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
