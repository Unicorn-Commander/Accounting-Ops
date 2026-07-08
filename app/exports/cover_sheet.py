"""Cover sheet PDF generator for tax submission packets.

Generates a 1-page cover sheet that goes on top of every paper submission:
  - Submission title (e.g. "TY 2023 SC1120 — Cure Filing")
  - Entity name + EIN
  - Tax year
  - Form list ("Enclosed:")
  - Mailing details (to + from + tracking #)
  - DRAFT watermark
  - Generated timestamp + platform attribution

The cover sheet is intentionally minimal — printed and stapled to the front of
the packet so the receiving clerk can route it without opening the envelope.

Uses reportlab for vector PDF generation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Canonical SCDOR + IRS mailing addresses for SC-based corporations.
# All cover sheets pull from this dict so the user only has to fix one place
# if SCDOR/IRS republishes an address.
TAX_OFFICE_ADDRESSES: dict[str, dict[str, str]] = {
    "scdor_corporate": {
        "name": "South Carolina Department of Revenue",
        "line1": "Corporate Tax Section",
        "line2": "P.O. Box 125",
        "city_state_zip": "Columbia, SC 29214-0400",
    },
    "scdor_voucher": {
        "name": "South Carolina Department of Revenue",
        "line1": "Corporate Voucher",
        "line2": "P.O. Box 100151",
        "city_state_zip": "Columbia, SC 29202-3151",
    },
    "irs_1120_sc_nopayment": {
        "name": "Internal Revenue Service",
        "line1": "P.O. Box 409101",
        "line2": "",
        "city_state_zip": "Ogden, UT 84409",
    },
    "irs_1120_sc_withpayment": {
        "name": "Internal Revenue Service",
        "line1": "P.O. Box 802503",
        "line2": "",
        "city_state_zip": "Cincinnati, OH 45280-2503",
    },
}


# Generic placeholder return address. Callers should pass the entity's real
# return address explicitly (e.g. from workspace/entity settings); this default
# ships as an obviously-fake example so no real address is baked into the source.
DEFAULT_RETURN_ADDRESS = {
    "name": "Example Corp Inc.",
    "line1": "123 Example Street",
    "line2": "",
    "city_state_zip": "Anytown, ST 00000",
}


@dataclass
class CoverSheetData:
    """Inputs for cover-sheet generation."""
    # What this submission is about
    submission_title: str  # e.g. "TY 2023 SC1120 — Failure-to-File Cure Packet"
    tax_year: int
    entity_name: str
    entity_ein: str  # raw EIN string, e.g. "12-3456789"

    # Where it's going (use TAX_OFFICE_ADDRESSES key, or pass a custom address dict)
    to_office_key: str | None = None
    to_office_custom: dict[str, str] | None = None

    # Return address
    return_address: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_RETURN_ADDRESS)
    )

    # Forms / documents enclosed (human-readable strings)
    enclosures: list[str] = field(default_factory=list)

    # Certified Mail tracking number (placeholder OK; user writes in by hand after USPS)
    tracking_number: str | None = None

    # Optional: caller note printed under the "Notes" section
    notes: str | None = None


def _resolve_to_address(data: CoverSheetData) -> dict[str, str]:
    """Look up the destination address."""
    if data.to_office_custom:
        return data.to_office_custom
    if data.to_office_key:
        addr = TAX_OFFICE_ADDRESSES.get(data.to_office_key)
        if addr:
            return addr
    # Unknown destination — placeholder
    return {
        "name": "[DESTINATION OFFICE — fill in]",
        "line1": "",
        "line2": "",
        "city_state_zip": "",
    }


def generate_cover_sheet_pdf(data: CoverSheetData) -> bytes:
    """Render the cover-sheet PDF and return bytes.

    Layout (top to bottom):
      - Yellow DRAFT banner at top (consistent with other DRAFT artifacts)
      - Title in 24pt bold
      - Entity block
      - To/From address blocks (side by side)
      - "Enclosed" list (bulleted)
      - Tracking number callout
      - Footer with generation timestamp + draft warning
    """
    try:
        from reportlab.lib.colors import Color, HexColor, black
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for cover-sheet generation. "
            "pip install reportlab"
        ) from exc

    LETTER_W = 612.0
    LETTER_H = 792.0
    margin_x = 54.0  # 0.75 inch
    cursor_y = LETTER_H - 36.0  # 0.5 inch from top

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))

    # --- DRAFT banner at top (consistent with form_prefill) ---
    banner_yellow = HexColor("#FFC83D")
    banner_text_color = Color(0.3, 0.15, 0.0, alpha=1.0)
    banner_height = 28.0
    c.setFillColor(banner_yellow)
    c.rect(0, LETTER_H - banner_height, LETTER_W, banner_height, fill=1, stroke=0)
    c.setFillColor(banner_text_color)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(
        LETTER_W / 2,
        LETTER_H - banner_height + 9,
        "DRAFT COVER SHEET — review with CPA before mailing",
    )

    cursor_y -= banner_height + 24

    # --- Title ---
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(margin_x, cursor_y, data.submission_title[:60])
    cursor_y -= 28

    c.setFont("Helvetica-Oblique", 11)
    c.drawString(
        margin_x, cursor_y,
        f"Tax Year {data.tax_year}  ·  {data.entity_name}  ·  EIN {data.entity_ein}"
    )
    cursor_y -= 36

    # --- To / From two-column ---
    col_w = (LETTER_W - 2 * margin_x - 24) / 2

    def _draw_address(x: float, y: float, label: str, addr: dict[str, str]):
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x, y, label)
        c.setFont("Helvetica", 11)
        addr_lines = [
            addr.get("name", ""),
            addr.get("line1", ""),
            addr.get("line2", ""),
            addr.get("city_state_zip", ""),
        ]
        line_y = y - 16
        for line in addr_lines:
            if line:
                c.drawString(x, line_y, line)
                line_y -= 14

    to_addr = _resolve_to_address(data)
    _draw_address(margin_x, cursor_y, "MAIL TO:", to_addr)
    _draw_address(margin_x + col_w + 24, cursor_y, "FROM:", data.return_address)

    cursor_y -= 110

    # --- Tracking number callout (boxed) ---
    box_x = margin_x
    box_w = LETTER_W - 2 * margin_x
    box_h = 36
    c.setStrokeColor(black)
    c.setLineWidth(1)
    c.rect(box_x, cursor_y - box_h, box_w, box_h, fill=0, stroke=1)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(box_x + 8, cursor_y - 14, "USPS CERTIFIED MAIL — Tracking Number:")
    c.setFont("Courier", 13)
    tracking_display = data.tracking_number or "_____ _____ _____ _____ ___"
    c.drawString(box_x + 8, cursor_y - 30, tracking_display)
    cursor_y -= box_h + 20

    # --- Enclosures ---
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin_x, cursor_y, "Enclosed:")
    cursor_y -= 18
    c.setFont("Helvetica", 11)
    # Filter out None entries (caller may have conditionals like
    # `"Form X" if year >= 2024 else None`).
    enclosures = [e for e in (data.enclosures or []) if e]
    if enclosures:
        for enc in enclosures:
            c.drawString(margin_x + 12, cursor_y, f"• {enc[:90]}")
            cursor_y -= 14
    else:
        c.drawString(margin_x + 12, cursor_y, "• (no enclosures listed)")
        cursor_y -= 14
    cursor_y -= 18

    # --- Notes ---
    if data.notes:
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin_x, cursor_y, "Notes:")
        cursor_y -= 16
        c.setFont("Helvetica", 11)
        # Word-wrap the notes manually at 95 chars
        words = data.notes.split()
        line: list[str] = []
        line_len = 0
        for word in words:
            if line_len + len(word) + 1 > 95:
                c.drawString(margin_x + 12, cursor_y, " ".join(line))
                cursor_y -= 14
                line, line_len = [word], len(word)
            else:
                line.append(word)
                line_len += len(word) + 1
        if line:
            c.drawString(margin_x + 12, cursor_y, " ".join(line))
            cursor_y -= 14
        cursor_y -= 12

    # --- Footer ---
    footer_y = 48
    c.setFont("Helvetica-Oblique", 8)
    c.setFillColor(Color(0.4, 0.4, 0.4))
    generated = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    c.drawString(
        margin_x, footer_y + 12,
        f"Generated by Accounting-Ops at {generated}  ·  Sign + date before mailing."
    )
    c.drawString(
        margin_x, footer_y,
        "DRAFT — not a substitute for CPA review. Verify every line on the enclosed return."
    )

    c.save()
    buf.seek(0)
    return buf.read()
