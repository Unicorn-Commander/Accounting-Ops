"""SC1120 (South Carolina Corporate Income Tax Return) PDF pre-fill engine.

Unlike the IRS Form 1120 PDF — which is a true AcroForm with addressable
fillable fields — the SCDOR SC1120 PDF at
    https://dor.sc.gov/sites/dor/files/forms/SC1120.pdf
is a STATIC PDF with no AcroForm fields. ``pypdf.get_fields()`` returns 0.

The pre-fill strategy is therefore overlay-based:
  1. Open the static SC1120 PDF.
  2. Build a one-page overlay PDF (reportlab) that stamps typed text at known
     coordinates on top of the source.
  3. Merge overlay onto each page.
  4. Apply the DRAFT watermark (same as IRS Form 1120).

When SCDOR republishes the SC1120 PDF (typically once per calendar year), the
y-coordinates in ``SC1120_PAGE1_COORDS`` may drift. Re-run
``scripts/probe_sc1120_layout.py`` to verify and update.

Conservative blank-filling policy:
  - If a field has no value, leave it blank. Never insert "0" or guess.
  - The CPA fills any blank line.

Output: PDF bytes with DRAFT watermark, ready for CPA review.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

# Reuse the IRS draft-watermark code path
from app.exports.form_prefill import _build_watermark_overlay

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCDOR_FORMS_DIR = Path(__file__).parent.parent.parent / "data" / "scdor_forms"

SC1120_SOURCE_PDF = SCDOR_FORMS_DIR / "SC1120.pdf"
SC1120V_SOURCE_PDF = SCDOR_FORMS_DIR / "SC1120V.pdf"

# Page size: US Letter
LETTER_W = 612.0
LETTER_H = 792.0

# ---------------------------------------------------------------------------
# SC1120 Page 1 coordinate map
#
# Coordinates were extracted from the current SC1120 PDF (Rev. 7/2/25,
# downloaded 2026-05-30) via `scripts/probe_sc1120_layout.py`.
#
# Coordinate origin is bottom-left (standard PDF). All coordinates in points.
# ---------------------------------------------------------------------------

# Header fields — page 1
SC1120_PAGE1_HEADER_COORDS: dict[str, tuple[float, float]] = {
    # "FEIN" label is at y=468; the input box for the EIN sits just above it.
    "fein":                  (95.0, 470.0),
    # "Name" label at y=448; corp name goes to the right
    "corp_name":             (95.0, 450.0),
    # "Mailing address" label at y=268; address text goes to the right
    "mailing_address":       (130.0, 270.0),
    # City / State / ZIP at y=88
    "city_state_zip":        (110.0, 90.0),
    # Income tax period (MM-DD-YYYY format) at y=668
    "income_tax_period_end": (190.0, 670.0),
    # License fee period (MM-DD-YYYY format) at y=648
    "license_fee_period_end": (190.0, 650.0),
    # Total gross receipts (top of page) at y=427
    "total_gross_receipts":  (190.0, 428.0),
}

# Page 1 — Lines 1 through 19 (Part I: Computation of Income Tax Liability).
# Each tuple is (x, y) of the right-aligned dollar field. We draw the text
# right-aligned so the dollar amount visually sits inside the field box.
# X coordinate ~530 corresponds to the rightmost dollar-field column.
SC1120_PAGE1_LINE_COORDS: dict[str, tuple[float, float]] = {
    "1":   (530.0, 398.1),  # Federal taxable income
    "2":   (530.0, 386.1),  # Net adjustment from Sched A+B
    "3":   (530.0, 374.1),  # Total net income as reconciled
    "4":   (530.0, 362.1),  # SC-source income (multi-state) or line 3
    "5":   (530.0, 350.1),  # SC NOL carryover
    "6":   (530.0, 338.1),  # SC net income subject to tax
    "7":   (530.0, 326.1),  # Tax (line 6 × 5%)
    "8":   (530.0, 314.1),  # Tax deferred (foreign trade)
    "9":   (530.0, 302.1),  # Balance (line 7 − line 8)
    "10":  (530.0, 290.1),  # Nonrefundable credits
    "11":  (530.0, 278.1),  # Balance of tax
    "12":  (530.0, 266.1),  # Interest on DISC-deferred tax
    "13":  (530.0, 242.1),  # Total tax and/or interest
    "14a": (530.0, 230.1),  # Tax withheld (1099s/I-290s)
    "14b": (530.0, 218.1),  # Paid by declaration
    "14c": (530.0, 206.1),  # Paid with extension
    "14d": (530.0, 194.1),  # Credit from line 29b
    "14e": (530.0, 182.1),  # Ammonia Additive credit
    "14f": (530.0, 170.1),  # Milk Credit
    "14g": (530.0, 158.1),  # Reserved
    "15":  (530.0, 146.2),  # Total payments + refundable credits
    "16":  (530.0, 134.2),  # Balance of tax/interest
    "17a": (390.0, 110.0),  # Interest
    "17b": (530.0, 110.0),  # Late file/pay penalty
    "17c": (390.0, 98.0),   # Declaration penalty (SC2220)
    "17_total": (530.0, 98.0),  # Total of 17a+b+c
    "18":  (530.0, 86.1),   # Total income tax + interest + penalty (BALANCE DUE)
    "19":  (530.0, 74.1),   # Overpayment
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SC1120Context:
    """Data assembled for a single SC1120 pre-fill."""
    tax_year: int
    entity_display_name: str
    ein: str | None
    mailing_address: str | None
    city_state_zip: str | None
    # Page-1 line values, line_number → dollar string (no comma, no $)
    page1_values: dict[str, str] = field(default_factory=dict)
    # Diagnostic
    filled_lines: list[str] = field(default_factory=list)
    blank_lines: list[str] = field(default_factory=list)


@dataclass
class SC1120VContext:
    """Data for an SC1120-V (payment voucher) pre-fill."""
    tax_year: int
    entity_display_name: str
    ein: str | None
    mailing_address: str | None
    city_state_zip: str | None
    payment_amount_cents: int | None  # how much accompanies the voucher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cents_to_dollar_str(cents: int) -> str:
    """Format integer cents as a dollar string (whole dollars, no commas)."""
    dollars = cents // 100
    return str(dollars)


def _format_dollar_with_commas(cents: int) -> str:
    """Format integer cents as $X,XXX style (for cover sheet display)."""
    dollars = cents / 100.0
    return f"${dollars:,.2f}"


# ---------------------------------------------------------------------------
# Overlay builders
# ---------------------------------------------------------------------------

def _build_sc1120_overlay(ctx: SC1120Context) -> bytes:
    """Build a single-page overlay PDF stamping ctx values onto SC1120 page 1.

    Uses reportlab. Returns bytes of an 8-page Letter overlay PDF (matching the
    SC1120's 8-page count) where only page 1 has content.
    """
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import black
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for SC1120 overlay. "
            "pip install reportlab"
        ) from exc

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))
    c.setFillColor(black)
    c.setFont("Helvetica", 9)

    # ---- Page 1: header + line items ----

    # Header text fields
    if ctx.ein:
        x, y = SC1120_PAGE1_HEADER_COORDS["fein"]
        c.drawString(x, y, ctx.ein)
    if ctx.entity_display_name:
        x, y = SC1120_PAGE1_HEADER_COORDS["corp_name"]
        c.drawString(x, y, ctx.entity_display_name[:60])
    if ctx.mailing_address:
        x, y = SC1120_PAGE1_HEADER_COORDS["mailing_address"]
        c.drawString(x, y, ctx.mailing_address[:80])
    if ctx.city_state_zip:
        x, y = SC1120_PAGE1_HEADER_COORDS["city_state_zip"]
        c.drawString(x, y, ctx.city_state_zip[:50])

    # Income tax period end (e.g. "12-31-2023")
    if ctx.tax_year:
        period_str = f"12-31-{ctx.tax_year}"
        x, y = SC1120_PAGE1_HEADER_COORDS["income_tax_period_end"]
        c.drawString(x, y, period_str)
        x, y = SC1120_PAGE1_HEADER_COORDS["license_fee_period_end"]
        c.drawString(x, y, period_str)

    # Line-item dollar amounts (right-aligned at x coordinate)
    c.setFont("Helvetica", 10)
    for line_num, value in ctx.page1_values.items():
        coord = SC1120_PAGE1_LINE_COORDS.get(line_num)
        if not coord:
            continue
        x, y = coord
        c.drawRightString(x, y, value)

    c.showPage()

    # ---- Pages 2-8 blank (just to match page count for safe merging) ----
    for _ in range(7):
        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


def _build_sc1120v_overlay(ctx: SC1120VContext) -> bytes:
    """Build overlay for SC1120-V payment voucher.

    The voucher is a 2-page PDF; only page 1 (or 2) has the input fields. We
    place the corp name, EIN, period end, payment amount.
    """
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import black
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for SC1120V overlay. "
            "pip install reportlab"
        ) from exc

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))
    c.setFillColor(black)
    c.setFont("Helvetica", 10)

    # Voucher payable to SCDOR — corp name + EIN + period + amount.
    # Coordinates are approximate; the voucher is a static PDF too.
    # These positions place text in the typical voucher input zones.
    if ctx.ein:
        c.drawString(95.0, 660.0, ctx.ein)
    if ctx.entity_display_name:
        c.drawString(95.0, 640.0, ctx.entity_display_name[:60])
    if ctx.mailing_address:
        c.drawString(130.0, 470.0, ctx.mailing_address[:80])
    if ctx.city_state_zip:
        c.drawString(110.0, 290.0, ctx.city_state_zip[:50])
    if ctx.tax_year:
        c.drawString(190.0, 600.0, f"12-31-{ctx.tax_year}")
    if ctx.payment_amount_cents is not None:
        amount_str = _cents_to_dollar_str(ctx.payment_amount_cents)
        c.setFont("Helvetica-Bold", 11)
        c.drawRightString(530.0, 580.0, amount_str)

    c.showPage()
    # Page 2 blank for page-count match
    c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


def _merge_overlay(source_pdf_bytes: bytes, overlay_pdf_bytes: bytes) -> bytes:
    """Merge an overlay PDF on top of a source PDF, page by page.

    Returns the merged PDF bytes.
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


def _apply_watermark_to_static_pdf(source_pdf_bytes: bytes) -> bytes:
    """Apply the standard DRAFT watermark from the IRS form pipeline.

    Same overlay style: diagonal "DRAFT" + yellow page-1 banner.
    """
    reader = PdfReader(io.BytesIO(source_pdf_bytes))
    num_pages = len(reader.pages)
    wm_bytes = _build_watermark_overlay(num_pages)
    return _merge_overlay(source_pdf_bytes, wm_bytes)


# ---------------------------------------------------------------------------
# Value-map builders (from cpa-export package + queue items)
# ---------------------------------------------------------------------------

def _build_sc1120_values_from_package(
    pkg: dict[str, Any] | None,
) -> dict[str, str]:
    """Map a cpa-export 1120 package to SC1120 line values.

    SC1120 line 1 = federal taxable income (Form 1120 line 30).
    SC1120 line 2 = SC modifications (Schedule A + B reconciliation).
    SC1120 line 3 = total net income = line 1 + line 2.
    SC1120 line 4 = SC source income (typically same as line 3 for single-state corp).
    SC1120 line 5 = SC NOL carryover.
    SC1120 line 6 = SC net income subject to tax = line 4 - line 5.
    SC1120 line 7 = SC income tax = line 6 × 5%.

    Example (a loss year): federal taxable income = -$3,670.79 → SC line 1 -3670 →
    SC line 3 -3670 → SC line 4 -3670 → SC line 5 (no carryover yet) blank →
    SC line 6 -3670 → SC line 7 $0 (loss year).
    """
    values: dict[str, str] = {}
    if not pkg:
        return values

    # Federal taxable income (Form 1120 line 30, but cpa-export uses
    # taxable_income_before_nol_cents at the package root)
    fed_taxable_cents = pkg.get("taxable_income_before_nol_cents")
    if fed_taxable_cents is not None:
        cents = int(fed_taxable_cents)
        line1_str = _cents_to_dollar_str(cents)
        # Negative loss: prefix minus
        values["1"] = line1_str
        # Line 3 = line 1 + line 2 (line 2 = 0 by default; CPA fills if any)
        values["3"] = line1_str
        # Line 4 = line 3 for a single-state SC corp
        values["4"] = line1_str
        # Line 6 = line 4 - line 5; assume line 5 blank → line 6 = line 4
        values["6"] = line1_str
        # Line 7 = max(0, line 6 × 5%)
        if cents <= 0:
            values["7"] = "0"
        else:
            tax_cents = int(cents * 0.05)
            values["7"] = _cents_to_dollar_str(tax_cents)
        # Line 9 = line 7 (assuming line 8 = 0)
        values["9"] = values["7"]
        # Line 11 = line 9 (assuming no nonrefundable credits)
        values["11"] = values["7"]
        # Line 13 = line 11
        values["13"] = values["7"]
        # Line 18 = line 16 ≈ line 13 (assuming no penalty/interest tail yet)
        values["18"] = values["7"]

    return values


def _build_sc1120_values_from_queue(
    queue_items: list[dict[str, Any]],
) -> dict[str, str]:
    """Override SC1120 line amounts from queue form_line_proposal rows.

    Queue items reference SC1120 via form_reference like "SC1120 Line 7" or
    "Form SC1120, Line 13".
    """
    import re

    values: dict[str, str] = {}
    line_re = re.compile(r"sc1120.*?[Ll]ine\s+(\d+[a-g]?)", re.IGNORECASE)

    for item in queue_items:
        if item.get("item_type") != "form_line_proposal":
            continue
        form_ref = item.get("form_reference") or ""
        # Only SC1120 references; skip federal
        if "sc1120" not in form_ref.lower():
            continue
        m = line_re.search(form_ref)
        if not m:
            continue
        line = m.group(1).lower()
        content = item.get("content")
        if not isinstance(content, dict):
            continue
        proposed = content.get("proposed_amount_cents")
        if proposed is None:
            continue
        try:
            values[line] = _cents_to_dollar_str(int(proposed))
        except (TypeError, ValueError):
            continue

    return values


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_sc1120_context(
    tax_year: int,
    entity_display_name: str,
    ein: str | None,
    mailing_address: str | None,
    city_state_zip: str | None,
    queue_items: list[dict[str, Any]],
    cpa_package: dict[str, Any] | None,
) -> SC1120Context:
    """Assemble an SC1120 context from queue + package data.

    Queue items take precedence over the package.
    """
    page1: dict[str, str] = {}

    if cpa_package:
        page1.update(_build_sc1120_values_from_package(cpa_package))

    queue_overrides = _build_sc1120_values_from_queue(queue_items)
    page1.update(queue_overrides)

    # For a pre-revenue corp: license fee line is on page 2 (Part II);
    # we don't auto-fill page 2 yet — the CPA fills it manually.

    all_lines = set(SC1120_PAGE1_LINE_COORDS.keys())
    filled = sorted(page1.keys())
    blank = sorted(all_lines - set(page1.keys()))

    return SC1120Context(
        tax_year=tax_year,
        entity_display_name=entity_display_name,
        ein=ein,
        mailing_address=mailing_address,
        city_state_zip=city_state_zip,
        page1_values=page1,
        filled_lines=filled,
        blank_lines=blank,
    )


def generate_sc1120_pdf(
    tax_year: int,
    entity_display_name: str,
    ein: str | None,
    mailing_address: str | None,
    city_state_zip: str | None,
    queue_items: list[dict[str, Any]],
    cpa_package: dict[str, Any] | None,
) -> tuple[bytes, SC1120Context]:
    """Generate a DRAFT-watermarked, pre-filled SC1120 PDF.

    Returns (pdf_bytes, context). The context exposes filled_lines and
    blank_lines so the caller can log diagnostics.

    Raises FileNotFoundError if the source SC1120 PDF is not present in
    data/scdor_forms/. Run scripts/download_scdor_forms.sh to fetch it.
    """
    if not SC1120_SOURCE_PDF.exists():
        raise FileNotFoundError(
            f"SC1120 source PDF not found at {SC1120_SOURCE_PDF}. "
            "Run scripts/download_scdor_forms.sh to fetch it."
        )

    ctx = build_sc1120_context(
        tax_year=tax_year,
        entity_display_name=entity_display_name,
        ein=ein,
        mailing_address=mailing_address,
        city_state_zip=city_state_zip,
        queue_items=queue_items,
        cpa_package=cpa_package,
    )

    source_bytes = SC1120_SOURCE_PDF.read_bytes()
    overlay_bytes = _build_sc1120_overlay(ctx)
    merged = _merge_overlay(source_bytes, overlay_bytes)
    watermarked = _apply_watermark_to_static_pdf(merged)
    return watermarked, ctx


def generate_sc1120v_pdf(
    tax_year: int,
    entity_display_name: str,
    ein: str | None,
    mailing_address: str | None,
    city_state_zip: str | None,
    payment_amount_cents: int | None,
) -> bytes:
    """Generate a DRAFT-watermarked, pre-filled SC1120-V (payment voucher) PDF.

    Example: license fee $25.00 → payment_amount_cents=2500.
    """
    if not SC1120V_SOURCE_PDF.exists():
        raise FileNotFoundError(
            f"SC1120-V source PDF not found at {SC1120V_SOURCE_PDF}. "
            "Run scripts/download_scdor_forms.sh to fetch it."
        )

    ctx = SC1120VContext(
        tax_year=tax_year,
        entity_display_name=entity_display_name,
        ein=ein,
        mailing_address=mailing_address,
        city_state_zip=city_state_zip,
        payment_amount_cents=payment_amount_cents,
    )

    source_bytes = SC1120V_SOURCE_PDF.read_bytes()
    overlay_bytes = _build_sc1120v_overlay(ctx)
    merged = _merge_overlay(source_bytes, overlay_bytes)
    watermarked = _apply_watermark_to_static_pdf(merged)
    return watermarked
