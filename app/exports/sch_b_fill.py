"""Schedule B (Form 1040) — Interest and Ordinary Dividends — form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute and does NOT fabricate any figure. Each input value carries a
``source_ref`` (validated upstream by ``app.api.work_products._assert_provenance``).
This is the honest "fill what the taxpayer / their 1099-INT / 1099-DIV give us
onto the official form" surface, not a tax-calculation engine.

Schedule B is attached to Form 1040 when a taxpayer has over $1,500 of taxable
interest or ordinary dividends (or a foreign account/trust). Its Part I line 4
total carries to Form 1040 line 2b and its Part II line 6 total to line 3b — but
this renderer writes only what the caller supplies; it never derives one line
from another.

Conservative blank-fill
-----------------------
Any line whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another. A logical key with no AcroForm
mapping is simply skipped.

Taxpayer-completed fields
-------------------------
The taxpayer's SSN (form field ``f1_02``) and the signature/date on the parent
Form 1040 are intentionally LEFT BLANK by this renderer even if data were
available — they are wet-ink / identity items the taxpayer must enter and sign
themselves. There is no EIN on Schedule B (it is an individual schedule).

XFA-hybrid form
---------------
Schedule B is a modern single-page XFA-hybrid AcroForm (``topmostSubform[0]``
field naming, like the W-4P / Form 1040), so it is filled via
``form_prefill._render_acroform_pdf`` — the XFA-safe /Fields-tree walker that
sets /V, BAKES each value in as a flattened, visible coordinate overlay (so it
renders in every viewer/printer, not just /NeedAppearances-honoring ones), and
stamps the status watermark.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.sch_b_fill import render_sch_b

    pdf_bytes, content_type = render_sch_b(
        tax_year=2025,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name_shown_on_return":   {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "interest_payer_1_name":  {"value": "Acme Credit Union", "source_ref": "1099int#1", "origin": "document"},
            "interest_payer_1_amount":{"value": "1234", "source_ref": "1099int#1", "origin": "document"},
            "interest_total_2":       {"value": "1234", "source_ref": "calc#1", "origin": "computed"},
            "interest_taxable_4":     {"value": "1234", "source_ref": "calc#1", "origin": "computed"},
            "dividend_payer_1_name":  {"value": "Vanguard", "source_ref": "1099div#1", "origin": "document"},
            "dividend_payer_1_amount":{"value": "2500", "source_ref": "1099div#1", "origin": "document"},
            "dividends_total_6":      {"value": "2500", "source_ref": "calc#1", "origin": "computed"},
            "foreign_account_7a":     {"value": "no", "source_ref": "intake#1", "origin": "user_entered"},
            "foreign_trust_8":        {"value": "no", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.sch_b_field_map import (
    SCHB_FIELDS,
    SCHB_YESNO_FIELDS,
)


def _sch_b_form_path(tax_year: int | None) -> Path:
    """Resolve the Schedule B source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1040sb_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1040sb.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1040sb_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1040sb.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_sch_b(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Schedule B (Form 1040) from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``f1040sb_{tax_year}.pdf`` then ``f1040sb.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``SCHB_FIELDS`` (e.g.
        ``"name_shown_on_return"``, ``"interest_payer_1_name"``,
        ``"interest_payer_1_amount"``, ``"interest_total_2"``,
        ``"interest_taxable_4"``, ``"dividend_payer_1_amount"``,
        ``"dividends_total_6"``, ``"foreign_country_7b"``) OR one of the Part III
        Yes/No question keys in ``SCHB_YESNO_FIELDS``:

          - ``"foreign_account_7a"`` — value ``"yes"`` / ``"no"``: financial
            interest in / signature authority over a foreign account.
          - ``"fincen_required_7a"`` — value ``"yes"`` / ``"no"``: required to
            file FinCEN Form 114 (FBAR).
          - ``"foreign_trust_8"`` — value ``"yes"`` / ``"no"``: foreign-trust
            distribution / grantor / transferor.

        Provenance (``source_ref``) is validated upstream; this renderer trusts
        the caller. A blank/empty value is skipped. Unknown keys are ignored
        (conservative — never invent a field). The taxpayer's SSN is NOT filled
        here even if supplied (a wet-ink/identity item).

    Raises
    ------
    FileNotFoundError
        If no Schedule B source PDF is present in ``data/irs_forms/``.
    """
    source_path = _sch_b_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Schedule B PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh (or fetch f1040sb.pdf) to provide it."
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

        # Part III Yes/No question: set exactly the chosen checkbox.
        question = SCHB_YESNO_FIELDS.get(key)
        if question is not None:
            mapped = question.get(value.strip().lower())
            if mapped:
                fname, on_state = mapped
                field_values[fname] = on_state
            continue

        # Ordinary text line: map the logical key to its AcroForm field name.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = SCHB_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"


__all__ = ["render_sch_b", "WatermarkMode"]
