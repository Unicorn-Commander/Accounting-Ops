"""Form 1125-E (Compensation of Officers) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute the compensation totals and does NOT fabricate any figure. Each input
value carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

What Form 1125-E is
------------------
An attachment to Form 1120 or 1120-S that lists compensation paid to officers.
The fillable form is a single-page table.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another (e.g. line 4 is taken from the
caller, not computed from lines 2 and 3).

Officer SSNs
------------
Officer SSNs are intentionally LEFT BLANK by this renderer even if values were
supplied — they are identity items the taxpayer must enter themselves. (Form
1125-E has no signature line.) See ``TAXPAYER_COMPLETED_KEYS``.

XFA-hybrid form
---------------
Form 1125-E is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field
naming), so it is filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe
/Fields-tree walker that bakes values in as a flattened, visible overlay), NOT
the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f1125e_fill import render_1125e

    pdf_bytes, content_type = render_1125e(
        tax_year=2024,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "corporation_name": {"value": "Acme Corp", "source_ref": "intake#1", "origin": "user_entered"},
            "ein":             {"value": "12-3456789", "source_ref": "intake#1", "origin": "user_entered"},
            "officers": [
                {
                    "name":      {"value": "John Doe", "source_ref": "payroll#1", "origin": "document"},
                    "ssn":       {"value": "123-45-6789", "source_ref": "payroll#1", "origin": "document"},
                    "pct_time":  {"value": "100", "source_ref": "payroll#1", "origin": "document"},
                    "common":    {"value": "50", "source_ref": "cap_table#1", "origin": "document"},
                    "preferred": {"value": "0", "source_ref": "cap_table#1", "origin": "document"},
                    "comp":      {"value": "100000", "source_ref": "payroll#1", "origin": "document"},
                },
            ],
            "line_2_total": {"value": "100000", "source_ref": "calc#1", "origin": "computed"},
            "line_3_claimed": {"value": "0", "source_ref": "calc#1", "origin": "computed"},
            "line_4_net": {"value": "100000", "source_ref": "calc#1", "origin": "computed"},
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
from app.exports.f1125e_field_map import (
    F1125E_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
)


def _f1125e_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 1125-E source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1125e_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1125e.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1125e_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1125e.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_1125e(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 1125-E from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f1125e_{tax_year}.pdf``
        then ``f1125e.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F1125E_FIELDS`` (e.g.
        ``"corporation_name"``, ``"line_2_total"``) OR a special key ``"officers"``
        which is a list of officer dicts. Each officer dict may contain keys:
        ``name``, ``ssn``, ``pct_time``, ``common``, ``preferred``, ``comp``.
        Provenance (``source_ref``) is validated upstream; this renderer trusts
        the caller. A blank/empty value is skipped.

        Notes:
          - Officer SSNs are NEVER filled here even if supplied (identity items
            for the taxpayer).
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form 1125-E source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f1125e_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1125-E PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}

    # Process header and total line fields
    for key, payload in inputs.items():
        if key == "officers":
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value) == "":
            continue
        value = str(value)

        acro_name = F1125E_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    # Process officers list
    officers = inputs.get("officers")
    if isinstance(officers, list):
        for idx, officer in enumerate(officers, start=1):
            if not isinstance(officer, dict):
                continue
            if idx > 20:
                break  # Max 20 officers

            for col_key in ("name", "ssn", "pct_time", "common", "preferred", "comp"):
                logical_key = f"officer_{idx}_{col_key}"
                payload = officer.get(col_key)
                if not isinstance(payload, dict):
                    continue
                value = payload.get("value")
                if value is None or str(value) == "":
                    continue
                value = str(value)

                # Skip SSNs (identity fields)
                if col_key == "ssn":
                    continue

                acro_name = F1125E_FIELDS.get(logical_key)
                if acro_name:
                    field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
