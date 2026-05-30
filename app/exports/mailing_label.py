"""USPS mailing label PDF generator.

Generates a 4×6" PDF mailing label suitable for printing on an Avery 5168 /
Avery 8168 (or similar 4×6 label) or for use as a shipping-label sheet via
USPS Click-N-Ship.

IMPORTANT: We do NOT print postage. The output is the VISUAL of a Certified
Mail label — the address blocks, the tracking-number barcode placeholder, and
the certified-mail marking. The user takes this to USPS to pay for postage and
the actual Certified Mail tracking sticker, OR uses Click-N-Ship to buy
postage online and prints the resulting label themselves.

The QR code on the label encodes the tracking number (if known) so a phone
camera can pull it up in USPS Tracking immediately. If no tracking number is
known yet, the QR code is omitted and a blank space is left for the USPS
counter-applied sticker.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

# Same address book as the cover sheet
from app.exports.cover_sheet import (
    DEFAULT_MU_INC_RETURN_ADDRESS,
    TAX_OFFICE_ADDRESSES,
)

# 4 × 6 inch label in points (72 pt/inch)
LABEL_W = 4 * 72.0  # 288 pt
LABEL_H = 6 * 72.0  # 432 pt


@dataclass
class MailingLabelData:
    """Inputs for label generation."""
    # Where the envelope goes
    to_office_key: str | None = None
    to_office_custom: dict[str, str] | None = None

    # Return address (defaults to MU Inc)
    return_address: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_MU_INC_RETURN_ADDRESS)
    )

    # Tracking number (optional). If present, encoded as QR for phone lookup.
    tracking_number: str | None = None

    # Marking on the label face — defaults to "CERTIFIED MAIL"
    service_marking: str = "CERTIFIED MAIL\nRETURN RECEIPT REQUESTED"

    # Optional reference (e.g. "MU Inc EIN 00-0000000 — TY 2023 SC1120")
    sender_reference: str | None = None


def _resolve_to_address(data: MailingLabelData) -> dict[str, str]:
    """Look up the destination address."""
    if data.to_office_custom:
        return data.to_office_custom
    if data.to_office_key:
        addr = TAX_OFFICE_ADDRESSES.get(data.to_office_key)
        if addr:
            return addr
    return {
        "name": "[DESTINATION OFFICE — fill in]",
        "line1": "",
        "line2": "",
        "city_state_zip": "",
    }


def generate_mailing_label_pdf(data: MailingLabelData) -> bytes:
    """Render the mailing-label PDF (4×6 inch) and return bytes.

    Layout (top to bottom):
      - "CERTIFIED MAIL" marking (top-left), tracking QR (top-right)
      - Return address (above-center)
      - Recipient address (centered, largest text)
      - Sender reference (small, bottom)
    """
    try:
        from reportlab.lib.colors import Color, black, white
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for mailing-label generation. "
            "pip install reportlab"
        ) from exc

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LABEL_W, LABEL_H))

    # Border (1pt black box around the entire label so it cuts cleanly)
    c.setStrokeColor(black)
    c.setLineWidth(0.75)
    c.rect(4, 4, LABEL_W - 8, LABEL_H - 8, fill=0, stroke=1)

    margin = 14.0

    # --- Top band: certified mail marking + QR code ---
    band_top = LABEL_H - margin
    # Black bar with "CERTIFIED MAIL" — left side
    c.setFillColor(black)
    bar_h = 26.0
    c.rect(margin, band_top - bar_h, LABEL_W * 0.55 - margin, bar_h, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin + 6, band_top - bar_h + 9, "CERTIFIED MAIL")

    # "RETURN RECEIPT REQUESTED" underneath
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin, band_top - bar_h - 12, "RETURN RECEIPT REQUESTED")

    # --- QR code (top-right) ---
    qr_size = 70.0
    qr_x = LABEL_W - margin - qr_size
    qr_y = band_top - qr_size
    if data.tracking_number:
        try:
            from reportlab.graphics.barcode.qr import QrCodeWidget
            from reportlab.graphics.shapes import Drawing
            from reportlab.graphics import renderPDF

            qr = QrCodeWidget(data.tracking_number)
            bounds = qr.getBounds()
            qr_w = bounds[2] - bounds[0]
            qr_h = bounds[3] - bounds[1]
            d = Drawing(qr_size, qr_size, transform=[qr_size / qr_w, 0, 0, qr_size / qr_h, 0, 0])
            d.add(qr)
            renderPDF.draw(d, c, qr_x, qr_y)
            # Tracking number text under QR
            c.setFont("Courier", 7)
            c.drawString(qr_x, qr_y - 10, data.tracking_number[:25])
        except ImportError:
            # Fall back to a box
            c.setStrokeColor(black)
            c.rect(qr_x, qr_y, qr_size, qr_size, fill=0, stroke=1)
            c.setFont("Helvetica", 7)
            c.drawString(qr_x + 4, qr_y + qr_size / 2, "QR module not available")
    else:
        # Empty placeholder box for the USPS counter sticker
        c.setStrokeColor(Color(0.6, 0.6, 0.6))
        c.setLineWidth(0.5)
        c.rect(qr_x, qr_y, qr_size, qr_size, fill=0, stroke=1)
        c.setFont("Helvetica-Oblique", 7)
        c.setFillColor(Color(0.5, 0.5, 0.5))
        c.drawCentredString(
            qr_x + qr_size / 2, qr_y + qr_size / 2,
            "Affix USPS"
        )
        c.drawCentredString(
            qr_x + qr_size / 2, qr_y + qr_size / 2 - 10,
            "Certified Mail"
        )
        c.drawCentredString(
            qr_x + qr_size / 2, qr_y + qr_size / 2 - 20,
            "sticker here"
        )

    # --- Return address block (above center, smaller font) ---
    c.setFillColor(black)
    ret_y = LABEL_H - 150
    c.setFont("Helvetica-Bold", 7)
    c.drawString(margin, ret_y + 14, "FROM:")
    c.setFont("Helvetica", 8)
    ret_lines = [
        data.return_address.get("name", ""),
        data.return_address.get("line1", ""),
        data.return_address.get("line2", ""),
        data.return_address.get("city_state_zip", ""),
    ]
    line_y = ret_y
    for line in ret_lines:
        if line:
            c.drawString(margin, line_y, line[:48])
            line_y -= 10

    # --- Recipient address (centered, large) ---
    c.setStrokeColor(black)
    c.setLineWidth(0.5)
    # Light divider above recipient
    c.line(margin, LABEL_H / 2 + 24, LABEL_W - margin, LABEL_H / 2 + 24)

    to_addr = _resolve_to_address(data)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin, LABEL_H / 2 + 10, "TO:")
    c.setFont("Helvetica-Bold", 12)
    to_lines = [
        to_addr.get("name", ""),
        to_addr.get("line1", ""),
        to_addr.get("line2", ""),
        to_addr.get("city_state_zip", ""),
    ]
    line_y = LABEL_H / 2 - 8
    for line in to_lines:
        if line:
            c.drawString(margin + 10, line_y, line[:48])
            line_y -= 16

    # --- Sender reference (bottom) ---
    c.setFont("Helvetica-Oblique", 7)
    c.setFillColor(Color(0.4, 0.4, 0.4))
    if data.sender_reference:
        c.drawString(margin, 18, f"Ref: {data.sender_reference[:80]}")
    c.drawString(
        margin, 8,
        "Pay postage at USPS counter or via Click-N-Ship. Do not affix postage at home unless verified."
    )

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()
