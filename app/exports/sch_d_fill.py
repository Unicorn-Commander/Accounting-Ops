"""IRS Schedule D (Form 1040) — Capital Gains and Losses — fill renderer.

Schedule D is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like
the W-4P and Form 1040). Its values, if set only via /V, are invisible in any
viewer that ignores /NeedAppearances (poppler, many print paths), so we reuse
``form_prefill._render_acroform_pdf`` — the XFA-safe filler that sets each
terminal field's semantic value AND bakes it in as a flattened, visible overlay
at the widget /Rect (renders in every viewer/printer, then flattens so nothing
double-renders) before stamping the status watermark.

This is a ``fill`` renderer (transcribe-not-compute): it stamps the figures the
taxpayer's Form 8949 / 1099-B / 1099-DA / Schedule(s) K-1 and the Schedule D
worksheets supply onto the official form. It does NOT compute the netting math
or the 28% Rate Gain / Unrecaptured §1250 / Capital Loss Carryover worksheets —
those amounts are the preparer's input. Figures are never invented; only
present, non-blank inputs are written (conservative blank-fill).

SSN ("Your social security number") and the signature are intentionally left
BLANK for the taxpayer to enter and sign. Loss lines 6, 14, and 21 are
pre-printed with parentheses on the form, so the caller passes the positive
magnitude of the loss.

Renderer contract (form_registry): ``render_sch_d(tax_year, watermark_mode,
inputs) -> (pdf_bytes, "application/pdf")``, where ``inputs`` is the
provenance-validated WorkProduct.inputs map
``{logical_key: {"value": str, "source_ref": str, "origin": str}}``.
"""

from __future__ import annotations

from pathlib import Path

from app.exports.form_prefill import (
    IRS_FORMS_DIR,
    WatermarkMode,
    _render_acroform_pdf,
)
from app.exports.sch_d_field_map import (
    SCH_D_CHOICE_FIELDS,
    SCH_D_FIELDS,
)


def _sch_d_form_path(tax_year: int | None) -> Path:
    """Resolve the Schedule D source PDF, preferring a year-specific file."""
    candidates = []
    if tax_year:
        candidates.append(IRS_FORMS_DIR / f"f1040sd_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1040sd.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def _norm_choice(raw: object) -> str | None:
    """Normalize a Yes/No-ish input value to ``"yes"`` / ``"no"`` (or None)."""
    t = str(raw).strip().lower()
    if t in ("yes", "y", "true", "t", "1"):
        return "yes"
    if t in ("no", "n", "false", "f", "0"):
        return "no"
    return None


def render_sch_d(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Render a filled Schedule D (Form 1040) PDF.

    Returns ``(pdf_bytes, "application/pdf")``. ``inputs`` is the
    provenance-validated map ``{logical_key: {"value": str, "source_ref": str,
    "origin": str}}``; only present, non-blank values are written (conservative
    blank-fill — figures are never invented). Text keys map onto AcroForm fields
    via ``SCH_D_FIELDS``; Yes/No questions map via ``SCH_D_CHOICE_FIELDS``
    (value normalized to yes/no -> the matching checkbox on-state).

    SSN and signature are intentionally left blank for the taxpayer.
    ``watermark_mode`` (DRAFT | SELF_CERTIFIED | NONE) selects the overlay
    stamped on the result. Raises FileNotFoundError if the source PDF is absent.
    """
    form_path = _sch_d_form_path(tax_year)
    if not form_path.exists():
        raise FileNotFoundError(
            f"IRS Schedule D PDF not found at {form_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source = form_path.read_bytes()

    field_values: dict[str, str] = {}

    # Plain-text lines: write only present, non-blank values.
    for key, field_name in SCH_D_FIELDS.items():
        rec = inputs.get(key)
        if not isinstance(rec, dict):
            continue
        raw = rec.get("value")
        if raw is None:
            continue
        text = str(raw).strip()
        if text == "":
            continue
        field_values[field_name] = text

    # Yes/No questions: normalize the value, then set the matching checkbox to
    # its on-state. An unrecognized or blank answer is skipped (conservative).
    for question, choices in SCH_D_CHOICE_FIELDS.items():
        rec = inputs.get(question)
        if not isinstance(rec, dict):
            continue
        raw = rec.get("value")
        if raw is None or str(raw).strip() == "":
            continue
        choice = _norm_choice(raw)
        if choice and choice in choices:
            field_name, on_state = choices[choice]
            field_values[field_name] = on_state

    return _render_acroform_pdf(source, field_values, watermark_mode), "application/pdf"


__all__ = ["render_sch_d", "WatermarkMode"]
