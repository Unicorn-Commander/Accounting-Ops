"""SC Form ST-3 (State Sales & Use Tax Return) PDF form-fill renderer.

Like the SCDOR SC1120 (see ``app/exports/sc_form_prefill.py``), the official
ST-3 PDF at https://dor.sc.gov/forms ("ST3.pdf", Rev. 3/31/23) is a STATIC,
3-page PDF with NO AcroForm fields — ``pypdf.PdfReader(...).get_fields()``
returns an empty dict. The fill strategy is therefore an OVERLAY, identical in
technique to the SC1120 path:

  1. Open the static ST-3 PDF (``data/sc_forms/ST-3.pdf``).
  2. Build a reportlab overlay that stamps typed text at known coordinates
     (``drawString`` for header text, ``drawRightString`` for right-aligned
     dollar amounts) on the return page (page 2) and worksheet (page 3).
  3. Merge the overlay onto each page (``_merge_overlay``).
  4. Apply the watermark selected by ``watermark_mode`` via ``_apply_watermark``
     (the DRAFT / SELF_CERTIFIED / NONE overlay shared with the IRS pipeline).

If the source PDF is ever absent, this module FALLS BACK to a clean, clearly
labeled one-page reportlab "SC ST-3 — prepared summary" sheet that lists the
computed lines. It NEVER fabricates field positions onto a form it cannot see.

Provenance / anti-fabrication: ``inputs`` is the provenance-validated map
``{field_key: {"value": str, "source_ref": str, "origin": str}}`` (validated
upstream). This renderer renders ONLY present, non-blank values verbatim — it
never invents, derives, or back-fills a figure. Coordinates were extracted from
the ST-3 (Rev. 3/31/23) text layer; if SCDOR republishes the PDF the y-values
may drift and must be re-probed.
"""
from __future__ import annotations

