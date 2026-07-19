"""CA Form 540 (California Resident Income Tax Return) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FTB FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute a return and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
taxpayer / their documents give us onto the official form" surface, not a
tax-calculation engine.

Conservative blank-fill
-----------------------
Any line whose input is absent (or blank) is left blank. We never guess, never
insert a zero, and never derive one line from another. A logical key with no
mapping is simply skipped.

Taxpayer-completed fields
-------------------------
The taxpayer's / spouse's / dependents' SSN-or-ITIN and the Side-6 wet-ink
signature block are intentionally LEFT BLANK by this renderer even if data were
available — they are identity / signature items the taxpayer must enter and sign
themselves on the final form. (They are not in ``ca_540_field_map``.)

How the 2024 Form 540 is filled
-------------------------------
The 2024 Form 540 is a 6-page fillable AcroForm with FLAT field names (no XFA).
Two of its three control types fill cleanly through
``form_prefill._render_acroform_pdf`` (set ``/V`` + bake a flattened, visible
overlay so values render in EVERY viewer/printer):

  * text/amount lines  — ``"540-NNNN"`` (``CA_540_FIELDS``)
  * independent checkboxes — ``"540-NNNN CB"`` on-state ``/Yes``
    (``CA_540_CHECKBOX_FIELDS``)

The third type — filing status and a few either/or choices — is each a SINGLE
radio group whose option widgets ALL share the parent's qualified name. Marking
such a name through the value-overlay would stamp an "X" on EVERY option, so
those are handled here by a per-option coordinate overlay
(``_stamp_radio_marks``) that draws a single "X" in the chosen option's widget
rect (from ``CA_540_RADIO_FIELDS``) on top of the filled form.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.ca_540_fill import render_ca_540

    pdf_bytes, content_type = render_ca_540(
        tax_year=2024,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "first_name":            {"value": "Aaron", "source_ref": "intake#1", "origin": "user_entered"},
            "last_name":             {"value": "Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "filing_status":         {"value": "mfj", "source_ref": "intake#1", "origin": "user_entered"},
            "line13_federal_agi":    {"value": "85000", "source_ref": "1040-2024", "origin": "document"},
            "line19_taxable_income": {"value": "72000", "source_ref": "calc#1", "origin": "computed"},
            "line71_ca_withholding": {"value": "3200", "source_ref": "w2-2024-acme", "origin": "document"},
        },
    )
"""

from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.exports.form_prefill import (
    WatermarkMode,
    _render_acroform_pdf,
)
from app.exports.ca_540_field_map import (
    CA_540_FIELDS,
    CA_540_CHECKBOX_FIELDS,
    CA_540_RADIO_FIELDS,
)

# Official CA state-form PDFs live alongside the IRS ones, in data/state_forms/.
STATE_FORMS_DIR = Path(__file__).parent.parent.parent / "data" / "state_forms"

# Values a checkbox treats as "check this box"; anything else leaves it /Off.
_TRUTHY = frozenset({"true", "1", "yes", "y", "x", "on", "checked"})


