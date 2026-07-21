"""Form 7004 (Automatic Extension of Time To File Certain Business Income Tax Returns) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
compute the tax estimate and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

What Form 7004 is
-----------------
Form 7004 is used by certain business entities (e.g. corporations, partnerships,
trusts) to request an automatic extension of time to FILE their income tax return.
It does NOT extend the time to PAY — any payment made with the request is entered
on line 7 (total payments) and line 8 (balance due). The fillable form is the
entire single page.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another (e.g. line 8 balance due is taken
from the caller, not computed from lines 6 and 7).

Left blank for the human
------------------------
The filer's **identifying number** (EIN/SSN, logical key ``"ein"``) is
intentionally LEFT BLANK even if supplied, consistent with the 1040 / 1120 /
1120-S / 1065 / 4562 / 4868 renderers — identity numbers are the filer's own
entry.

XFA-hybrid form
---------------
Form 7004 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like the W-4P / 1040), so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that bakes
values in as a flattened, visible overlay), NOT the older page-based
``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f7004_fill import render_7004

    pdf_bytes, content_type = render_7004(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name":              {"value": "Acme Corp", "source_ref": "intake#1", "origin": "user_entered"},
            "address":           {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city_town":         {"value": "Columbia", "source_ref": "intake#1", "origin": "user_entered"},
            "state":             {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "zip_code":          {"value": "29201", "source_ref": "intake#1", "origin": "user_entered"},
            "form_code":         {"value": "12", "source_ref": "intake#1", "origin": "user_entered"},
            "calendar_year_yy":  {"value": "25", "source_ref": "intake#1", "origin": "user_entered"},
            "tentative_tax_6":   {"value": "12000", "source_ref": "calc#1", "origin": "computed"},
            "payments_7":        {"value": "9000", "source_ref": "w2#1", "origin": "document"},
            "balance_due_8":     {"value": "3000", "source_ref": "calc#1", "origin": "computed"},
            "foreign_corp_2":    {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f7004_field_map import (
    F7004_FIELDS,
    F7004_CHECKBOX_FIELDS,
    F7004_TAXPAYER_COMPLETED_KEYS,
)

# Truthy strings that set a checkbox. Anything else (incl. "false"/"no"/blank)
# leaves the box /Off (conservative).
_TRUE_VALUES = {"true", "1", "yes", "y", "x", "on", "checked"}


def _f7004_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 7004 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f7004_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f7004.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f7004_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f7004.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_7004(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 7004 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the keys present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f7004_{tax_year}.pdf`` then
        ``f7004.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``F7004_FIELDS`` (e.g. ``"name"``,
        ``"tentative_tax_6"``, ``"balance_due_8"``) OR a checkbox key from
        ``F7004_CHECKBOX_FIELDS`` (e.g. ``"foreign_corp_2"``,
        ``"short_year_initial"``) whose truthy value ("true"/"1"/"yes"/"x") sets
        that box. Provenance (``source_ref``) is validated upstream; this
        renderer trusts the caller. A blank/empty value is skipped.

        Notes:
          - ``"ein"`` (the identifying number) is NEVER filled here even if
            supplied — an identity item the filer enters themselves.
          - ``"form_code"`` is a convenience key: line 1 is printed as TWO
            single-digit comb boxes, so a 2-character code (e.g. "12" for
            Form 1120, "09" for Form 1065) is split across
            ``form_code_digit1`` / ``form_code_digit2``. A value that is not
            exactly 2 characters after trimming is skipped (conservative —
            never guess which box a malformed code belongs in). The digit keys
            may also be supplied directly.
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field).
          - Line 5b's five short-tax-year reason boxes are SEPARATE fields with
            distinct on-states; this renderer sets every box whose key arrives
            truthy and does not enforce mutual exclusivity — the printed form
            expects one reason, which upstream validation is responsible for.

    Raises
    ------
    FileNotFoundError
        If no Form 7004 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f7004_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 7004 PDF not found at {source_path}. "
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

        # Checkbox keys: a truthy value sets the box (with that box's own
        # on-state — line 5b's states differ per box); anything else leaves
        # it /Off.
        if key in F7004_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUE_VALUES:
                fname, on_state = F7004_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Identity items the filer completes themselves — never auto-filled.
        if key in F7004_TAXPAYER_COMPLETED_KEYS:
            continue

        # Line 1 convenience key: split a 2-digit form code across the two
        # single-digit comb boxes. A malformed (non-2-char) code is skipped
        # rather than guessed.
        if key == "form_code":
            code = value.strip()
            if len(code) == 2:
                field_values[F7004_FIELDS["form_code_digit1"]] = code[0]
                field_values[F7004_FIELDS["form_code_digit2"]] = code[1]
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = F7004_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"


__all__ = ["render_7004", "_f7004_form_path", "WatermarkMode"]
