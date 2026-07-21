"""Form W-9 (Request for TIN) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute or fabricate any figure. Each input value carries a ``source_ref``
(validated upstream, e.g. by ``app.api.work_products._assert_provenance``).

What Form W-9 is
-----------------
A form used by businesses to collect the TIN and certification from U.S.
persons (individuals, businesses, etc.) who are providing services. The form
is given to the requester (NOT the IRS).

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another.

Taxpayer-completed fields
-------------------------
The TIN (Part I) is intentionally LEFT BLANK by this renderer even if values
were supplied — it is an identity item the taxpayer must enter themselves.
(See ``TAXPAYER_COMPLETED_KEYS``).

XFA-hybrid form
---------------
Form W-9 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
with zero-padded indices like ``f1_01``), so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that bakes
values in as a flattened, visible overlay), NOT the older page-based
``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.fw9_fill import render_w9

    pdf_bytes, content_type = render_w9(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name": {"value": "John Doe", "source_ref": "intake#1", "origin": "user_entered"},
            "business_name": {"value": "Doe Consulting LLC", "source_ref": "intake#1", "origin": "user_entered"},
            "llc": {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
            "llc_classification": {"value": "P", "source_ref": "intake#1", "origin": "user_entered"},
            "address": {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city_state_zip": {"value": "Columbia, SC 29201", "source_ref": "intake#1", "origin": "user_entered"},
        },
    )

Note: Line 6 of the official form is ONE box ("City, state, and ZIP code"),
so the logical key is the suite-standard ``city_state_zip`` (same as the
W-4P / 843 / 941 / Schedule C maps). The requester box is likewise one
field: ``requester_name_address``.
"""

from __future__ import annotations

from pathlib import Path

from app.exports.form_prefill import (
    IRS_FORMS_DIR,
    WatermarkMode,
    _render_acroform_pdf,
)
from app.exports.fw9_field_map import (
    FW9_FIELDS,
    FW9_RADIO_FIELDS,
    FW9_CHECKBOX_FIELDS,
    FW9_LLC_CLASSIFICATION,
    FW9_OTHER_DETAIL,
)

# Truthy strings that set a line-3a classification box or line-3b checkbox.
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}

# Identity items the renderer never auto-fills, even if supplied — the taxpayer
# enters these themselves. (Form W-9 Part I TIN is entered by the taxpayer.)
# Includes the legacy ``tin_part*`` aliases so older callers' TIN inputs stay
# explicitly blocked rather than depending on unknown-key skipping.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    "ssn_part1", "ssn_part2", "ssn_part3", "ein_part1", "ein_part2",
    "tin_part1", "tin_part2", "tin_part3", "tin_part4", "tin_part5",
})


def _fw9_form_path(tax_year: int | None) -> Path:
    """Resolve the Form W-9 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/fw9_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/fw9.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"fw9_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "fw9.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_w9(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form W-9 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``fw9_{tax_year}.pdf`` then
        ``fw9.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``FW9_FIELDS`` (e.g. ``"name"``,
        ``"address"``, ``"city_state_zip"``, ``"requester_name_address"``), a
        classification key from ``FW9_RADIO_FIELDS``
        (``"individual_sole_prop"``, ``"c_corporation"``, ``"s_corporation"``,
        ``"partnership"``, ``"trust_estate"``, ``"llc"``, ``"other"``), or a
        checkbox key from ``FW9_CHECKBOX_FIELDS`` (``"foreign_status"``,
        line 3b) whose truthy value ("true"/"1"/"yes"/"x") checks that box.

        Notes:
          - ``llc_classification``: If "llc" is selected, this value (C, S, or P)
            is written to the LLC classification box.
          - ``other_detail``: If "other" is selected, this value is written to
            the other detail box.
          - ``city_state_zip`` is ONE box on the official form (line 6); pass
            the already-formatted "City, ST 12345" string, as with the W-4P.
          - ``requester_name_address`` is the single optional requester box to
            the right of lines 5-6. The baked overlay draws values on ONE
            line — keep this concise (~35 chars) or it will overflow the
            printed box.
          - Part I TIN keys (``ssn_part1``..``ssn_part3``,
            ``ein_part1``/``ein_part2``, legacy ``tin_part1``..``tin_part5``)
            are NEVER filled here even if supplied (identity items the
            taxpayer enters themselves).
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form W-9 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _fw9_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form W-9 PDF not found at {source_path}. "
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

        # Identity items the taxpayer completes themselves — never auto-filled.
        if key in TAXPAYER_COMPLETED_KEYS:
            continue

        # Classification boxes (Line 3a) and standalone checkboxes (Line 3b):
        # a truthy value sets the box to its own on-state ("1".."7" for the
        # 3a widgets — each widget has a distinct export value); anything
        # else leaves it unselected.
        if key in FW9_RADIO_FIELDS or key in FW9_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = (
                    FW9_RADIO_FIELDS.get(key) or FW9_CHECKBOX_FIELDS[key]
                )
                field_values[fname] = on_state
            continue

        # LLC classification detail (Line 3a)
        if key == "llc_classification":
            field_values[FW9_LLC_CLASSIFICATION] = value
            continue

        # Other detail (Line 3a)
        if key == "other_detail":
            field_values[FW9_OTHER_DETAIL] = value
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = FW9_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