def _ca540_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 540 source PDF, preferring a year-specific file.

    Tries ``data/state_forms/ca_540_{tax_year}.pdf`` first (when a tax year is
    given), then the bundled ``ca_540_2024.pdf``, then an unversioned
    ``ca_540.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(STATE_FORMS_DIR / f"ca_540_{tax_year}.pdf")
    candidates.append(STATE_FORMS_DIR / "ca_540_2024.pdf")
    candidates.append(STATE_FORMS_DIR / "ca_540.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def _stamp_radio_marks(
    pdf_bytes: bytes,
    marks: list[tuple[int, tuple[float, float, float, float]]],
) -> bytes:
    """Draw a single "X" in each (page_index, rect) widget box.

    Used to mark exactly one option of a shared-name radio group (filing status,
    direct-deposit type, …) — which the value-overlay cannot do by name. Mirrors
    the checkbox-mark geometry in ``form_prefill._build_value_overlay`` so the
    radio "X" matches the baked checkbox "X". Page sizes are read from the
    (already flattened) input so the overlay aligns to each page.
    """
    from reportlab.lib.colors import black
    from reportlab.pdfgen import canvas as rl_canvas

    # Clone into a writer first, then merge onto its pages — the robust pypdf
    # path (mirrors form_prefill._render_acroform_pdf; avoids the deprecated
    # merge-onto-a-detached-reader-page behavior).
    writer = PdfWriter(clone_from=io.BytesIO(pdf_bytes))
    by_page: dict[int, list[tuple[float, float, float, float]]] = {}
    for page_index, rect in marks:
        by_page.setdefault(page_index, []).append(rect)

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    for i, page in enumerate(writer.pages):
        mb = page.mediabox
        c.setPageSize((float(mb.width), float(mb.height)))
        c.setFillColor(black)
        for (x0, y0, x1, y1) in by_page.get(i, []):
            h = abs(y1 - y0)
            size = max(7.0, min(12.0, h * 0.8))
            c.setFont("Helvetica-Bold", size)
            c.drawCentredString((x0 + x1) / 2.0, min(y0, y1) + h * 0.22, "X")
        c.showPage()
    c.save()
    buf.seek(0)

    overlay = PdfReader(buf)
    for i, page in enumerate(writer.pages):
        if i < len(overlay.pages):
            page.merge_page(overlay.pages[i])
    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def render_ca_540(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official CA Form 540 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs`` with a non-blank value; every other line is left blank
    (conservative blank-fill — no figure is ever guessed, derived, or
    zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF
        (``ca_540_{tax_year}.pdf`` then ``ca_540_2024.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``CA_540_FIELDS`` (e.g.
        ``"line19_taxable_income"``, ``"line71_ca_withholding"``), a checkbox key
        from ``CA_540_CHECKBOX_FIELDS`` (truthy value → checked), or a radio key
        from ``CA_540_RADIO_FIELDS``:

          - ``"filing_status"`` — value is one of ``single``, ``mfj``, ``mfs``,
            ``hoh``, ``qss``. Marks exactly that box.
          - ``"use_tax_zero_reason"`` — ``no_use_tax_owed`` /
            ``paid_directly_to_cdtfa``.
          - ``"direct_deposit_1_type"`` / ``"direct_deposit_2_type"`` —
            ``checking`` / ``savings``.
          - ``"third_party_designee"`` — ``yes`` / ``no``.

        Provenance (``source_ref``) is validated upstream; this renderer trusts
        the caller. A blank/empty value is skipped. Unknown keys are ignored
        (conservative — never invent a field). SSN/ITIN and the signature block
        are not fillable here.

    Raises
    ------
    FileNotFoundError
        If no Form 540 source PDF is present in ``data/state_forms/``.
    """
    source_path = _ca540_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"CA Form 540 PDF not found at {source_path}. "
            "Download it from https://www.ftb.ca.gov/forms/2024/2024-540.pdf "
            "into data/state_forms/ca_540_2024.pdf."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}
    radio_marks: list[tuple[int, tuple[float, float, float, float]]] = []

    for key, payload in inputs.items():
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value) == "":
            continue
        value = str(value)

        # Radio groups (filing status, direct-deposit type, …): mark one option
        # via a coordinate overlay — never through the value map (shared name).
        if key in CA_540_RADIO_FIELDS:
            option = CA_540_RADIO_FIELDS[key].get(value.strip().lower())
            if option is not None:
                radio_marks.append(option)
            continue

        # Independent checkboxes: check only on a truthy value.
        if key in CA_540_CHECKBOX_FIELDS:
            if value.strip().lower() in _TRUTHY:
                fname, on_state = CA_540_CHECKBOX_FIELDS[key]
                field_values[fname] = on_state
            continue

        # Ordinary text/amount line: map the logical key to its field name.
        # Unknown / unmapped keys are skipped (conservative).
        acro_name = CA_540_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    # Fill text + independent checkboxes (set /V + bake visible, flattened
    # values) and stamp the status watermark.
    pdf_bytes = _render_acroform_pdf(source_bytes, field_values, watermark_mode)

    # Mark any chosen radio options on top (the watermark is faint; a checkbox
    # "X" reads clearly over it).
    if radio_marks:
        pdf_bytes = _stamp_radio_marks(pdf_bytes, radio_marks)

    return pdf_bytes, "application/pdf"


__all__ = ["render_ca_540", "WatermarkMode"]
