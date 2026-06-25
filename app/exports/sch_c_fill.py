"""Schedule C (Form 1040) — Profit or Loss From Business — form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute the schedule and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their books give us onto the official form" surface, not a
bookkeeping engine.

Conservative blank-fill
-----------------------
Any line whose input is absent (or blank) is left empty. We never guess, never
insert a zero, and never derive one line from another (e.g. line 3 is NOT
computed from lines 1 and 2 — the caller supplies it). A logical key with no
AcroForm mapping is simply skipped.

Left blank for the human
------------------------
The proprietor's SSN and the business EIN (line D) are intentionally NOT fillable
by this renderer — they are identity items the taxpayer must enter themselves, so
they are excluded from ``sch_c_field_map`` entirely (see that module). The
signature is likewise wet-ink.

XFA-hybrid form
---------------
Schedule C is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like Form 1040 / the W-4P), so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that bakes
the values in as a flattened, visible overlay so they render in EVERY viewer),
NOT the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.sch_c_fill import render_sch_c

    pdf_bytes, content_type = render_sch_c(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name_of_proprietor":  {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "principal_business":  {"value": "Software consulting", "source_ref": "intake#1", "origin": "user_entered"},
            "accounting_method":   {"value": "cash", "source_ref": "intake#1", "origin": "user_entered"},
            "line1_gross_receipts":{"value": "120000", "source_ref": "books#1", "origin": "document"},
            "line8_advertising":   {"value": "3500", "source_ref": "books#1", "origin": "document"},
            "material_participation": {"value": "yes", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.sch_c_field_map import (
    SCH_C_FIELDS,
    SCH_C_ACCOUNTING_METHOD_FIELDS,
    SCH_C_COGS_METHOD_FIELDS,
    SCH_C_AT_RISK_FIELDS,
    SCH_C_CHECKBOX_FIELDS,
    SCH_C_YESNO_FIELDS,
)

# Values (case-insensitive) that mean "check this standalone box".
_TRUTHY = frozenset({"true", "1", "yes", "y", "x", "on", "checked"})


def _sch_c_form_path(tax_year: int | None) -> Path:
    """Resolve the Schedule C source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1040sc_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1040sc.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1040sc_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1040sc.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_sch_c(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Schedule C (Form 1040) from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``f1040sc_{tax_year}.pdf`` then ``f1040sc.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``SCH_C_FIELDS`` (e.g.
        ``"line1_gross_receipts"``, ``"line8_advertising"``,
        ``"cogs_line42_cogs"``) OR one of the special grouped keys:

          - ``"accounting_method"`` — value ``cash`` | ``accrual`` | ``other``
            (line F radio). Sets exactly that box.
          - ``"cogs_method"`` — value ``cost`` | ``lower_of_cost_or_market`` |
            ``other`` (line 33 inventory-valuation checkboxes).
          - ``"at_risk"`` — value ``all`` (32a) | ``some`` (32b).
          - a Yes/No question key in ``SCH_C_YESNO_FIELDS`` (e.g.
            ``"material_participation"``, ``"made_1099_payments"``,
            ``"will_file_1099"``, ``"vehicle_personal_use"``) — value
            ``yes`` | ``no``.
          - a standalone checkbox key in ``SCH_C_CHECKBOX_FIELDS``
            (``"statutory_employee"``, ``"started_business"``) — checked when the
            value is truthy (``yes`` / ``true`` / ``1`` / ``x`` …).

        Provenance (``source_ref``) is validated upstream; this renderer does not
        re-validate but trusts the caller. A blank/empty value is skipped, as is
        any unknown key (conservative — never invent a field).

    Raises
    ------
    FileNotFoundError
        If no Schedule C source PDF is present in ``data/irs_forms/``.
    """
    source_path = _sch_c_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Schedule C PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh to fetch it "
            "(https://www.irs.gov/pub/irs-pdf/f1040sc.pdf)."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}

    for key, payload in inputs.items():
        # Each input payload is a dict with at least a "value"; provenance was
        # validated upstream. A missing/blank value means "leave it blank".
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value).strip() == "":
            continue
        value = str(value).strip()

        # --- Grouped radios / checkboxes -----------------------------------
        if key == "accounting_method":
            mapped = SCH_C_ACCOUNTING_METHOD_FIELDS.get(value.lower())
            if mapped:
                field_values[mapped[0]] = mapped[1]
            continue

        if key == "cogs_method":
            mapped = SCH_C_COGS_METHOD_FIELDS.get(value.lower())
            if mapped:
                field_values[mapped[0]] = mapped[1]
            continue

        if key == "at_risk":
            mapped = SCH_C_AT_RISK_FIELDS.get(value.lower())
            if mapped:
                field_values[mapped[0]] = mapped[1]
            continue

        if key in SCH_C_YESNO_FIELDS:
            answer = SCH_C_YESNO_FIELDS[key].get(value.lower())
            if answer:
                field_values[answer[0]] = answer[1]
            continue

        if key in SCH_C_CHECKBOX_FIELDS:
            if value.lower() in _TRUTHY:
                fname, on_state = SCH_C_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # --- Ordinary text line --------------------------------------------
        # Map the logical key to its AcroForm field name. Unknown / unmapped
        # keys are skipped (conservative — never invent a field, never write to
        # a line we don't recognize).
        acro_name = SCH_C_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
