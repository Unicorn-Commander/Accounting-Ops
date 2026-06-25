"""Schedule A (Form 1040) — Itemized Deductions — form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute itemized deductions and does NOT fabricate any figure. Each input
value carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

Schedule A is an ATTACHMENT to Form 1040 (it carries the total itemized
deductions to Form 1040 line 12). It is filed WITH the return to the IRS.

Conservative blank-fill
-----------------------
Any line whose input is absent (or blank) is left blank. We never guess, never
insert a zero, and never derive one line from another (e.g. we do not compute
the line-4 medical deduction from lines 1-3, the line-5d/5e SALT figures, or the
line-17 total — the caller supplies each figure it wants on the form). A logical
key with no AcroForm mapping is simply skipped.

Taxpayer-completed fields
-------------------------
The taxpayer's SSN (the page-1 ``f1_2[0]`` box) is intentionally LEFT BLANK by
this renderer — it is a wet-ink/identity item the taxpayer enters themselves.
``F1040SA_FIELDS`` deliberately omits it, so an "ssn" input is ignored. Line 8d
("Reserved for future use") is likewise not a writable key. There is no
signature line on Schedule A itself.

XFA-hybrid form
---------------
Schedule A is a modern XFA-hybrid AcroForm (single page, ``form1[0].Page1[0]``
field naming), so it is filled via ``form_prefill._render_acroform_pdf`` — the
XFA-safe /Fields-tree walker that sets each /V, BAKES the value in as a visible
flattened overlay at the widget /Rect (so it renders in EVERY viewer/printer, not
just /NeedAppearances-aware ones), flattens the widgets, and stamps the
status watermark.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.sch_a_fill import render_sch_a

    pdf_bytes, content_type = render_sch_a(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "taxpayer_name":           {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "salt_real_estate_5b":     {"value": "8200",  "source_ref": "prop-tax-2025", "origin": "document"},
            "salt_deduction_5e":       {"value": "10000", "source_ref": "calc#1", "origin": "computed"},
            "mortgage_interest_1098_8a": {"value": "12450", "source_ref": "1098-2025", "origin": "document"},
            "gifts_cash_11":           {"value": "3000",  "source_ref": "donations-2025", "origin": "document"},
            "total_itemized_17":       {"value": "33650", "source_ref": "calc#1", "origin": "computed"},
            "sales_tax_election_5a":   {"value": "true",  "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.sch_a_field_map import (
    F1040SA_FIELDS,
    F1040SA_CHECKBOX_FIELDS,
)

# Values (case-insensitive) that mean "check this box". Anything else
# (incl. "false", "no", "0", "off") leaves the box /Off — conservative.
_CHECKBOX_TRUTHY: frozenset[str] = frozenset(
    {"true", "1", "yes", "x", "checked", "on", "y"}
)


def _sch_a_form_path(tax_year: int | None) -> Path:
    """Resolve the Schedule A source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1040sa_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1040sa.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1040sa_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1040sa.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_sch_a(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Schedule A (Form 1040) from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``f1040sa_{tax_year}.pdf`` then ``f1040sa.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the 1040 / W-4P watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F1040SA_FIELDS`` (e.g.
        ``"medical_dental_1"``, ``"salt_deduction_5e"``, ``"total_itemized_17"``)
        OR one of the ``F1040SA_CHECKBOX_FIELDS`` keys
        (``"sales_tax_election_5a"``, ``"mortgage_use_box_8"``,
        ``"elect_itemize_18"``) whose value is truthy ("true"/"yes"/"x"/"1") to
        check the box.

        Provenance (``source_ref``) is validated upstream; this renderer does not
        re-validate but trusts the caller. A blank/empty value is skipped.

        Notes:
          - The taxpayer's SSN is NOT filled (the field is omitted from the map).
          - An unknown/unmapped key is skipped (never invents a field).

    Raises
    ------
    FileNotFoundError
        If no Schedule A source PDF is present in ``data/irs_forms/``.
    """
    source_path = _sch_a_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Schedule A PDF not found at {source_path}. "
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

        # Checkbox keys: set the box only on a truthy value (else leave /Off).
        checkbox = F1040SA_CHECKBOX_FIELDS.get(key)
        if checkbox is not None:
            if value.strip().lower() in _CHECKBOX_TRUTHY:
                fname, on_state = checkbox
                field_values[fname] = on_state
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize). Note: "ssn" is not
        # in F1040SA_FIELDS, so it is naturally ignored here.
        acro_name = F1040SA_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
