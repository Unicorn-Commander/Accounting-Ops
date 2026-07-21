"""Form 2848 (Power of Attorney and Declaration of Representative) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute anything and does NOT fabricate any figure. Each input value carries a
``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what
the taxpayer / their documents give us onto the official form" surface.

What Form 2848 is
-----------------
Form 2848 authorizes one or more eligible representatives (CPA, EA, attorney,
…) to REPRESENT the taxpayer before the IRS — receive confidential tax
information AND perform the acts listed on the form (e.g. respond in an audit).
It is processed by the IRS CAF unit. It is broader than Form 8821, which only
authorizes information access. The form is legally inert until the taxpayer
signs line 7 and each representative signs the Part II declaration — both of
which this renderer always leaves blank.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another. Unknown keys are silently
skipped.

Taxpayer/representative-completed fields
----------------------------------------
The line 7 taxpayer signature block (signature, date, title, print name) and
the Part II representative signature/date columns are intentionally LEFT BLANK
even if values were supplied — they are identity/attestation items the signers
must enter themselves. See ``TAXPAYER_COMPLETED_KEYS`` in the field map; the
corresponding widgets are additionally absent from the field maps, so they can
never be written.

Shared-box conveniences (see ``f2848_field_map`` for details)
-------------------------------------------------------------
- ``taxpayer_address`` + ``taxpayer_city_state_zip`` share the form's single
  two-row address box; when both are present they are joined as
  ``"<street>, <city_state_zip>"``.
- ``taxpayer_ssn_ein`` routes by format: ``NN-NNNNNNN`` goes to the EIN
  sub-box, anything else to the SSN sub-box. Explicit ``taxpayer_ssn`` /
  ``taxpayer_itin`` / ``taxpayer_ein`` keys win over the combined key.

XFA-hybrid form
---------------
Form 2848 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming),
so it is filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe
/Fields-tree walker that bakes values in as a flattened, visible overlay), NOT
the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f2848_fill import render_2848

    pdf_bytes, content_type = render_2848(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "taxpayer_name":           {"value": "John Doe", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_address":        {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_city_state_zip": {"value": "Columbia, SC 29201", "source_ref": "intake#1", "origin": "user_entered"},
            "taxpayer_ssn_ein":        {"value": "123-45-6789", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_name":               {"value": "Jane Smith, CPA", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_address":            {"value": "456 Oak Ave, Columbia, SC 29201", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_caf":                {"value": "1234-56789R", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_ptin":               {"value": "P01234567", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_telephone":          {"value": "555-1234", "source_ref": "intake#1", "origin": "user_entered"},
            "acts_description_1":      {"value": "Income", "source_ref": "intake#1", "origin": "user_entered"},
            "acts_form_number_1":      {"value": "1040", "source_ref": "intake#1", "origin": "user_entered"},
            "acts_years_1":            {"value": "2024", "source_ref": "intake#1", "origin": "user_entered"},
            "access_records":          {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_designation":        {"value": "b", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_jurisdiction":       {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "rep1_bar_number":         {"value": "12345", "source_ref": "intake#1", "origin": "user_entered"},
        },
    )
"""

from __future__ import annotations

import re
from pathlib import Path

from app.exports.form_prefill import (
    IRS_FORMS_DIR,
    WatermarkMode,
    _render_acroform_pdf,
)
from app.exports.f2848_field_map import (
    F2848_FIELDS,
    F2848_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
)

# Truthy strings that set a checkbox. Anything else (incl. "false"/"no"/blank)
# leaves the box /Off (conservative).
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}

# The single physical taxpayer-address box holds street + city/state/ZIP; when
# both logical keys are present the parts are joined in this order.
_ADDRESS_PART_KEYS = ("taxpayer_address", "taxpayer_city_state_zip")

# Combined-TIN convenience key: routed by format (see _route_taxpayer_tin).
_TIN_COMBINED_KEY = "taxpayer_ssn_ein"
_EIN_RE = re.compile(r"\d{2}-\d{7}")


def _f2848_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 2848 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f2848_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f2848.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f2848_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f2848.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def _clean_value(inputs: dict, key: str) -> str:
    """Extract a non-blank string value for ``key`` from provenance payloads.

    Returns ``""`` when the key is absent, malformed, or blank.
    """
    payload = inputs.get(key)
    if not isinstance(payload, dict):
        return ""
    value = payload.get("value")
    if value is None:
        return ""
    return str(value).strip()


def _route_taxpayer_tin(value: str) -> str:
    """Pick the ID sub-box for the combined ``taxpayer_ssn_ein`` key.

    The printed form has one caption ("Taxpayer identification number(s)")
    over three sub-boxes. An EIN-formatted value (``NN-NNNNNNN``) goes to the
    EIN sub-box; everything else (SSN-formatted or ambiguous) goes to the
    leftmost SSN sub-box. Callers that know the TIN kind should use the
    explicit ``taxpayer_ssn`` / ``taxpayer_itin`` / ``taxpayer_ein`` keys.
    """
    if _EIN_RE.fullmatch(value):
        return F2848_FIELDS["taxpayer_ein"]
    return F2848_FIELDS["taxpayer_ssn"]


def render_2848(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 2848 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled). The output is flattened and watermarked per house rules.

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f2848_{tax_year}.pdf``
        then ``f2848.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 / 8821 watermark
        behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F2848_FIELDS`` (e.g.
        ``"taxpayer_name"``, ``"rep1_caf"``, ``"acts_years_1"``) OR a checkbox
        key from ``F2848_CHECKBOX_FIELDS`` (e.g. ``"access_records"``) whose
        truthy value ("true"/"1"/"yes"/"x") sets that box. Provenance
        (``source_ref``) is validated upstream; this renderer trusts the
        caller. A blank/empty value is skipped.

        Notes:
          - Signature-block keys (``signature_taxpayer``, ``signature_date``,
            ``signature_print_name``, ``signature_representative``,
            ``rep{N}_signature``, …) are NEVER filled here even if supplied —
            the signers complete them by hand.
          - An unknown / unmapped key is silently skipped (conservative —
            never invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form 2848 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f2848_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 2848 PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}

    # --- Shared-box conveniences (handled before the generic loop) ---------
    # 1) Street + city/state/ZIP share ONE physical box; join present parts in
    #    printed order.
    address_parts = [v for v in (_clean_value(inputs, k) for k in _ADDRESS_PART_KEYS) if v]
    if address_parts:
        field_values[F2848_FIELDS["taxpayer_address"]] = ", ".join(address_parts)

    # 2) Combined TIN key routes by format. Applied first so the explicit
    #    taxpayer_ssn / taxpayer_itin / taxpayer_ein keys (generic loop below)
    #    win when both are supplied.
    combined_tin = _clean_value(inputs, _TIN_COMBINED_KEY)
    if combined_tin:
        field_values[_route_taxpayer_tin(combined_tin)] = combined_tin

    _SPECIAL_KEYS = set(_ADDRESS_PART_KEYS) | {_TIN_COMBINED_KEY}

    for key, payload in inputs.items():
        # Each input payload is a dict with at least a "value"; provenance was
        # validated upstream. A missing/blank value means "leave it blank".
        if key in _SPECIAL_KEYS:
            continue
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value).strip() == "":
            continue
        value = str(value)

        # Checkbox keys: a truthy value sets the box; anything else leaves it
        # /Off.
        if key in F2848_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F2848_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Identity / attestation items the signers complete themselves — never
        # auto-filled.
        if key in TAXPAYER_COMPLETED_KEYS:
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = F2848_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"


__all__ = ["render_2848", "_f2848_form_path", "WatermarkMode"]
