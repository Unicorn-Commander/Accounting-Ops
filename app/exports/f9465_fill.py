"""Form 9465 (Installment Agreement Request) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute the tax estimate and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

What Form 9465 is
-----------------
The application an individual files with the IRS to request an installment
agreement (payment plan) for outstanding tax liabilities.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another (e.g. line 8 monthly payment is
taken from the caller, not computed from lines 5 and 6).

Taxpayer-completed fields
-------------------------
The taxpayer's SSN(s) and bank routing/account numbers are intentionally LEFT
BLANK by this renderer even if values were supplied — they are identity/payment
items the taxpayer must enter themselves. (Form 9465 has no signature line in
the fillable portion, but requires physical signature on the printed form.)
See ``TAXPAYER_COMPLETED_KEYS``.

XFA-hybrid form
---------------
Form 9465 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming),
so it is filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-
tree walker that bakes values in as a flattened, visible overlay), NOT the older
page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f9465_fill import render_9465

    pdf_bytes, content_type = render_9465(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "request_forms":       {"value": "1040", "source_ref": "intake#1", "origin": "user_entered"},
            "tax_periods":         {"value": "2024", "source_ref": "intake#1", "origin": "user_entered"},
            "first_name":          {"value": "Aaron J", "source_ref": "intake#1", "origin": "user_entered"},
            "last_name":           {"value": "Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "address":             {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city_state_zip":      {"value": "Columbia, SC 29201", "source_ref": "intake#1", "origin": "user_entered"},
            "total_owed_5":        {"value": "12,000", "source_ref": "calc#1", "origin": "computed"},
            "monthly_payment_11a": {"value": "500", "source_ref": "intake#1", "origin": "user_entered"},
            "payment_day_12":      {"value": "15", "source_ref": "intake#1", "origin": "user_entered"},
            "marital_single_16a":  {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f9465_field_map import (
    F9465_FIELDS,
    F9465_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
)

# Truthy strings that set a checkbox / radio choice (1b, the 433-F bullet,
# 13c, 14, and the Part II choice keys). Anything else (incl. "false"/"no"/
# blank) leaves the box /Off (conservative).
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}


def _f9465_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 9465 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f9465_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f9465.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f9465_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f9465.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_9465(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 9465 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f9465_{tax_year}.pdf`` then
        ``f9465.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F9465_FIELDS`` (e.g.
        ``"first_name"``, ``"total_owed_5"``, ``"monthly_payment_11a"``) OR a
        checkbox/choice key from ``F9465_CHECKBOX_FIELDS``
        (``"address_new_1b"``, ``"marital_single_16a"``, etc.) whose truthy
        value ("true"/"1"/"yes"/"x") sets that box. Radio-style questions
        (16a/16b/19/21/25a/25b/26a/26b) are one key per choice — pass at most
        one truthy choice per question. Provenance (``source_ref``) is
        validated upstream; this renderer trusts the caller. A blank/empty
        value is skipped.

        Notes:
          - ``ssn``, ``spouse_ssn``, ``routing_number``, ``account_number`` are
            NEVER filled here even if supplied (identity/payment items for the
            taxpayer; security sensitive).
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form 9465 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f9465_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 9465 PDF not found at {source_path}. "
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
        if value is None or str(value).strip() == "":
            continue
        value = str(value)

        # Checkbox keys: a truthy value sets the box; anything else leaves it
        # /Off.
        if key in F9465_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F9465_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Identity/Payment items the taxpayer completes themselves — never
        # auto-filled.
        if key in TAXPAYER_COMPLETED_KEYS:
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = F9465_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
