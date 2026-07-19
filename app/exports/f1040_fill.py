"""Form 1040 (U.S. Individual Income Tax Return) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute a tax return and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another. A logical key with no AcroForm
mapping on the current form revision (e.g. line 24 "total tax", which the
TY2025 f1040.pdf has no fillable box for) is simply skipped.

Taxpayer-completed fields
-------------------------
The taxpayer's SSN and the signature/date are intentionally LEFT BLANK by this
renderer even if data were available — they are wet-ink / identity items the
taxpayer must enter and sign themselves on the final form.

XFA-hybrid form
---------------
Form 1040 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like the W-4P), so it is filled via ``form_prefill._render_acroform_pdf`` (the
XFA-safe /Fields-tree walker that drops the XFA layer and sets
/NeedAppearances), NOT the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f1040_fill import render_1040

    pdf_bytes, content_type = render_1040(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "first_name_mi":           {"value": "Aaron J", "source_ref": "intake#1", "origin": "user_entered"},
            "last_name":               {"value": "Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "filing_status":           {"value": "mfj", "source_ref": "intake#1", "origin": "user_entered"},
            "wages_1a":                {"value": "85000", "source_ref": "w2-2025-acme", "origin": "document"},
            "agi_11":                  {"value": "85000", "source_ref": "calc#1", "origin": "computed"},
            "federal_withholding_25a": {"value": "9500", "source_ref": "w2-2025-acme", "origin": "document"},
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
from app.exports.f1040_field_map import (
    F1040_FIELDS,
    F1040_FILING_STATUS_FIELDS,
    F1040_DIGITAL_ASSET_FIELDS,
)


def _f1040_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 1040 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1040_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1040.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1040_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1040.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_1040(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 1040 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``f1040_{tax_year}.pdf`` then ``f1040.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1120 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F1040_FIELDS`` (e.g. ``"wages_1a"``,
        ``"agi_11"``, ``"federal_withholding_25a"``) OR one of the special keys:

          - ``"filing_status"`` — value is one of the
            ``F1040_FILING_STATUS_FIELDS`` keys: ``single``, ``mfj``, ``mfs``,
            ``hoh``, ``qss``. Sets exactly that checkbox.
          - ``"digital_assets"`` — value is ``"yes"`` or ``"no"``. Sets that
            checkbox on the digital-asset question.

        Provenance (``source_ref``) is validated upstream; this renderer does
        not re-validate but trusts the caller. A blank/empty value is skipped.

        Notes:
          - The taxpayer's SSN and the signature/date are NOT filled here even
            if supplied — they are wet-ink/identity items for the taxpayer.
          - A logical key with no AcroForm mapping on this form revision (e.g.
            line 24 "total tax", which has no fillable box on TY2025) is skipped.

    Raises
    ------
    FileNotFoundError
        If no Form 1040 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f1040_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1040 PDF not found at {source_path}. "
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

        if key == "filing_status":
            status = value.strip().lower()
            mapped = F1040_FILING_STATUS_FIELDS.get(status)
            if mapped:
                fname, on_state = mapped
                field_values[fname] = on_state
            continue

        if key == "digital_assets":
            answer = value.strip().lower()
            mapped = F1040_DIGITAL_ASSET_FIELDS.get(answer)
            if mapped:
                fname, on_state = mapped
                field_values[fname] = on_state
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = F1040_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
