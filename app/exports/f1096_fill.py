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
zero, and never derive one line from another. Unknown keys are skipped.

Box 6 constraint
----------------
Form 1096 instructs "File a separate Form 1096 for each type of return" —
only ONE box-6 checkbox may be set per transmittal. If multiple truthy box-6
keys are provided, the renderer raises a ValueError.

Address line
------------
The 2024 revision has ONE combined "City or town, state or province, country,
and ZIP or foreign postal code" box (logical key ``city_state_zip``). For
callers that supply the legacy separate ``city`` / ``state`` / ``zip_code``
keys, those caller-provided parts are joined ("City, ST 12345") into that box —
pure transcription of supplied values, never derivation. There is no separate
``address_line2`` box on this revision.

XFA-hybrid form
---------------
Form 1096 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming),
so it is filled via ``form_prefill._render_acroform_pdf`` (visible baked
overlay + flatten + watermark). The form itself is PAGE INDEX 1 of the 3-page
official file.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f1096_fill import render_1096

    pdf_bytes, content_type = render_1096(
        tax_year=2024,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "filer_name":        {"value": "Acme Corp", "source_ref": "intake#1", "origin": "user_entered"},
            "address_line1":     {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city_state_zip":    {"value": "Columbia, SC 29201", "source_ref": "intake#1", "origin": "user_entered"},
            "ein":               {"value": "12-3456789", "source_ref": "intake#1", "origin": "user_entered"},
            "box_3_total_forms": {"value": "10", "source_ref": "calc#1", "origin": "computed"},
            "box_4_withheld":    {"value": "500.00", "source_ref": "calc#1", "origin": "computed"},
            "box_5_total_amount":{"value": "10000.00", "source_ref": "calc#1", "origin": "computed"},
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
)

# Truthy strings that set a checkbox.
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}

# Legacy separate-location keys accepted for compatibility and joined into the
# single combined city/state/ZIP box (in this order).
_LEGACY_LOCATION_KEYS = ("city", "state", "zip_code")


def _f1096_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 1096 source PDF, preferring a year-specific file.

    The repo currently carries ``f1096_2024.pdf`` (the 2024 revision). A tax
    year with no matching ``f1096_<year>.pdf`` and no generic ``f1096.pdf``
    raises FileNotFoundError from ``render_1096`` — we never silently render
    onto a different year's printed form.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1096_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1096.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def _payload_value(payload: object) -> str | None:
    """Extract a non-blank string value from a provenance payload, else None."""
    if not isinstance(payload, dict):
        return None
    value = payload.get("value")
    if value is None or str(value).strip() == "":
        return None
    return str(value)


def _combined_city_state_zip(inputs: dict) -> str | None:
    """Join caller-provided legacy city/state/zip parts: "City, ST 12345".

    Pure transcription — only parts the caller actually supplied are used.
    Returns None when none of the legacy keys carry a value.
    """
    city = _payload_value(inputs.get("city"))
    state = _payload_value(inputs.get("state"))
    zip_code = _payload_value(inputs.get("zip_code"))
    state_zip = " ".join(p for p in (state, zip_code) if p)
    parts = [p for p in (city, state_zip) if p]
    return ", ".join(parts) if parts else None


def render_1096(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 1096 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank. Unknown
    keys are skipped (conservative — we never write into a box we do not
    recognize, and never over a preprinted form value).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f1096_<year>.pdf``,
        falling back to ``f1096.pdf``). May be ``None``.
    watermark_mode:
        DRAFT, SELF_CERTIFIED, or NONE.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}``.
        Text keys: see ``f1096_field_map.F1096_FIELDS`` (``filer_name``,
        ``address_line1``, ``city_state_zip``, ``contact_person``, ``phone``,
        ``email``, ``fax``, ``ein``, ``ssn``, ``box_3_total_forms``,
        ``box_4_withheld``, ``box_5_total_amount``, ``signer_title``).
        Legacy ``city`` / ``state`` / ``zip_code`` are joined into
        ``city_state_zip`` when that key itself is absent.
        Box 6 keys (``box_6_*``): at most one may be truthy.

    Raises
    ------
    FileNotFoundError
        If no Form 1096 source PDF is present for the requested year.
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
        value = _payload_value(payload)
        if value is None:
            continue

        # Box 6 checkboxes (each has its own unique on-state)
        if key in F1096_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F1096_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
                box6_checked.append(key)
            continue

        # Ordinary text line (unknown keys skipped)
        acro_name = F1096_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    # Legacy separate city/state/zip -> combined box, only when the caller did
    # not supply city_state_zip itself.
    combined_field = F1096_FIELDS["city_state_zip"]
    if combined_field not in field_values:
        combined = _combined_city_state_zip(inputs)
        if combined:
            field_values[combined_field] = combined

    # Enforce single Box 6 checkbox
    if len(box6_checked) > 1:
        raise ValueError(
            f"Form 1096 Box 6 allows only one form type per transmittal. Found: {box6_checked}"
        )

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"


__all__ = ["render_1096", "_f1096_form_path", "WatermarkMode"]
