"""Schedule K-1 (Form 1065) — Partner's Share of Income, Deductions, Credits,
etc. — form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute a partner's distributive share and does NOT fabricate any figure.
Each input value carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
partnership's books / the prepared Form 1065 give us onto the official K-1"
surface, not a tax-calculation engine.

The Schedule K-1 is issued BY the partnership TO each partner and is attached to
the partnership's Form 1065; the partner then uses it to prepare their own
return. It has NO signature line.

Conservative blank-fill
-----------------------
Any box whose input is absent (or blank) is left blank. We never guess, never
insert a zero, and never derive one line from another. A logical key with no
AcroForm mapping is simply skipped.

Identity numbers left blank for the human
-----------------------------------------
The partnership's EIN (Part I A), the partner's SSN or TIN (Part II E), and a
disregarded-entity partner's TIN (Part II H2) are intentionally LEFT BLANK by
this renderer even if data were supplied — they are sensitive identity items the
preparer completes themselves (matching the SSN/EIN handling on the 1040 / 1065
paths). They are not present in the field map, so passing them is a no-op. The
form has no signature field.

XFA-hybrid form
---------------
Schedule K-1 (Form 1065) is a modern XFA-hybrid AcroForm (``topmostSubform[0]``
field naming, like the W-4P / 1040 / 1065), so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that sets
/V, BAKES each value in as a flattened visible coordinate overlay so it renders
in every viewer/printer, and drops the XFA layer), NOT the older page-based
``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.k1_1065_fill import render_k1_1065

    pdf_bytes, content_type = render_k1_1065(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "partnership_name_address":     {"value": "Acme Holdings LP\\n1 Main St, Aspen CO 81611", "source_ref": "f1065-2025", "origin": "document"},
            "partner_name_address":         {"value": "Aaron J Stransky\\n2 Oak Ave, Aspen CO 81611", "source_ref": "intake#1", "origin": "user_entered"},
            "general_partner":              {"value": "true", "source_ref": "partnership-agmt", "origin": "document"},
            "domestic_partner":             {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
            "box1_ordinary_business_income":{"value": "50000", "source_ref": "f1065-k", "origin": "computed"},
            "box14_code":                   {"value": "A", "source_ref": "f1065-k", "origin": "computed"},
            "box14_amount":                 {"value": "50000", "source_ref": "f1065-k", "origin": "computed"},
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
from app.exports.k1_1065_field_map import (
    K1_1065_CHECKBOX_FIELDS,
    K1_1065_FIELDS,
)

# Strings (case-insensitive) that mark a checkbox as "checked".
_TRUTHY = frozenset({"true", "1", "yes", "y", "x", "checked", "on"})


def _k1_1065_form_path(tax_year: int | None) -> Path:
    """Resolve the Schedule K-1 (Form 1065) source PDF, preferring a year file.

    Tries ``data/irs_forms/f1065sk1_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1065sk1.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1065sk1_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1065sk1.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_k1_1065(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Schedule K-1 (Form 1065) from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the boxes present in
    ``inputs``; every other box is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``f1065sk1_{tax_year}.pdf`` then ``f1065sk1.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 / 1065 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``K1_1065_FIELDS`` (a text/amount box
        such as ``"box1_ordinary_business_income"``, ``"capital_ending"``,
        ``"box20_code"``) OR a checkbox key from ``K1_1065_CHECKBOX_FIELDS`` (such
        as ``"final_k1"``, ``"general_partner"``, ``"box16_k3_attached"``, whose
        value is interpreted as truthy/falsey — only a truthy value checks the
        box; the mutually-exclusive radio groups G/H1/M are independent keys, set
        exactly the one you mean).

        Provenance (``source_ref``) is validated upstream; this renderer does not
        re-validate but trusts the caller. A blank/empty value is skipped.

        Notes:
          - The partnership EIN, the partner SSN/TIN, and a DE partner's TIN are
            NOT fillable here (not in the map) — identity items for the preparer.
            The Schedule K-1 has no signature line.
          - An unknown / unmapped key is skipped (never invent a field).

    Raises
    ------
    FileNotFoundError
        If no Schedule K-1 (Form 1065) source PDF is present in
        ``data/irs_forms/``.
    """
    source_path = _k1_1065_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Schedule K-1 (Form 1065) PDF not found at {source_path}. "
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

        # Checkboxes / radio options: only a truthy value checks the box; a
        # falsey value is left /Off (conservative).
        checkbox = K1_1065_CHECKBOX_FIELDS.get(key)
        if checkbox is not None:
            if value.strip().lower() in _TRUTHY:
                fname, on_state = checkbox
                field_values[fname] = on_state
            continue

        # Ordinary text / amount box: map the logical key to its AcroForm field
        # name. Unknown / unmapped keys are skipped (conservative — never invent
        # a field, never write to a box we don't recognize).
        acro_name = K1_1065_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
