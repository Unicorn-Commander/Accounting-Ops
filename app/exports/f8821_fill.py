"""Form 8821 (Tax Information Authorization) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute anything and does NOT fabricate any figure. Each input value carries a
``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface.

What Form 8821 is
-----------------
Form 8821 allows a taxpayer to authorize the IRS to discuss their tax
information with a designated person or organization (e.g., a CPA, EA, or
attorney). It does NOT authorize the designee to sign the tax return or bind
the taxpayer to any agreement.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another.

Taxpayer-completed fields
-------------------------
The taxpayer's signature and date (and potentially the designee's signature
if required by specific use cases, though typically only taxpayer signs) are
intentionally LEFT BLANK by this renderer even if values were supplied — they
are identity items the taxpayer must enter themselves. See
``TAXPAYER_COMPLETED_KEYS``.

XFA-hybrid form
---------------
Form 8821 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming),
so it is filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe
/Fields-tree walker that bakes values in as a flattened, visible overlay), NOT
the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f8821_fill import render_8821

    pdf_bytes, content_type = render_8821(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "taxpayer_name":      {"value": "John Doe", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_address":   {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_city":      {"value": "Columbia", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_state":     {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_zip":       {"value": "29201", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_tin":       {"value": "123-45-6789", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_phone":     {"value": "555-1234", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_name":      {"value": "Jane Smith, CPA", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_address":   {"value": "456 Oak Ave", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_city":      {"value": "Columbia", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_state":     {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_zip":       {"value": "29201", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_caf":       {"value": "12345", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_ptin":      {"value": "P12345678", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_phone":     {"value": "555-5678", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_fax":       {"value": "555-9999", "source_ref": "intake#1", "origin": "user_entered"},
            "tax_type_1":         {"value": "Federal", "source_ref": "intake#1", "origin": "user_entered"},
            "tax_form_1":         {"value": "1040", "source_ref": "intake#1", "origin": "user_entered"},
            "tax_years_1":        {"value": "2024", "source_ref": "intake#1", "origin": "user_entered"},
            "tax_matters_1":      {"value": "All matters", "source_ref": "intake#1", "origin": "user_entered"},
            "designee_new_address":{"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f8821_field_map import (
    F8821_FIELDS,
    F8821_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
)

# Truthy strings that set a checkbox. Anything else (incl. "false"/"no"/blank)
# leaves the box /Off (conservative).
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}


def _f8821_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 8821 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f8821_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f8821.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f8821_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f8821.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_8821(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 8821 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f8821_{tax_year}.pdf`` then
        ``f8821.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F8821_FIELDS`` (e.g. ``"name"``,
        ``"tax_type_1"``) OR a checkbox key from ``F8821_CHECKBOX_FIELDS``
        (e.g. ``"designee_new_address"``) whose truthy value ("true"/"1"/"yes"/"x")
        sets that box. Provenance (``source_ref``) is validated upstream; this
        renderer trusts the caller. A blank/empty value is skipped.

        Notes:
          - ``taxpayer_signature``, ``taxpayer_date``, ``designee_signature``,
            ``designee_date`` are NEVER filled here even if supplied (identity
            items for the taxpayer/designee).
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form 8821 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f8821_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 8821 PDF not found at {source_path}. "
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

        # Checkbox keys: a truthy value sets the box; anything else leaves it
        # /Off.
        if key in F8821_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F8821_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Identity items the taxpayer/designee completes themselves — never
        # auto-filled.
        if key in TAXPAYER_COMPLETED_KEYS:
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = F8821_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
