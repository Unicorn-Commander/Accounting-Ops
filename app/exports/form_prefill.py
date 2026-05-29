"""IRS form pre-fill engine.

Fills official IRS AcroForm PDFs with data sourced from:
  1. The cpa-export JSON package (income/deduction/balance-sheet amounts).
  2. The tax_review_queue (form_line_proposal items — agent-proposed amounts).

Queue items take precedence over the cpa-export package amounts when present.

Every filled PDF is stamped with a multi-layer DRAFT watermark:
  - A diagonal "DRAFT — CPA REVIEW REQUIRED" watermark on every page
    (added as a transparent overlay using reportlab + pypdf).
  - A solid yellow "DRAFT — CPA review required — do not file as-is" banner
    injected into page 1 via a separate overlay.

Usage
-----
    from app.exports.form_prefill import generate_form_pdf, FormName
    from pathlib import Path

    pdf_bytes = await generate_form_pdf(
        form_name=FormName.FORM_1120,
        workspace_id=wid,
        entity_id=eid,
        tax_year=2023,
        session=db_session,           # async SQLAlchemy session (tenant schema)
        cpa_package=pkg_dict,         # from cpa-export JSON; may be None
    )

Supported form_names: "1120", "1120-schedule-l", "1125-e", "1120-package"
  - "1120-package" returns a zip (application/zip) containing all three PDFs.
  - Others return a single application/pdf.

Conservative blank-filling policy
----------------------------------
If a field has no value from our data, it is left blank. We never guess or
insert zero for a field that was not in our data — the CPA must complete it.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject

from app.exports.form_1120_field_map import (
    FORM_1120_FIELDS,
    FORM_1120_HEADER_FIELDS,
    SCHEDULE_L_FIELDS,
    SCHEDULE_L_LINE_TO_FIELD,
    FORM_1125E_FIELDS,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IRS_FORMS_DIR = Path(__file__).parent.parent.parent / "data" / "irs_forms"

FORM_PATHS: dict[str, Path] = {
    "1120":      IRS_FORMS_DIR / "f1120_2024.pdf",
    "1125-e":    IRS_FORMS_DIR / "f1125e_2024.pdf",
    # "1120-schedule-l" uses the same PDF as "1120" (Schedule L is on page 5).
    "1120-schedule-l": IRS_FORMS_DIR / "f1120_2024.pdf",
}

# If TY 2023 forms are requested, use 2023 PDFs if available, else fall back
# to 2024 (field names are identical through 2022–2024).
FORM_PATHS_2023: dict[str, Path] = {
    "1120":           IRS_FORMS_DIR / "f1120_2023.pdf",
    "1125-e":         IRS_FORMS_DIR / "f1125e_2023.pdf",
    "1120-schedule-l": IRS_FORMS_DIR / "f1120_2023.pdf",
}

DRAFT_BANNER_TEXT = "DRAFT — CPA review required — do not file as-is"
DRAFT_WATERMARK_TEXT = "DRAFT"

# Page size constants (IRS forms are US Letter: 8.5 × 11 in = 612 × 792 pt)
LETTER_W = 612.0
LETTER_H = 792.0


class FormName(str, Enum):
    FORM_1120 = "1120"
    SCHEDULE_L = "1120-schedule-l"
    FORM_1125E = "1125-e"
    PACKAGE = "1120-package"


# ---------------------------------------------------------------------------
# Dollar helpers
# ---------------------------------------------------------------------------

def _cents_to_dollar_str(cents: int) -> str:
    """Convert integer cents to a plain dollar string for PDF fields.

    The IRS AcroForm fields are plain-text; no currency symbol or commas.
    Negative values are represented with a leading minus (e.g. "-1250").

    Examples:
        123456  → "1234"   (truncated to whole dollars, IRS rounds to dollars)
        -50000  → "-500"
        0       → "0"
    """
    dollars = cents // 100
    return str(dollars)


def _parse_line_from_reference(form_reference: str) -> str | None:
    """Extract a bare line number from a form_reference string.

    Examples:
        "Form 1120, Line 26"             → "26"
        "Form 1120, Line 1a"             → "1a"
        "Form 1120, Line 26 / Form 1125-A" → "26"
        "Schedule L, Line 10a"           → "10a"
    """
    m = re.search(r"[Ll]ine\s+([0-9]+[a-c]?)", form_reference)
    if m:
        return m.group(1).lower()
    return None


# ---------------------------------------------------------------------------
# Queue → field-value map builder
# ---------------------------------------------------------------------------

def _build_field_values_from_queue(
    queue_items: list[dict[str, Any]],
) -> dict[str, str]:
    """Convert tax_review_queue form_line_proposal rows into field→value map.

    Only processes rows where:
      - item_type == 'form_line_proposal'
      - proposed_amount_cents is present in the content dict

    Returns a mapping of line_number → dollar_string.
    """
    values: dict[str, str] = {}
    for item in queue_items:
        if item.get("item_type") != "form_line_proposal":
            continue
        form_ref = item.get("form_reference") or ""
        line = _parse_line_from_reference(form_ref)
        if not line:
            continue
        content = item.get("content")
        if not isinstance(content, dict):
            continue
        proposed = content.get("proposed_amount_cents")
        if proposed is None:
            continue
        try:
            cents = int(proposed)
        except (TypeError, ValueError):
            continue
        values[line] = _cents_to_dollar_str(cents)
    return values


def _build_field_values_from_package(
    pkg: dict[str, Any],
) -> dict[str, str]:
    """Extract line amounts from the cpa-export JSON package.

    Returns a mapping of line_number → dollar_string.
    """
    values: dict[str, str] = {}

    income_lines: list[dict] = pkg.get("income", {}).get("lines", [])
    for l in income_lines:
        line = str(l.get("line", "")).lower()
        amt = l.get("amount_cents")
        if line and amt is not None:
            values[line] = _cents_to_dollar_str(int(amt))

    deduction_lines: list[dict] = pkg.get("deductions", {}).get("lines", [])
    for l in deduction_lines:
        line = str(l.get("line", "")).lower()
        amt = l.get("amount_cents")
        if line and amt is not None:
            values[line] = _cents_to_dollar_str(int(amt))

    # Taxable income line 28
    tib = pkg.get("taxable_income_before_nol_cents")
    if tib is not None:
        values["28"] = _cents_to_dollar_str(int(tib))

    return values


def _build_schedule_l_values_from_package(
    pkg: dict[str, Any],
) -> dict[str, str]:
    """Extract Schedule L (balance sheet) amounts from the cpa-export package.

    Returns a mapping of Schedule L field key → dollar_string.
    """
    values: dict[str, str] = {}
    sched = pkg.get("schedule_l", {})

    for side_key in ("assets", "liabilities_equity"):
        for l in sched.get(side_key, []):
            line = str(l.get("line", "")).lower()
            amt = l.get("amount_cents")
            if not line or amt is None:
                continue
            field_key = SCHEDULE_L_LINE_TO_FIELD.get(line)
            if field_key:
                values[field_key] = _cents_to_dollar_str(int(amt))

    # Total assets → line 15
    ta = sched.get("total_assets_cents")
    if ta is not None:
        values["15_eoy"] = _cents_to_dollar_str(int(ta))

    # Total liabilities + equity → line 28
    tl = sched.get("total_liabilities_equity_cents")
    if tl is not None:
        values["28_eoy"] = _cents_to_dollar_str(int(tl))

    return values


# ---------------------------------------------------------------------------
# Watermark overlay builder
# ---------------------------------------------------------------------------

def _build_watermark_overlay(num_pages: int) -> bytes:
    """Build a multi-page PDF with DRAFT watermark + page-1 banner.

    Uses reportlab to build a PDF of the same number of pages as the target.
    Each page gets:
      - Diagonal "DRAFT" text centred on the page (semi-transparent red).
    Page 1 additionally gets:
      - A solid yellow banner at the top reading DRAFT_BANNER_TEXT.

    Returns bytes of the watermark PDF.
    """
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import Color, HexColor
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for the DRAFT watermark overlay. "
            "Install with: pip install reportlab"
        ) from exc

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))

    draft_red = Color(0.85, 0.1, 0.1, alpha=0.18)
    banner_yellow = HexColor("#FFC83D")
    banner_text_color = Color(0.3, 0.15, 0.0, alpha=1.0)

    for page_num in range(num_pages):
        # Page 1 banner
        if page_num == 0:
            c.setFillColor(banner_yellow)
            c.rect(0, LETTER_H - 28, LETTER_W, 28, fill=1, stroke=0)
            c.setFillColor(banner_text_color)
            c.setFont("Helvetica-Bold", 9)
            c.drawCentredString(
                LETTER_W / 2,
                LETTER_H - 18,
                DRAFT_BANNER_TEXT,
            )

        # Diagonal DRAFT watermark
        c.saveState()
        c.translate(LETTER_W / 2, LETTER_H / 2)
        c.rotate(45)
        c.setFillColor(draft_red)
        c.setFont("Helvetica-Bold", 96)
        c.drawCentredString(0, -12, "DRAFT")
        c.restoreState()

        c.showPage()

    c.save()
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# PDF form filler (PyPDF + direct field annotation update)
# ---------------------------------------------------------------------------

def _fill_pdf_fields(
    source_pdf_bytes: bytes,
    field_values: dict[str, str],
) -> bytes:
    """Fill AcroForm fields in a PDF using pypdf.

    pypdf's writer.update_page_form_field_values() fills text fields by
    matching the field NameObject. We iterate all pages and patch every
    annotation whose /T (field name) matches a key in field_values.

    Returns the modified PDF bytes with fields filled and NeedAppearances=True
    so Acrobat/Preview regenerates the visual appearance on open.
    """
    reader = PdfReader(io.BytesIO(source_pdf_bytes))
    writer = PdfWriter()

    # Copy all pages
    for page in reader.pages:
        writer.add_page(page)

    # Copy form structure
    if reader.trailer.get("/Root") and reader.trailer["/Root"].get("/AcroForm"):
        writer._root_object.update({
            NameObject("/AcroForm"): reader.trailer["/Root"]["/AcroForm"]
        })

    # Set NeedAppearances so PDF viewers re-render the typed text
    try:
        acroform = writer._root_object["/AcroForm"].get_object()
        acroform.update({
            NameObject("/NeedAppearances"): NameObject("true")
        })
    except (KeyError, AttributeError):
        pass

    # Fill fields across all pages
    writer.update_page_form_field_values(
        writer.pages,
        field_values,
        auto_regenerate=False,
    )

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def _apply_watermark(source_pdf_bytes: bytes) -> bytes:
    """Stamp the DRAFT overlay on every page of source_pdf_bytes.

    The overlay is built with reportlab and merged page-by-page using pypdf.
    The form data (AcroForm fields) from source_pdf is preserved.
    """
    reader = PdfReader(io.BytesIO(source_pdf_bytes))
    num_pages = len(reader.pages)

    watermark_bytes = _build_watermark_overlay(num_pages)
    wm_reader = PdfReader(io.BytesIO(watermark_bytes))

    writer = PdfWriter()

    # Re-attach AcroForm to preserve filled fields
    if reader.trailer.get("/Root") and reader.trailer["/Root"].get("/AcroForm"):
        writer._root_object.update({
            NameObject("/AcroForm"): reader.trailer["/Root"]["/AcroForm"]
        })
        try:
            acroform = writer._root_object["/AcroForm"].get_object()
            acroform.update({NameObject("/NeedAppearances"): NameObject("true")})
        except (KeyError, AttributeError):
            pass

    for i, page in enumerate(reader.pages):
        if i < len(wm_reader.pages):
            page.merge_page(wm_reader.pages[i])
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


# ---------------------------------------------------------------------------
# Per-form builders
# ---------------------------------------------------------------------------

@dataclass
class PrefillContext:
    """All data needed to pre-fill a set of IRS forms for one entity-year."""
    tax_year: int
    entity_display_name: str
    ein: str | None
    # Field → dollar-string values for page-1 income/deduction lines
    p1_values: dict[str, str]
    # Field → dollar-string values for Schedule L
    sched_l_values: dict[str, str]
    # Officer compensation rows (list of dicts with officer1_* keys)
    officer_values: dict[str, str]
    # Filled lines (for diagnostic logging)
    filled_lines: list[str]
    # Lines that were blank (no data)
    blank_lines: list[str]


def _resolve_form_path(form_key: str, tax_year: int) -> Path:
    """Return the correct IRS form PDF path for the given tax year."""
    if tax_year == 2023:
        path = FORM_PATHS_2023.get(form_key)
        if path and path.exists():
            return path
    return FORM_PATHS[form_key]


def _build_form_1120(ctx: PrefillContext) -> bytes:
    """Fill Form 1120 page 1 income/deduction lines. Returns PDF bytes."""
    form_path = _resolve_form_path("1120", ctx.tax_year)
    if not form_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1120 PDF not found at {form_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )

    source = form_path.read_bytes()

    # Build field→value dict from our line-number values
    field_values: dict[str, str] = {}

    # Header fields we can fill
    if ctx.entity_display_name:
        field_values[FORM_1120_HEADER_FIELDS["corp_name"]] = ctx.entity_display_name
    if ctx.ein:
        field_values[FORM_1120_HEADER_FIELDS["ein"]] = ctx.ein

    # Page 1 income/deduction lines
    for line, value in ctx.p1_values.items():
        field_name = FORM_1120_FIELDS.get(line)
        if field_name:
            field_values[field_name] = value

    filled_pdf = _fill_pdf_fields(source, field_values)
    return _apply_watermark(filled_pdf)


def _build_schedule_l(ctx: PrefillContext) -> bytes:
    """Fill Schedule L balance sheet fields on Form 1120. Returns PDF bytes."""
    form_path = _resolve_form_path("1120-schedule-l", ctx.tax_year)
    if not form_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1120 PDF not found at {form_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )

    source = form_path.read_bytes()

    field_values: dict[str, str] = {}
    if ctx.entity_display_name:
        field_values[FORM_1120_HEADER_FIELDS["corp_name"]] = ctx.entity_display_name
    if ctx.ein:
        field_values[FORM_1120_HEADER_FIELDS["ein"]] = ctx.ein

    for field_key, value in ctx.sched_l_values.items():
        sched_l_field = SCHEDULE_L_FIELDS.get(field_key)
        if sched_l_field:
            field_values[sched_l_field] = value

    filled_pdf = _fill_pdf_fields(source, field_values)
    return _apply_watermark(filled_pdf)


def _build_form_1125e(ctx: PrefillContext) -> bytes:
    """Fill Form 1125-E officer compensation. Returns PDF bytes."""
    form_path = _resolve_form_path("1125-e", ctx.tax_year)
    if not form_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1125-E PDF not found at {form_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )

    source = form_path.read_bytes()

    field_values: dict[str, str] = {}
    if ctx.entity_display_name:
        field_values[FORM_1125E_FIELDS["corp_name"]] = ctx.entity_display_name
    if ctx.ein:
        field_values[FORM_1125E_FIELDS["ein"]] = ctx.ein
    field_values[FORM_1125E_FIELDS["tax_year"]] = str(ctx.tax_year)

    for field_key, value in ctx.officer_values.items():
        form_field = FORM_1125E_FIELDS.get(field_key)
        if form_field:
            field_values[form_field] = value

    filled_pdf = _fill_pdf_fields(source, field_values)
    return _apply_watermark(filled_pdf)


# ---------------------------------------------------------------------------
# Context builder — merges queue + cpa-export data
# ---------------------------------------------------------------------------

def build_prefill_context(
    tax_year: int,
    entity_display_name: str,
    ein: str | None,
    queue_items: list[dict[str, Any]],
    cpa_package: dict[str, Any] | None,
) -> PrefillContext:
    """Merge cpa-export package + queue proposals into a PrefillContext.

    Priority: queue form_line_proposal items override cpa-export amounts.
    Conservative: if a line has no data from either source, it is left blank.
    """
    # Start with cpa-export amounts (lower priority)
    p1_values: dict[str, str] = {}
    sched_l_values: dict[str, str] = {}

    if cpa_package:
        p1_values.update(_build_field_values_from_package(cpa_package))
        sched_l_values.update(_build_schedule_l_values_from_package(cpa_package))

    # Apply queue proposals (higher priority — agent-reviewed line amounts)
    queue_overrides = _build_field_values_from_queue(queue_items)
    p1_values.update(queue_overrides)

    # Officer compensation from queue (item_type="form_line_proposal", form_reference
    # contains "Form 1125-E"). Build officer_values from queue items that reference
    # 1125-E fields. For now, use the total from queue if available.
    officer_values: dict[str, str] = {}
    for item in queue_items:
        if item.get("item_type") != "form_line_proposal":
            continue
        ref = (item.get("form_reference") or "").lower()
        if "1125-e" not in ref and "1125e" not in ref:
            continue
        content = item.get("content") or {}
        # Accept a flat dict with officer{N}_* keys directly in content
        for k, v in content.items():
            if k.startswith("officer") and v is not None:
                officer_values[k] = str(v)
        # Accept proposed_amount_cents as the total (net_officer_comp)
        if "proposed_amount_cents" in content:
            try:
                cents = int(content["proposed_amount_cents"])
                officer_values["net_officer_comp"] = _cents_to_dollar_str(cents)
                officer_values["total_officer_comp"] = _cents_to_dollar_str(cents)
            except (TypeError, ValueError):
                pass

    all_lines = set(FORM_1120_FIELDS.keys())
    filled_lines = sorted(p1_values.keys())
    blank_lines = sorted(all_lines - set(p1_values.keys()))

    return PrefillContext(
        tax_year=tax_year,
        entity_display_name=entity_display_name,
        ein=ein,
        p1_values=p1_values,
        sched_l_values=sched_l_values,
        officer_values=officer_values,
        filled_lines=filled_lines,
        blank_lines=blank_lines,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_form_pdf(
    form_name: str,
    tax_year: int,
    entity_display_name: str,
    ein: str | None,
    queue_items: list[dict[str, Any]],
    cpa_package: dict[str, Any] | None,
) -> tuple[bytes, str]:
    """Generate a filled IRS PDF (or zip for "1120-package").

    Returns (pdf_bytes, content_type).
      - Single forms: ("application/pdf", bytes)
      - Package: ("application/zip", bytes)

    Raises FileNotFoundError if the source PDF is not present in data/irs_forms/.
    CPA must obtain forms via scripts/download_irs_forms.sh.
    """
    ctx = build_prefill_context(
        tax_year=tax_year,
        entity_display_name=entity_display_name,
        ein=ein,
        queue_items=queue_items,
        cpa_package=cpa_package,
    )

    fn = form_name.lower()

    if fn == FormName.FORM_1120:
        pdf_bytes = _build_form_1120(ctx)
        return pdf_bytes, "application/pdf"

    elif fn == FormName.SCHEDULE_L:
        pdf_bytes = _build_schedule_l(ctx)
        return pdf_bytes, "application/pdf"

    elif fn == FormName.FORM_1125E:
        pdf_bytes = _build_form_1125e(ctx)
        return pdf_bytes, "application/pdf"

    elif fn == FormName.PACKAGE:
        return _build_package_zip(ctx), "application/zip"

    else:
        raise ValueError(
            f"Unknown form_name '{form_name}'. "
            "Supported: 1120, 1120-schedule-l, 1125-e, 1120-package."
        )


def _build_package_zip(ctx: PrefillContext) -> bytes:
    """Bundle Form 1120, Schedule L, and Form 1125-E into a zip archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        slug = (ctx.entity_display_name or "entity").lower().replace(" ", "-")
        yr = ctx.tax_year

        # Form 1120
        try:
            zf.writestr(
                f"form-1120-{slug}-{yr}.pdf",
                _build_form_1120(ctx),
            )
        except FileNotFoundError as e:
            # Write a small text placeholder rather than aborting the whole zip
            zf.writestr(f"form-1120-{slug}-{yr}.MISSING.txt", str(e))

        # Schedule L (same PDF file as 1120 — filled with balance-sheet data)
        try:
            zf.writestr(
                f"schedule-l-{slug}-{yr}.pdf",
                _build_schedule_l(ctx),
            )
        except FileNotFoundError as e:
            zf.writestr(f"schedule-l-{slug}-{yr}.MISSING.txt", str(e))

        # Form 1125-E
        try:
            zf.writestr(
                f"form-1125e-{slug}-{yr}.pdf",
                _build_form_1125e(ctx),
            )
        except FileNotFoundError as e:
            zf.writestr(f"form-1125e-{slug}-{yr}.MISSING.txt", str(e))

    buf.seek(0)
    return buf.read()
