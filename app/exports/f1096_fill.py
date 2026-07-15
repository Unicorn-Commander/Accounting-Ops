"""Form 1096 (Annual Summary and Transmittal of U.S. Information Returns) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute totals and does NOT fabricate any figure. Each input value carries a
``source_ref`` (validated upstream, e.g. by ``app.api.work_products._assert_provenance``).
This is the honest "fill what the taxpayer / their documents give us onto the official form" surface.

FILING CONSTRAINT
-----------------
The downloadable Form 1096 Copy A is NOT filable — the IRS requires the official
scannable red-ink version (ordered from the IRS). This renderer produces a
PREPARATION/RECORD copy the filer transcribes from or keeps; it must never be
presented as a filable artifact.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another.

Box 6 Constraint
----------------
Only ONE box-6 checkbox may be set per transmittal. If multiple are provided,
the renderer will raise a ValueError.

XFA-hybrid form
---------------
Form 1096 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming),
so it is filled via ``form_prefill._render_acroform_pdf``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f1096_fill import render_1096

    pdf_bytes, content_type = render_1096(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "filer_name":        {"value": "Acme Corp", "source_ref": "intake#1", "origin": "user_entered"},
            "address_line1":     {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city":              {"value": "Columbia", "source_ref": "intake#1", "origin": "user_entered"},
            "state":             {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "zip_code":          {"value": "29201", "source_ref": "intake#1", "origin": "user_entered"},
            "ein":               {"value": "12-3456789", "source_ref": "intake#1", "origin": "user_entered"},
            "box_3_total_forms": {"value": "10", "source_ref": "calc#1", "origin": "computed"},
            "box_4_withheld":    {"value": "500", "source_ref": "calc#1", "origin": "computed"},
            "box_5_total_amount":{"value": "10000", "source_ref": "calc#1", "origin": "computed"},
            "box_6_1099_nec":    {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f1096_field_map import (
    F1096_FIELDS,
    F1096_CHECKBOX_FIELDS,
    F1096_FINAL_RETURN_KEY,
    F1096_FINAL_RETURN_FIELD,
    VALID_F1096_FIELDS,
)

# Truthy strings that set a checkbox.
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}


def _f1096_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 1096 source PDF, preferring a year-specific file."""
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1096_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1096.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_1096(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 1096 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank.

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF. May be ``None``.
    watermark_mode:
        DRAFT, SELF_CERTIFIED, or NONE.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}``.
        Box 6 keys must have exactly one truthy value.

    Raises
    ------
    FileNotFoundError
        If no Form 1096 source PDF is present.
    ValueError
        If multiple Box 6 checkboxes are set.
    """
    source_path = _f1096_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1096 PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}
    box6_checked: list[str] = []

    for key, payload in inputs.items():
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value) == "":
            continue
        value = str(value)

        # Box 6 checkboxes
        if key in F1096_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F1096_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
                box6_checked.append(key)
            continue

        # Final return checkbox
        if key == F1096_FINAL_RETURN_KEY:
            if value.strip().lower() in _TRUE_VALUES:
                field_values[F1096_FINAL_RETURN_FIELD] = "1"
            continue

        # Ordinary text line
        acro_name = F1096_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    # Enforce single Box 6 checkbox
    if len(box6_checked) > 1:
        raise ValueError(
            f"Form 1096 Box 6 allows only one form type. Found: {box6_checked}"
        )

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
