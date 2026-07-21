"""Form 843 (Claim for Refund and Request for Abatement) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute the tax estimate and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

What Form 843 is
-----------------
Form 843 is used to claim a refund or request abatement of certain taxes,
penalties, and interest. It is not a general refund form (use 1040-X for that).
The fillable form is the official IRS form (Rev. December 2024 — the redesigned
revision: a 17-position reason-for-filing group on page 1, tax-type boxes a-g,
return-type boxes a-n and the explanation on page 2). See ``f843_field_map``
for the verified widget-by-widget layout.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another.

XFA-hybrid form
---------------
Form 843 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming),
so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that bakes
values in as a flattened, visible overlay), NOT the older page-based
``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f843_fill import render_843

    pdf_bytes, content_type = render_843(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name":         {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "ssn":          {"value": "123-45-6789", "source_ref": "intake#1", "origin": "user_entered"},
            "address":      {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city":         {"value": "Columbia", "source_ref": "intake#1", "origin": "user_entered"},
            "state":        {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "zip":          {"value": "29201", "source_ref": "intake#1", "origin": "user_entered"},
            "period_begin": {"value": "01/01/2025", "source_ref": "intake#1", "origin": "user_entered"},
            "period_end":   {"value": "12/31/2025", "source_ref": "intake#1", "origin": "user_entered"},
            "amount":       {"value": "12,000", "source_ref": "calc#1", "origin": "computed"},
            "explanation":  {"value": "I am claiming a refund because...", "source_ref": "intake#1", "origin": "user_entered"},
            # One reason-for-filing box (exclusive group, page 1 top):
            "filing_reason_penalty_reasonable_cause": {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
            # Line 4 tax type + line 7 request reason:
            "tax_type_income":         {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
            "reason_reasonable_cause": {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f843_field_map import (
    F843_FIELDS,
    F843_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
)

# Truthy strings that set a checkbox. Anything else (incl.
# "false"/"no"/blank) leaves the box /Off (conservative).
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}


def _f843_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 843 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f843_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f843.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f843_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f843.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_843(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 843 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f843_{tax_year}.pdf`` then
        ``f843.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F843_FIELDS`` (e.g. ``"name"``,
        ``"amount"``, ``"explanation"``, ``"irc_section"``) OR a checkbox key
        from ``F843_CHECKBOX_FIELDS`` (e.g. ``"tax_type_income"``,
        ``"reason_reasonable_cause"``, ``"filing_reason_penalty_reasonable_cause"``)
        whose truthy value ("true"/"1"/"yes"/"x") sets that box. Provenance
        (``source_ref``) is validated upstream; this renderer trusts the
        caller. A blank/empty value is skipped.

        Notes:
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field).
          - The ``filing_reason_*`` boxes (page 1 top) and the ``reason_*``
            line-7 boxes are each EXCLUSIVE one-of groups on the printed form:
            supply at most one truthy key per group.

    Raises
    ------
    FileNotFoundError
        If no Form 843 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f843_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 843 PDF not found at {source_path}. "
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

        # Checkbox keys: a truthy value sets the box; anything
        # else leaves it /Off.
        if key in F843_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F843_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Identity items the taxpayer completes themselves — never auto-filled.
        if key in TAXPAYER_COMPLETED_KEYS:
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = F843_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