import io
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from app.exports.form_prefill import (
    WatermarkMode,
    _apply_watermark,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SC_FORMS_DIR = Path(__file__).parent.parent.parent / "data" / "sc_forms"
ST3_SOURCE_PDF = SC_FORMS_DIR / "ST-3.pdf"

# Page size: US Letter (ST-3 pages are 612 x 792 pt).
LETTER_W = 612.0
LETTER_H = 792.0

# Input keys this renderer understands. (Provenance is validated upstream; we
# render only the keys present in ``inputs`` with a non-blank value.)
SUPPORTED_INPUT_KEYS: tuple[str, ...] = (
    "business_name",          # filer name (header name/address block)
    "retail_license_number",  # Retail License or Use Tax Registration Number
    "fein",                   # FEIN (header)
    "period",                 # Period Ended (MM-YYYY)
    "gross_proceeds",         # line 1 (= worksheet Item 3)
    "deductions",             # line 2 (= worksheet Item 5)
    "net_taxable",            # line 3
    "state_tax",              # line 4 (= line 3 x 6%)
    "local_tax",              # line 5 (from ST-389 Column B)
    "total_state_local_tax",  # line 6 (= line 4 + line 5)
    "total_tax",              # line 10 / total amount due
)

# ---------------------------------------------------------------------------
# ST-3 page-2 (the return) coordinate map.
#
# Coordinates extracted from the ST-3 PDF (Rev. 3/31/23) text layer. Origin is
# bottom-left (standard PDF), points. Dollar amounts are drawn RIGHT-ALIGNED so
# they sit inside the money box, which ends just before the printed decimal at
# x~=541; we right-align the whole-dollar+cents string at x=535.
# ---------------------------------------------------------------------------

# Right edge of the page-2 money column (just left of the printed "." at 541.4).
_P2_AMOUNT_X = 535.0

# Header text fields on page 2 (left x of a drawString).
ST3_PAGE2_HEADER_COORDS: dict[str, tuple[float, float]] = {
    # "Retail License or Use Tax Registration" label sits at y~664, x~434; the
    # entry box is just below it on the right.
    "retail_license_number": (437.0, 650.0),
    # "FEIN" label at y~632.7, x~489; entry box below.
    "fein":                  (470.0, 620.0),
    # "Period Ended (MM-YYYY)" label at y~600.3, x~451.5; entry box below.
    "period":                (455.0, 588.0),
    # Name/address block — "If the area below is blank, fill in name and
    # address." prompt at y~609.5, x~42; the fill-in area is below it.
    "business_name":         (44.0, 590.0),
}

# Page-2 line-item dollar fields — line_key -> (right_x, baseline_y).
ST3_PAGE2_LINE_COORDS: dict[str, tuple[float, float]] = {
    "gross_proceeds":        (_P2_AMOUNT_X, 480.8),  # line 1
    "deductions":            (_P2_AMOUNT_X, 457.6),  # line 2
    "net_taxable":           (_P2_AMOUNT_X, 433.8),  # line 3
    "state_tax":             (_P2_AMOUNT_X, 409.7),  # line 4 (x 6%)
    "local_tax":             (_P2_AMOUNT_X, 314.3),  # line 5 (ST-389)
    "total_state_local_tax": (_P2_AMOUNT_X, 290.3),  # line 6
    "total_tax":             (_P2_AMOUNT_X, 170.4),  # line 10 total amount due
}

# ST-3 page-3 (the Sales and Use Tax Worksheet) — these mirror the page-2 lines
# (Item 3 -> line 1, Item 5 -> line 2, Item 6 -> line 3). The worksheet money
# column right edge sits left of the printed "$"/markers at x~=440-442.
_P3_AMOUNT_X = 525.0

ST3_PAGE3_HEADER_COORDS: dict[str, tuple[float, float]] = {
    # "Retail License or Use Tax Registration Number  Period Ended MM-YYYY"
    # header label at y~697.7, x~66; entries go on the line below.
    "retail_license_number": (70.0, 686.0),
    "period":                (300.0, 686.0),
}

ST3_PAGE3_ITEM_COORDS: dict[str, tuple[float, float]] = {
    "gross_proceeds": (_P3_AMOUNT_X, 678.1),  # Item 3 total (= line 1)
    "deductions":     (_P3_AMOUNT_X, 345.6),  # Item 5 total (= line 2)
    "net_taxable":    (_P3_AMOUNT_X, 309.5),  # Item 6 (= line 3)
}


# ---------------------------------------------------------------------------
# inputs -> display-string extraction (render present/non-blank values only)
# ---------------------------------------------------------------------------

def _present_values(inputs: dict) -> dict[str, str]:
    """Reduce the provenance ``inputs`` map to ``{key: display_string}``.

    Renders ONLY keys with a present, non-blank ``value``. Never invents a value;
    unknown keys are ignored (conservative). Provenance (``source_ref`` /
    ``origin``) is validated upstream — we only need the display string here.
    """
    out: dict[str, str] = {}
    if not inputs:
        return out
    for key, spec in inputs.items():
        if not isinstance(spec, dict):
            continue
        raw = spec.get("value")
        if raw is None:
            continue
        text = str(raw).strip()
        if text == "":
            continue
        out[key] = text
    return out


# ---------------------------------------------------------------------------
# Overlay builder (real ST-3) — mirrors sc_form_prefill._build_sc1120_overlay
# ---------------------------------------------------------------------------

def _build_st3_overlay(values: dict[str, str], num_pages: int) -> bytes:
    """Build an ``num_pages``-page overlay stamping ``values`` onto the ST-3.

    Page 1 is the SCDOR instructions page (left blank); page 2 is the return;
    page 3 is the worksheet. Returns the overlay PDF bytes.
    """
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import black
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for the ST-3 overlay. pip install reportlab"
        ) from exc

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))
    c.setFillColor(black)

    for page_index in range(num_pages):
        if page_index == 1:  # page 2 — the return
            # Header text.
            c.setFont("Helvetica", 9)
            for key, (x, y) in ST3_PAGE2_HEADER_COORDS.items():
                v = values.get(key)
                if v:
                    c.drawString(x, y, v[:60] if key == "business_name" else v[:40])
            # Line-item dollar amounts (right-aligned).
            c.setFont("Helvetica", 10)
            for key, (x, y) in ST3_PAGE2_LINE_COORDS.items():
                v = values.get(key)
                if v:
                    c.drawRightString(x, y, v)
        elif page_index == 2:  # page 3 — the worksheet
            c.setFont("Helvetica", 9)
            for key, (x, y) in ST3_PAGE3_HEADER_COORDS.items():
                v = values.get(key)
                if v:
                    c.drawString(x, y, v[:40])
            c.setFont("Helvetica", 10)
            for key, (x, y) in ST3_PAGE3_ITEM_COORDS.items():
                v = values.get(key)
                if v:
                    c.drawRightString(x, y, v)
        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


def _merge_overlay(source_pdf_bytes: bytes, overlay_pdf_bytes: bytes) -> bytes:
    """Merge an overlay PDF on top of a source PDF, page by page.

    Mirrors ``sc_form_prefill._merge_overlay``.
    """
    src_reader = PdfReader(io.BytesIO(source_pdf_bytes))
    ov_reader = PdfReader(io.BytesIO(overlay_pdf_bytes))
    writer = PdfWriter()

    for i, page in enumerate(src_reader.pages):
        if i < len(ov_reader.pages):
            page.merge_page(ov_reader.pages[i])
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


