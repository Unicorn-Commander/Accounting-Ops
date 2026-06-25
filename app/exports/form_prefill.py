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
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    NameObject,
    TextStringObject,
)

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
# Softer banner used when a user with no tax professional has self-certified a
# work product. It is honest about provenance without the alarming "do not file"
# language, because a self-attested W-4P/return IS meant to be used.
SELF_CERT_BANNER_TEXT = (
    "Prepared via self-certification — review carefully before you sign and submit"
)

# Page size constants (IRS forms are US Letter: 8.5 × 11 in = 612 × 792 pt)
LETTER_W = 612.0
LETTER_H = 792.0


class FormName(str, Enum):
    FORM_1120 = "1120"
    SCHEDULE_L = "1120-schedule-l"
    FORM_1125E = "1125-e"
    PACKAGE = "1120-package"
    W4P = "w-4p"


class WatermarkMode(str, Enum):
    """Controls the overlay stamped on a generated work-product PDF.

    DRAFT (default) preserves the original CPA-review-required behavior so the
    1120 path is byte-for-byte unchanged. SELF_CERTIFIED is used once a user
    with no available tax professional has self-attested a work product — it
    drops the alarming diagonal "DRAFT" + "do not file" language in favor of an
    honest, softer provenance banner. NONE leaves the PDF unstamped (e.g. a
    licensed pro has finalized it).
    """
    DRAFT = "draft"
    SELF_CERTIFIED = "self_certified"
    NONE = "none"


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

