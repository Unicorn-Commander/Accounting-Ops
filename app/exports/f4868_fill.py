"""Form 4868 (Automatic Extension of Time To File) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute the tax estimate and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

What Form 4868 is
-----------------
The application an individual files with the IRS for an automatic 6-month
extension of time to FILE a 1040. It does NOT extend the time to PAY — line 7 is
the payment (if any) made with the request. The fillable form is the detachable
voucher at the bottom of page 1.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another (e.g. line 6 balance due is taken
from the caller, not computed from lines 4 and 5).

Taxpayer-completed fields
-------------------------
The taxpayer's SSN (line 2) and spouse's SSN (line 3) are intentionally LEFT
BLANK by this renderer even if values were supplied — they are identity items the
taxpayer must enter themselves. (Form 4868 has no signature line.) See
``TAXPAYER_COMPLETED_KEYS``.

XFA-hybrid form
---------------
Form 4868 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like the W-4P / 1040), so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that bakes
values in as a flattened, visible overlay), NOT the older page-based
``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f4868_fill import render_4868

    pdf_bytes, content_type = render_4868(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name":            {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "address":         {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city_town":       {"value": "Columbia", "source_ref": "intake#1", "origin": "user_entered"},
            "state":           {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "zip_code":        {"value": "29201", "source_ref": "intake#1", "origin": "user_entered"},
            "tax_liability_4": {"value": "12000", "source_ref": "calc#1", "origin": "computed"},
            "payments_5":      {"value": "9000", "source_ref": "w2#1", "origin": "document"},
            "balance_due_6":   {"value": "3000", "source_ref": "calc#1", "origin": "computed"},
            "amount_paying_7": {"value": "3000", "source_ref": "intake#1", "origin": "user_entered"},
            "out_of_country_8": {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f4868_field_map import (
    F4868_FIELDS,
    F4868_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
)

# Truthy strings that set a line-8 / line-9 checkbox. Anything else (incl.
# "false"/"no"/blank) leaves the box /Off (conservative).
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}


def _f4868_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 4868 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f4868_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f4868.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f4868_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f4868.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_4868(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 4868 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f4868_{tax_year}.pdf`` then
        ``f4868.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F4868_FIELDS`` (e.g. ``"name"``,
        ``"tax_liability_4"``, ``"amount_paying_7"``) OR a checkbox key from
        ``F4868_CHECKBOX_FIELDS`` (``"out_of_country_8"`` /
        ``"form_1040nr_9"``) whose truthy value ("true"/"1"/"yes"/"x") sets that
        box. Provenance (``source_ref``) is validated upstream; this renderer
        trusts the caller. A blank/empty value is skipped.

        Notes:
          - ``your_ssn`` / ``spouse_ssn`` are NEVER filled here even if supplied
            (identity items for the taxpayer; Form 4868 has no signature line).
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field).

    Raises
    ------
    FileNotFoundError
        If no Form 4868 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f4868_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 4868 PDF not found at {source_path}. "
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

        # Checkbox keys (lines 8 & 9): a truthy value sets the box; anything
        # else leaves it /Off.
        if key in F4868_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F4868_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Identity items the taxpayer completes themselves — never auto-filled.
        if key in TAXPAYER_COMPLETED_KEYS:
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = F4868_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