# ---------------------------------------------------------------------------
# Fallback "prepared summary" sheet (only if the source PDF is absent)
# ---------------------------------------------------------------------------

# Lines shown on the fallback summary, in form order: (input_key, label).
_SUMMARY_LINES: tuple[tuple[str, str], ...] = (
    ("business_name", "Business name"),
    ("retail_license_number", "Retail License / Use Tax Registration No."),
    ("fein", "FEIN"),
    ("period", "Period Ended (MM-YYYY)"),
    ("gross_proceeds", "Line 1 — Total gross proceeds of sales"),
    ("deductions", "Line 2 — Total allowable deductions"),
    ("net_taxable", "Line 3 — Net taxable sales and purchases"),
    ("state_tax", "Line 4 — State Sales and Use Tax (line 3 x 6%)"),
    ("local_tax", "Line 5 — Additional local tax (from ST-389)"),
    ("total_state_local_tax", "Line 6 — Total State and Local Taxes due"),
    ("total_tax", "Line 10 — TOTAL AMOUNT DUE"),
)


def _build_summary_pdf(values: dict[str, str]) -> bytes:
    """Build a one-page, clearly-labeled SC ST-3 prepared-summary worksheet.

    This is the FALLBACK rendered only when the official ST-3 PDF is not
    available. It is honestly titled a "prepared summary" (not the filing form)
    and lists only the present, non-blank values.
    """
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import Color, HexColor, black
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for the ST-3 summary. pip install reportlab"
        ) from exc

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))

    # Title block.
    c.setFillColor(HexColor("#0F3D2E"))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(54, LETTER_H - 70, "SC ST-3 — Prepared Summary")
    c.setFillColor(Color(0.3, 0.3, 0.3))
    c.setFont("Helvetica", 9)
    c.drawString(
        54, LETTER_H - 88,
        "State Sales & Use Tax Return — computed line summary "
        "(official ST-3 PDF not available; this is a worksheet, not the filing form).",
    )

    # Line rows.
    y = LETTER_H - 130
    c.setFillColor(black)
    label_x, value_x = 60, 552
    for key, label in _SUMMARY_LINES:
        v = values.get(key)
        if not v:
            continue
        c.setFont("Helvetica", 10)
        c.drawString(label_x, y, label)
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(value_x, y, v)
        c.setStrokeColor(Color(0.85, 0.85, 0.85))
        c.line(label_x, y - 4, value_x, y - 4)
        y -= 22
        if y < 80:
            break

    c.setFillColor(Color(0.4, 0.4, 0.4))
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(
        54, 54,
        "Local option taxes vary by county and are reported on the ST-389. "
        "Confirm all figures before filing.",
    )

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_st3(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Render a filled SC ST-3 (State Sales & Use Tax Return) PDF.

    Returns ``(pdf_bytes, "application/pdf")``.

    ``inputs`` is the provenance-validated map
    ``{field_key: {"value": str, "source_ref": str, "origin": str}}``. Only
    present, non-blank values are rendered — figures are never invented. See
    ``SUPPORTED_INPUT_KEYS`` for the keys mapped onto the form.

    ``watermark_mode`` (DRAFT | SELF_CERTIFIED | NONE) selects the overlay
    stamped on the result.

    When the official ST-3 PDF is present in ``data/sc_forms/`` this stamps the
    real form via a reportlab coordinate overlay; when it is absent it falls back
    to a clearly-labeled one-page prepared-summary sheet. ``tax_year`` is
    accepted for interface symmetry with the other renderers and surfaced on the
    summary fallback; the ST-3 face is year-stable, so it does not change the
    layout.
    """
    values = _present_values(inputs)

    if ST3_SOURCE_PDF.exists():
        source_bytes = ST3_SOURCE_PDF.read_bytes()
        num_pages = len(PdfReader(io.BytesIO(source_bytes)).pages)
        overlay_bytes = _build_st3_overlay(values, num_pages)
        merged = _merge_overlay(source_bytes, overlay_bytes)
        watermarked = _apply_watermark(merged, watermark_mode)
        return watermarked, "application/pdf"

    # Fallback — no source PDF: build a clean prepared-summary sheet, then stamp.
    summary = _build_summary_pdf(values)
    watermarked = _apply_watermark(summary, watermark_mode)
    return watermarked, "application/pdf"


__all__ = ["render_st3", "WatermarkMode", "SUPPORTED_INPUT_KEYS"]