def _build_watermark_overlay(
    num_pages: int,
    mode: WatermarkMode = WatermarkMode.DRAFT,
) -> bytes:
    """Build a multi-page overlay PDF whose stamping depends on ``mode``.

    DRAFT (default, unchanged): every page gets a diagonal semi-transparent red
    "DRAFT"; page 1 also gets a solid yellow DRAFT_BANNER_TEXT banner.

    SELF_CERTIFIED: no diagonal "DRAFT". Page 1 gets a muted slate banner with
    SELF_CERT_BANNER_TEXT — honest provenance without "do not file".

    Returns bytes of the overlay PDF (same page count as the target).
    """
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.colors import Color, HexColor
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for the watermark overlay. "
            "Install with: pip install reportlab"
        ) from exc

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))

    draft_red = Color(0.85, 0.1, 0.1, alpha=0.18)
    banner_yellow = HexColor("#FFC83D")
    banner_text_color = Color(0.3, 0.15, 0.0, alpha=1.0)
    # Self-certified: a calm slate banner with light text.
    selfcert_slate = HexColor("#475569")
    selfcert_text_color = Color(1.0, 1.0, 1.0, alpha=1.0)

    for page_num in range(num_pages):
        if mode == WatermarkMode.SELF_CERTIFIED:
            # Page-1 provenance banner only; no diagonal stamp.
            if page_num == 0:
                c.setFillColor(selfcert_slate)
                c.rect(0, LETTER_H - 26, LETTER_W, 26, fill=1, stroke=0)
                c.setFillColor(selfcert_text_color)
                c.setFont("Helvetica-Bold", 8.5)
                c.drawCentredString(
                    LETTER_W / 2,
                    LETTER_H - 17,
                    SELF_CERT_BANNER_TEXT,
                )
            c.showPage()
            continue

        # DRAFT mode (default).
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

    # Set NeedAppearances so PDF viewers re-render the typed text.
    # NB: NeedAppearances is a PDF boolean (not a /Name) — using NameObject("true")
    # raised pypdf.errors.DeprecationError on pypdf 5+.
    try:
        acroform = writer._root_object["/AcroForm"].get_object()
        acroform.update({
            NameObject("/NeedAppearances"): BooleanObject(True)
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


def _apply_watermark(
    source_pdf_bytes: bytes,
    mode: WatermarkMode = WatermarkMode.DRAFT,
) -> bytes:
    """Stamp the overlay selected by ``mode`` on every page of source_pdf_bytes.

    The overlay is built with reportlab and merged page-by-page using pypdf.
    The form data (AcroForm fields) from source_pdf is preserved. ``mode``
    defaults to DRAFT so existing callers (the 1120 path) are unchanged; NONE
    returns the source untouched.
    """
    if mode == WatermarkMode.NONE:
        return source_pdf_bytes

    reader = PdfReader(io.BytesIO(source_pdf_bytes))
    num_pages = len(reader.pages)

    watermark_bytes = _build_watermark_overlay(num_pages, mode)
    wm_reader = PdfReader(io.BytesIO(watermark_bytes))

    writer = PdfWriter()

    # Re-attach AcroForm to preserve filled fields
    if reader.trailer.get("/Root") and reader.trailer["/Root"].get("/AcroForm"):
        writer._root_object.update({
            NameObject("/AcroForm"): reader.trailer["/Root"]["/AcroForm"]
        })
        try:
            acroform = writer._root_object["/AcroForm"].get_object()
            acroform.update({NameObject("/NeedAppearances"): BooleanObject(True)})
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
# Form W-4P (individual pension/annuity withholding certificate)
# ---------------------------------------------------------------------------
# W-4P is an individual certificate given to the PAYER (not the IRS). Its data
# shape is unlike the corporate 1120 (no queue_items / cpa_package), so it has a
# dedicated fill path rather than reusing build_prefill_context. We fill ONLY
# page-1 face fields (the certificate); the page-3/4 deductions worksheet is the
# taxpayer's scratch work and is not part of what goes to the payer.


@dataclass
class W4PFill:
    """Resolved page-1 W-4P certificate values, ready to stamp onto the form.

    Each value is a plain display string (already provenance-checked upstream by
    the caller). A value of None leaves that field blank — conservative
    blank-fill, never guessed. ``filing_status`` is one of the
    ``W4P_FILING_STATUS_FIELDS`` keys.
    """

    tax_year: int
    first_name_mi: str | None = None
    last_name: str | None = None
    address: str | None = None
    city_state_zip: str | None = None
    ssn: str | None = None
    filing_status: str | None = None
    no_withholding: bool = False
    step2a: str | None = None
    step2b: str | None = None
    step2c: str | None = None
    step3a_child: str | None = None
    step3b_other: str | None = None
    step3c_credits: str | None = None
    step3_total: str | None = None
    step4a_income: str | None = None
    step4b_deductions: str | None = None
    step4c_extra: str | None = None


def _w4p_form_path(tax_year: int) -> Path:
    """Resolve the W-4P source PDF, preferring a year-specific file."""
    candidates = [
        IRS_FORMS_DIR / f"f-w4p_{tax_year}.pdf",
        IRS_FORMS_DIR / "f-w4p.pdf",
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def _set_acroform_values(writer: PdfWriter, field_values: dict[str, str]) -> int:
    """Set AcroForm field values by walking the /Fields tree to terminal fields.

    Matches by fully-qualified field name (parent /T chain joined by '.'). Text
    fields get /V set and any cached /AP dropped (so /NeedAppearances regenerates
    the visible text); button/checkbox fields get /V and /AS set to the on-state
    NameObject (e.g. "1" -> /1). Returns the number of fields set. Unknown keys
    are simply not matched (conservative — never invents a field).
    """
    try:
        acroform = writer._root_object["/AcroForm"].get_object()
        root_fields = acroform["/Fields"]
    except (KeyError, AttributeError):
        return 0

    set_count = 0

    def walk(ref: Any, prefix: str) -> None:
        nonlocal set_count
        field = ref.get_object()
        partial = field.get("/T")
        name = (
            f"{prefix}.{partial}" if (prefix and partial is not None)
            else (partial if partial is not None else prefix)
        )
        kids = field.get("/Kids")
        kid_fields = []
        if kids:
            for k in kids:
                if k.get_object().get("/T") is not None:
                    kid_fields.append(k)

        ft = field.get("/FT")
        is_terminal = ft is not None and not kid_fields
        if is_terminal and name in field_values:
            value = str(field_values[name])
            if ft == "/Btn":
                on_state = NameObject("/" + value)
                field[NameObject("/V")] = on_state
                field[NameObject("/AS")] = on_state
            else:
                field[NameObject("/V")] = TextStringObject(value)
                if "/AP" in field:
                    del field[NameObject("/AP")]
            set_count += 1

        for k in kid_fields:
            walk(k, name)

    for fr in root_fields:
        walk(fr, "")
    return set_count


def _annot_qualified_name(annot: Any) -> str | None:
    """Build a widget annotation's fully-qualified field name from its /T chain."""
    parts: list[str] = []
    obj: Any = annot
    for _ in range(24):  # guard against cycles
        if obj is None:
            break
        t = obj.get("/T")
        if t is not None:
            parts.append(str(t))
        parent = obj.get("/Parent")
        obj = parent.get_object() if parent is not None else None
    return ".".join(reversed(parts)) if parts else None


def _build_value_overlay(reader: PdfReader, field_values: dict[str, str]) -> bytes:
    """Build an overlay PDF that draws each field's value at its widget /Rect.

    This BAKES the values into the page content so they are visible in EVERY
    viewer/printer — not dependent on /NeedAppearances (which poppler and many
    print paths ignore). Text fields draw left-aligned; checkboxes draw an 'X'.
    """
    from reportlab.lib.colors import black
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf)
    for page in reader.pages:
        mb = page.mediabox
        pw, ph = float(mb.width), float(mb.height)
        c.setPageSize((pw, ph))
        c.setFillColor(black)
        annots = page.get("/Annots")
        if annots:
            for ref in annots:
                ao = ref.get_object()
                if ao.get("/Subtype") != "/Widget":
                    continue
                name = _annot_qualified_name(ao)
                if not name or name not in field_values:
                    continue
                rect = ao.get("/Rect")
                if rect is None:
                    continue
                x0, y0, x1, y1 = (float(v) for v in rect)
                h = abs(y1 - y0)
                ft = ao.get("/FT")
                if ft is None and ao.get("/Parent") is not None:
                    ft = ao["/Parent"].get_object().get("/FT")
                value = str(field_values[name])
                if ft == "/Btn":
                    size = max(7.0, min(12.0, h * 0.8))
                    c.setFont("Helvetica-Bold", size)
                    c.drawCentredString((x0 + x1) / 2.0, min(y0, y1) + h * 0.22, "X")
                else:
                    size = max(7.0, min(10.0, h - 4.0))
                    c.setFont("Helvetica", size)
                    baseline = min(y0, y1) + max(2.0, (h - size) / 2.0)
                    c.drawString(min(x0, x1) + 2.0, baseline, value)
        c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()


def _flatten_widgets(writer: PdfWriter) -> None:
    """Drop interactive form widgets + the AcroForm so only baked-in values show.

    Without this, a forms-aware viewer would render an empty editable field on
    top of the baked overlay value (or double-render under /NeedAppearances).
    """
    for page in writer.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        kept = [r for r in annots if r.get_object().get("/Subtype") != "/Widget"]
        if kept:
            page[NameObject("/Annots")] = ArrayObject(kept)
        else:
            del page[NameObject("/Annots")]
    try:
        if "/AcroForm" in writer._root_object:
            del writer._root_object[NameObject("/AcroForm")]
    except (KeyError, AttributeError):
        pass


def _render_acroform_pdf(
    source_pdf_bytes: bytes,
    field_values: dict[str, str],
    watermark_mode: WatermarkMode,
) -> bytes:
    """Fill an AcroForm PDF with VISIBLE, flattened values + stamp the watermark.

    Modern IRS forms (W-4P, 1040) are XFA hybrids whose values, set only via /V,
    are invisible in any viewer that ignores /NeedAppearances (poppler, many
    print paths). So we: (1) set /V on each terminal field (semantic value, via
    the /Fields-tree walker — pypdf's page matcher misses XFA names), (2) BAKE
    each value as a coordinate overlay at its widget /Rect so it renders
    everywhere, (3) flatten (drop widgets + AcroForm) so nothing double-renders,
    (4) stamp the status watermark. The result is a finished, print-ready,
    sign-ready document — not a fragile fillable form.
    """
    writer = PdfWriter(clone_from=io.BytesIO(source_pdf_bytes))
    _set_acroform_values(writer, field_values)
    if "/XFA" in (writer._root_object.get("/AcroForm", {}) or {}):
        try:
            del writer._root_object["/AcroForm"].get_object()[NameObject("/XFA")]
        except (KeyError, AttributeError):
            pass

    # Bake the values in as a visible overlay (reads widget rects from the
    # cloned writer's pages).
    value_overlay = PdfReader(io.BytesIO(_build_value_overlay(writer, field_values)))
    for i, page in enumerate(writer.pages):
        if i < len(value_overlay.pages):
            page.merge_page(value_overlay.pages[i])

    _flatten_widgets(writer)

    if watermark_mode != WatermarkMode.NONE:
        wm_reader = PdfReader(
            io.BytesIO(_build_watermark_overlay(len(writer.pages), watermark_mode))
        )
        for i, page in enumerate(writer.pages):
            if i < len(wm_reader.pages):
                page.merge_page(wm_reader.pages[i])

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


def _build_w4p(fill: W4PFill, watermark_mode: WatermarkMode) -> bytes:
    """Fill the page-1 W-4P certificate and stamp the selected watermark."""
    from app.exports.w4p_field_map import (
        W4P_FIELDS,
        W4P_FILING_STATUS_FIELDS,
        W4P_NO_WITHHOLDING_FIELD,
    )

    form_path = _w4p_form_path(fill.tax_year)
    if not form_path.exists():
        raise FileNotFoundError(
            f"IRS Form W-4P PDF not found at {form_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source = form_path.read_bytes()

    field_values: dict[str, str] = {}
    text_values = {
        "first_name_mi": fill.first_name_mi,
        "last_name": fill.last_name,
        "address": fill.address,
        "city_state_zip": fill.city_state_zip,
        "ssn": fill.ssn,
        "step2a": fill.step2a,
        "step2b": fill.step2b,
        "step2c": fill.step2c,
        "step3a_child": fill.step3a_child,
        "step3b_other": fill.step3b_other,
        "step3c_credits": fill.step3c_credits,
        "step3_total": fill.step3_total,
        "step4a_income": fill.step4a_income,
        "step4b_deductions": fill.step4b_deductions,
        "step4c_extra": fill.step4c_extra,
    }
    for key, value in text_values.items():
        if value is not None and str(value) != "":
            field_values[W4P_FIELDS[key]] = str(value)

    if fill.filing_status and fill.filing_status in W4P_FILING_STATUS_FIELDS:
        fname, on_state = W4P_FILING_STATUS_FIELDS[fill.filing_status]
        field_values[fname] = on_state
    if fill.no_withholding:
        fname, on_state = W4P_NO_WITHHOLDING_FIELD
        field_values[fname] = on_state

    return _render_acroform_pdf(source, field_values, watermark_mode)


def generate_w4p_pdf(
    fill: W4PFill,
    watermark_mode: WatermarkMode = WatermarkMode.DRAFT,
) -> tuple[bytes, str]:
    """Generate a filled page-1 W-4P certificate PDF.

    Returns (pdf_bytes, "application/pdf"). ``watermark_mode`` reflects the
    work-product status: DRAFT (default) for an un-attested draft,
    SELF_CERTIFIED once a user with no professional has self-certified, or NONE
    when a licensed pro has finalized it.
    """
    return _build_w4p(fill, watermark_mode), "application/pdf"


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

    elif fn == FormName.W4P:
        # W-4P is an individual certificate with a different data shape; it does
        # not use the corporate queue/cpa-export context.
        raise ValueError(
            "W-4P uses an individual data shape — call generate_w4p_pdf(fill, "
            "watermark_mode) instead of generate_form_pdf()."
        )

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
