"""Form 1065 (U.S. Return of Partnership Income) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute a partnership return and does NOT fabricate any figure. Each input
value carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
partnership / their books give us onto the official form" surface, not a
tax-calculation engine.

Scope
-----
The renderer fills **page 1** (the return face): identity header, income lines
1a–8, deduction lines 9–22, tax-and-payment lines 23–32, and the page-1
checkboxes (box G return-type flags, box H accounting method, box J, box K, the
"may the IRS discuss" question, and the 32c account type). Schedule B / K / L /
M-1 / M-2 on the later pages are out of scope. See ``f1065_field_map`` for the
exact logical-key → AcroForm-field mapping.

Conservative blank-fill
-----------------------
Any line whose input is absent (or blank) is left blank. We never guess, never
insert a zero, and never derive one line from another. A logical key with no
mapping on the current form revision is simply skipped.

Filer-completed fields
----------------------
The partnership **EIN** and the signature/date are intentionally LEFT BLANK by
this renderer even if data were available — they are identity / wet-ink items
the filer must enter and sign themselves on the final form (conservative, like
the Form 1040 SSN). The paid-preparer block is likewise left blank.

XFA-hybrid form
---------------
Form 1065 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like the W-4P / 1040), so it is filled via ``form_prefill._render_acroform_pdf``
(the XFA-safe /Fields-tree walker that drops the XFA layer and BAKES each value
in as a flattened, visible overlay so it renders in every viewer/printer), NOT
the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f1065_fill import render_1065

    pdf_bytes, content_type = render_1065(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "partnership_name":   {"value": "Acme Holdings LP", "source_ref": "intake#1", "origin": "user_entered"},
            "accounting_method":  {"value": "accrual", "source_ref": "intake#1", "origin": "user_entered"},
            "num_schedules_k1":   {"value": "3", "source_ref": "intake#1", "origin": "user_entered"},
            "gross_receipts_1a":  {"value": "1250000", "source_ref": "books-2025", "origin": "document"},
            "total_income_8":     {"value": "480000", "source_ref": "books-2025", "origin": "document"},
            "total_deductions_22": {"value": "415000", "source_ref": "books-2025", "origin": "document"},
            "ordinary_business_income_23": {"value": "65000", "source_ref": "calc#1", "origin": "computed"},
            "initial_return":     {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f1065_field_map import (
    F1065_FIELDS,
    F1065_CHECKBOX_FIELDS,
    F1065_ACCOUNTING_METHOD_FIELDS,
    F1065_IRS_DISCUSS_FIELDS,
    F1065_ACCOUNT_TYPE_FIELDS,
)

# Special grouped-checkbox input keys: logical key -> the (choice -> (field,
# on-state)) group it selects exactly one of. e.g. {"accounting_method": "cash"}.
_GROUP_KEYS: dict[str, dict[str, tuple[str, str]]] = {
    "accounting_method": F1065_ACCOUNTING_METHOD_FIELDS,
    "irs_discuss":       F1065_IRS_DISCUSS_FIELDS,
    "account_type_32c":  F1065_ACCOUNT_TYPE_FIELDS,
}

# Strings that mean "set this independent boolean checkbox".
_TRUTHY = frozenset({"true", "1", "yes", "y", "on", "x", "checked"})


def _f1065_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 1065 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1065_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1065.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1065_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1065.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_1065(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 1065 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line/box is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f1065_{tax_year}.pdf`` then
        ``f1065.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 / 1120 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F1065_FIELDS`` (e.g.
        ``"gross_receipts_1a"``, ``"total_income_8"``,
        ``"ordinary_business_income_23"``) OR one of the special keys:

          - ``"accounting_method"`` — value is ``cash`` / ``accrual`` / ``other``
            (box H). Sets exactly that checkbox.
          - ``"irs_discuss"`` — value is ``yes`` / ``no`` (the preparer-discuss
            question). Sets that checkbox.
          - ``"account_type_32c"`` — value is ``checking`` / ``savings`` (line
            32c direct-deposit type). Sets that checkbox.
          - any key in ``F1065_CHECKBOX_FIELDS`` (e.g. ``"initial_return"``,
            ``"final_return"``, ``"amended_return"``, ``"schedules_c_m3_attached"``,
            ``"aggregated_activities_465"``, ``"grouped_activities_469"``) — a
            truthy value (``true`` / ``1`` / ``yes`` / ``x`` …) sets that
            independent box; anything else leaves it /Off.

        Provenance (``source_ref``) is validated upstream; this renderer trusts
        the caller. A blank/empty value is skipped.

        Notes:
          - The partnership EIN and the signature/date are NOT filled here even
            if supplied — they are identity/wet-ink items for the filer.
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form 1065 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f1065_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1065 PDF not found at {source_path}. "
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

        # Mutually-exclusive checkbox group (accounting method / IRS-discuss /
        # account type): the value names the choice to check.
        group = _GROUP_KEYS.get(key)
        if group is not None:
            mapped = group.get(value.strip().lower())
            if mapped:
                fname, on_state = mapped
                field_values[fname] = on_state
            continue

        # Independent boolean checkbox (box G flags, J, K): a truthy value sets it.
        boolean = F1065_CHECKBOX_FIELDS.get(key)
        if boolean is not None:
            if value.strip().lower() in _TRUTHY:
                fname, on_state = boolean
                field_values[fname] = on_state
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative).
        acro_name = F1065_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"


__all__ = ["render_1065", "_f1065_form_path", "WatermarkMode"]
