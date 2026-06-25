"""Form 1120-S (U.S. Income Tax Return for an S Corporation) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute a return and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
S corporation's books / their documents give us onto the official form" surface,
not a tax-calculation engine.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another. A logical key with no AcroForm
mapping on the current form revision is simply skipped.

Left blank for the human
------------------------
The corporation's EIN, the officer signature / title, the "May the IRS discuss
this return with the preparer" authorization, and the paid-preparer block are
intentionally LEFT BLANK by this renderer even if data were available — they are
wet-ink / identity / preparer items the human must enter and sign on the final
form. They are not in ``f1120s_field_map`` (see that module's note).

XFA-hybrid form
---------------
Form 1120-S is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like the W-4P / 1040), so it is filled via ``form_prefill._render_acroform_pdf``
(the XFA-safe /Fields-tree walker that BAKES each value in as a flattened,
visible overlay so it renders in every viewer/printer), NOT the older page-based
``_fill_pdf_fields``.

Scope: page 1 (the return face) — identity header, income lines 1a–6, deduction
lines 7–22, Tax-and-Payments lines 23a–28b, and the page-1 checkboxes. The
supporting schedules (B / K / K-1 / L / M-1 / M-2) are not yet mapped and remain
blank for the preparer.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f1120s_fill import render_1120_s

    pdf_bytes, content_type = render_1120_s(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "corp_name":              {"value": "Acme Widgets S Corp", "source_ref": "intake#1", "origin": "user_entered"},
            "number_street":          {"value": "100 Main St",         "source_ref": "intake#1", "origin": "user_entered"},
            "city":                   {"value": "Columbia",            "source_ref": "intake#1", "origin": "user_entered"},
            "state":                  {"value": "SC",                  "source_ref": "intake#1", "origin": "user_entered"},
            "zip":                    {"value": "29201",               "source_ref": "intake#1", "origin": "user_entered"},
            "line_1a_gross_receipts": {"value": "5000000",  "source_ref": "books-2025", "origin": "document"},
            "line_6_total_income":    {"value": "4950000",  "source_ref": "books-2025", "origin": "computed"},
            "line_22_ordinary_income":{"value": "850000",   "source_ref": "books-2025", "origin": "computed"},
            "final_return":           {"value": "true",      "source_ref": "intake#1",  "origin": "user_entered"},
            "electing_s_corp":        {"value": "no",        "source_ref": "intake#1",  "origin": "user_entered"},
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
from app.exports.f1120s_field_map import (
    FORM_1120S_FIELDS,
    FORM_1120S_CHECKBOX_FIELDS,
    FORM_1120S_ELECTION_FIELDS,
    FORM_1120S_DEPOSIT_TYPE_FIELDS,
)

# Values that mark a boolean checkbox input as "checked".
_TRUTHY = {"true", "1", "yes", "y", "x", "on", "checked"}


def _f1120s_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 1120-S source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1120s_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1120s.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1120s_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1120s.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_1120_s(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 1120-S from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``f1120s_{tax_year}.pdf`` then ``f1120s.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``FORM_1120S_FIELDS`` (e.g.
        ``"corp_name"``, ``"line_1a_gross_receipts"``,
        ``"line_22_ordinary_income"``) OR one of the special keys:

          - ``"electing_s_corp"`` — item G; value ``"yes"`` or ``"no"``. Sets
            that checkbox.
          - ``"deposit_type"`` — line 28d; value ``"checking"`` or ``"savings"``.
            Sets that checkbox.
          - any key in ``FORM_1120S_CHECKBOX_FIELDS`` (``"sch_m3_attached"``,
            ``"final_return"``, ``"name_change"``, ``"address_change"``,
            ``"amended_return"``, ``"s_election_termination"``,
            ``"aggregated_activities"``, ``"grouped_activities"``,
            ``"form_2220_attached"``) — a boolean checkbox; set when its value is
            truthy ("true"/"1"/"yes"/"x"/"on"/"checked").

        Provenance (``source_ref``) is validated upstream; this renderer does
        not re-validate but trusts the caller. A blank/empty value is skipped.

        Notes:
          - The corporation's EIN, the officer signature/title, and the paid
            preparer block are NOT filled here even if supplied — they are
            wet-ink / identity / preparer items for the human.
          - A logical key with no AcroForm mapping (e.g. a supporting-schedule
            line not in ``FORM_1120S_FIELDS``) is skipped (conservative — never
            invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form 1120-S source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f1120s_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1120-S PDF not found at {source_path}. "
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

        if key == "electing_s_corp":
            mapped = FORM_1120S_ELECTION_FIELDS.get(value.strip().lower())
            if mapped:
                field_values[mapped[0]] = mapped[1]
            continue

        if key == "deposit_type":
            mapped = FORM_1120S_DEPOSIT_TYPE_FIELDS.get(value.strip().lower())
            if mapped:
                field_values[mapped[0]] = mapped[1]
            continue

        if key in FORM_1120S_CHECKBOX_FIELDS:
            # Boolean checkbox: set only when the value is truthy.
            if value.strip().lower() in _TRUTHY:
                fname, on_state = FORM_1120S_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = FORM_1120S_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
