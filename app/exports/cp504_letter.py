"""CP504 response-letter generator (IRS "Notice of Intent to Levy" reply).

A CP504 is one of the more alarming notices the IRS sends: it states a balance is
due and that, if unpaid, the IRS intends to levy (seize) the taxpayer's state tax
refund and search for other assets to levy. This module produces a finished,
ready-to-sign RESPONSE LETTER the taxpayer mails back — the professional-substitute
work product the platform delivers when a CPA, EA, or tax attorney is not available
or affordable.

It is NOT legal advice. The letter is written in the taxpayer's own voice, as a
self-represented individual, and keeps a deliberately conservative, non-advice-y
posture (it asks the IRS to act, it does not assert legal conclusions). A separate
integration layer (app/api/work_products.py) stores the row and re-renders this PDF
on demand, so the watermark always reflects the current status.

ANTI-FABRICATION
----------------
Every dollar figure printed comes from the caller-provided ``inputs`` (each value
already validated upstream to carry a ``source_ref``). This module NEVER invents a
number: if ``amount_due`` is absent it phrases around it ("the balance shown on the
notice") rather than guessing, and it renders only values that are present and
non-blank — anything missing is simply left out of the letter.

Public interface (slots into the work-product renderer)::

    from app.exports.form_prefill import WatermarkMode

    def render_cp504(tax_year, watermark_mode, inputs) -> tuple[bytes, str]:
        # returns (pdf_bytes, "application/pdf")

Watermarking reuses ``_apply_watermark`` from form_prefill so DRAFT /
self-certified / clean stamping is identical across the whole suite.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any

from app.exports.form_prefill import WatermarkMode, _apply_watermark

# US Letter, matching the rest of the suite (612 x 792 pt).
LETTER_W = 612.0
LETTER_H = 792.0
MARGIN_X = 72.0  # 1.0 inch — business-letter margins
TOP_Y = LETTER_H - 72.0
BODY_FONT = "Helvetica"
BODY_SIZE = 10.5
LEADING = 14.0  # line spacing for body paragraphs

# Generic IRS service-center placeholder. We deliberately do NOT print a specific
# campus address: the correct return address is on the taxpayer's own notice and
# varies by region. The body + footer both instruct the taxpayer to mail to the
# address shown on their CP504, and this block is clearly a fill-in placeholder.
IRS_ADDRESS_BLOCK = (
    "Internal Revenue Service",
    "[Use the address shown on your CP504 notice]",
)


# ---------------------------------------------------------------------------
# Input access — render only present, non-blank values; never invent.
# ---------------------------------------------------------------------------

def _val(inputs: dict[str, Any], key: str) -> str | None:
    """Return a present, non-blank input value as a stripped string, else None.

    ``inputs`` maps field_key -> {"value", "source_ref", "origin"}. Provenance was
    validated upstream; here we only surface values that are actually present and
    non-blank so nothing is fabricated and nothing empty is drawn.
    """
    rec = inputs.get(key)
    if not isinstance(rec, dict):
        return None
    raw = rec.get("value")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


# ---------------------------------------------------------------------------
# Lightweight word-wrap (reportlab canvas has no flow layout)
# ---------------------------------------------------------------------------

def _wrap(text: str, font: str, size: float, max_width: float) -> list[str]:
    """Greedy word-wrap ``text`` to lines no wider than ``max_width`` points."""
    from reportlab.pdfbase.pdfmetrics import stringWidth

    words = text.split()
    if not words:
        return [""]
    lines: list[str] = []
    line = words[0]
    for word in words[1:]:
        candidate = f"{line} {word}"
        if stringWidth(candidate, font, size) <= max_width:
            line = candidate
        else:
            lines.append(line)
            line = word
    lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# RE: line + body composition (content depends on response_type)
# ---------------------------------------------------------------------------

# Human-readable label for the notice; falls back to a plain "CP504" if absent so
# the letter is always coherent.
def _notice_label(inputs: dict[str, Any]) -> str:
    return _val(inputs, "notice_number") or "CP504"


def _re_line(inputs: dict[str, Any], tax_year: int | None) -> str:
    """Build the RE: subject line, citing only the facts we actually have."""
    notice = _notice_label(inputs)
    parts = [f"Response to Notice {notice}"]

    ty = _val(inputs, "tax_year")
    if ty:
        parts.append(f"Tax Year {ty}")
    elif tax_year is not None:
        parts.append(f"Tax Year {tax_year}")

    amount = _val(inputs, "amount_due")
    if amount:
        parts.append(f"Balance shown: {amount}")

    ssn4 = _val(inputs, "ssn_last4")
    if ssn4:
        parts.append(f"SSN ending in {ssn4}")

    return "RE:  " + "  |  ".join(parts)


def _amount_phrase(inputs: dict[str, Any]) -> str:
    """Refer to the balance by figure when we have it, else by the notice.

    NEVER fabricates: with no ``amount_due`` it returns a phrase that points the
    reader to the figure printed on their own notice.
    """
    amount = _val(inputs, "amount_due")
    if amount:
        return f"the balance of {amount} shown on the notice"
    return "the balance shown on the notice"


def _opening_paragraph(inputs: dict[str, Any], tax_year: int | None) -> str:
    """Shared opening: identify the writer, the notice, and the year."""
    notice = _notice_label(inputs)
    ty = _val(inputs, "tax_year") or (str(tax_year) if tax_year is not None else None)
    year_clause = f" for the {ty} tax year" if ty else ""
    notice_date = _val(inputs, "notice_date")
    date_clause = f" dated {notice_date}" if notice_date else ""
    return (
        f"I am writing in response to Notice {notice}{date_clause}, which I received "
        f"regarding {_amount_phrase(inputs)}{year_clause}. I am writing on my own "
        f"behalf as the taxpayer and am addressing this notice promptly."
    )


def _body_paragraphs(inputs: dict[str, Any], tax_year: int | None) -> list[str]:
    """Return the ordered body paragraphs for the given ``response_type``.

    Each branch keeps a conservative, non-advice posture (the taxpayer is
    self-represented) and prints a dollar figure only when ``inputs`` supplies it.
    """
    response_type = (_val(inputs, "response_type") or "").lower()
    paras: list[str] = [_opening_paragraph(inputs, tax_year)]

    explanation = _val(inputs, "explanation")
    monthly = _val(inputs, "monthly_payment_offer")

    if response_type == "request_installment_agreement":
        offer = (
            f" I am able to pay approximately {monthly} per month toward this balance, "
            "and I am requesting a monthly payment amount and due date that fit my "
            "circumstances." if monthly else
            " I am requesting a monthly payment amount and due date that fit my "
            "circumstances."
        )
        paras.append(
            "I am requesting an installment agreement so that I can pay this balance "
            "over time rather than in a single payment." + offer + " I am enclosing a "
            "completed Form 9465, Installment Agreement Request, with this letter and "
            "ask that you set up the agreement and confirm the terms in writing."
        )
        paras.append(
            "I understand that interest and any applicable penalties continue to accrue "
            "until the balance is paid in full, and I intend to keep all future returns "
            "filed and current while this agreement is in place. I respectfully request "
            "that collection action, including any levy, be held while this request is "
            "considered and while I make the agreed payments."
        )

    elif response_type == "dispute_balance":
        paras.append(
            "I do not believe the balance shown on this notice is correct, and I am "
            "requesting that my account for this period be reviewed. I am asking the "
            "IRS to verify the amount assessed and to send me an explanation of how "
            f"{_amount_phrase(inputs)} was calculated, including any payments, "
            "withholding, or credits that may not have been applied to my account."
        )
        paras.append(
            "I am exercising my right to a review of this matter and, to the extent the "
            "notice provides for it, my right to appeal and to request a Collection Due "
            "Process hearing within the timeframe stated on the notice. I respectfully "
            "request that no levy or other collection action be taken while this dispute "
            "is being reviewed. If supporting documentation is needed from me, please "
            "tell me what to provide and I will respond promptly."
        )

    elif response_type == "request_cnc_hardship":
        paras.append(
            "Paying this balance now would prevent me from meeting necessary living "
            "expenses such as housing, utilities, food, and medical care. Because of "
            "this financial hardship, I am requesting that my account be placed in "
            "Currently Not Collectible status. I am willing to provide a Collection "
            "Information Statement (Form 433-F or 433-A) documenting my income, expenses, "
            "and assets so that you can evaluate my situation."
        )
        paras.append(
            "I respectfully request that collection action, including any levy of my "
            "refund, wages, or bank accounts, be held while this request is reviewed. I "
            "understand that interest and any applicable penalties may continue to accrue, "
            "and that the IRS may review my financial situation again in the future."
        )

    elif response_type == "pay_in_full_notice":
        paras.append(
            f"I acknowledge {_amount_phrase(inputs)} and I intend to pay it. I am "
            "treating this notice as a priority and am arranging payment of the amount "
            "due. Please apply my payment to this period and confirm in writing when the "
            "balance has been satisfied so that any intent to levy is withdrawn."
        )
        paras.append(
            "If any portion of the balance reflects interest or penalties that I may be "
            "able to have reduced, please let me know what relief I may be eligible to "
            "request. I intend to keep my account current going forward."
        )

    else:
        # Unknown / unspecified response_type — a safe, neutral acknowledgement that
        # never commits the taxpayer to a position or invents facts.
        paras.append(
            "I am responding to this notice to acknowledge that I have received it and "
            "that I am addressing the matter. I am reviewing the amount shown and the "
            "options available to me, and I am requesting that you provide any "
            "explanation of the balance and the deadlines that apply. I respectfully "
            "ask that collection action be held while I respond."
        )

    # Taxpayer-supplied explanation, if any, is appended verbatim (already
    # provenance-checked upstream) so it carries a source_ref.
    if explanation:
        paras.append(explanation)

    # Closing request for written confirmation + contact — neutral across all types.
    paras.append(
        "Please send your response and any further instructions to me at the address "
        "above. I am keeping a copy of this letter and the enclosed documents for my "
        "records. Thank you for your attention to this matter."
    )
    return paras


# ---------------------------------------------------------------------------
# PDF layout
# ---------------------------------------------------------------------------

def _draw_letter(inputs: dict[str, Any], tax_year: int | None) -> bytes:
    """Render the (un-watermarked) letter body to PDF bytes via reportlab.

    Lays out a clean US-Letter business letter. Paragraphs flow onto a second page
    if the body is long; the signature block and footer are kept together on the
    final page.
    """
    try:
        from reportlab.lib.colors import Color, black
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - reportlab is a hard dependency
        raise RuntimeError(
            "reportlab is required for CP504 letter generation. pip install reportlab"
        ) from exc

    content_w = LETTER_W - 2 * MARGIN_X
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(LETTER_W, LETTER_H))

    # Mutable cursor; page-break helper resets it on a new page.
    state = {"y": TOP_Y}
    BOTTOM_LIMIT = 96.0  # keep clear of the footer band

    def new_page() -> None:
        c.showPage()
        state["y"] = TOP_Y

    def ensure(space: float) -> None:
        if state["y"] - space < BOTTOM_LIMIT:
            new_page()

    def text_line(s: str, font: str = BODY_FONT, size: float = BODY_SIZE,
                  dy: float = LEADING, color=black) -> None:
        ensure(dy)
        c.setFont(font, size)
        c.setFillColor(color)
        c.drawString(MARGIN_X, state["y"], s)
        state["y"] -= dy

    def paragraph(s: str, gap: float = 8.0) -> None:
        for ln in _wrap(s, BODY_FONT, BODY_SIZE, content_w):
            text_line(ln)
        state["y"] -= gap

    def blank(space: float = LEADING) -> None:
        state["y"] -= space

    # --- Sender block (taxpayer name + address) ---
    name = _val(inputs, "taxpayer_name")
    if name:
        text_line(name, font="Helvetica-Bold", size=BODY_SIZE)
    address = _val(inputs, "taxpayer_address")
    if address:
        # Address may contain embedded newlines (multi-line street/city/state).
        for ln in address.replace("\r\n", "\n").split("\n"):
            ln = ln.strip()
            if ln:
                text_line(ln)
    if name or address:
        blank(LEADING)

    # --- Date line (generation date; the taxpayer dates the signature by hand too) ---
    today = datetime.now(tz=timezone.utc).strftime("%B %d, %Y")
    text_line(today)
    blank(LEADING)

    # --- IRS address block ---
    for ln in IRS_ADDRESS_BLOCK:
        text_line(ln)
    notice = _notice_label(inputs)
    text_line(f"Re: Notice {notice}")
    blank(LEADING)

    # --- RE: subject line (bold) ---
    for ln in _wrap(_re_line(inputs, tax_year), "Helvetica-Bold", BODY_SIZE, content_w):
        text_line(ln, font="Helvetica-Bold", size=BODY_SIZE)
    blank(LEADING)

    # --- Salutation ---
    text_line("To Whom It May Concern:")
    blank(8.0)

    # --- Body ---
    for para in _body_paragraphs(inputs, tax_year):
        paragraph(para)

    # --- Signature block (kept together on the final page) ---
    SIG_BLOCK_HEIGHT = 96.0
    ensure(SIG_BLOCK_HEIGHT)
    blank(LEADING)
    text_line("Sincerely,")
    blank(LEADING * 2)  # whitespace for a wet signature
    # Signature line
    c.setStrokeColor(black)
    c.setLineWidth(0.75)
    sig_line_y = state["y"]
    c.line(MARGIN_X, sig_line_y, MARGIN_X + 240, sig_line_y)
    # Date line to the right
    c.line(MARGIN_X + 300, sig_line_y, MARGIN_X + 300 + 150, sig_line_y)
    state["y"] -= 12
    c.setFont(BODY_FONT, 8.5)
    c.setFillColor(Color(0.35, 0.35, 0.35))
    c.drawString(MARGIN_X, state["y"], "Signature")
    c.drawString(MARGIN_X + 300, state["y"], "Date")
    state["y"] -= 16
    # Printed name under the signature line
    if name:
        text_line(name, font="Helvetica-Bold", size=BODY_SIZE, color=black)
    ssn4 = _val(inputs, "ssn_last4")
    if ssn4:
        text_line(f"Taxpayer SSN ending in {ssn4}", size=9.0, color=Color(0.35, 0.35, 0.35))

    # --- "What to do next" footer on the final page (bottom band) ---
    footer_y = 60.0
    c.setStrokeColor(Color(0.6, 0.6, 0.6))
    c.setLineWidth(0.5)
    c.line(MARGIN_X, footer_y + 26, LETTER_W - MARGIN_X, footer_y + 26)
    c.setFont("Helvetica-Bold", 8.0)
    c.setFillColor(Color(0.3, 0.3, 0.3))
    c.drawString(MARGIN_X, footer_y + 14, "What to do next:")
    c.setFont("Helvetica", 8.0)
    c.setFillColor(Color(0.4, 0.4, 0.4))
    c.drawString(
        MARGIN_X, footer_y + 4,
        "Sign and date above. Keep a copy for your records. Mail by USPS Certified "
        "Mail (return receipt) to the address printed on your notice.",
    )
    c.drawString(
        MARGIN_X, footer_y - 6,
        "This letter is a self-representation work product prepared by Accounting-Ops. "
        "It is not legal advice.",
    )

    c.save()
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_cp504(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Render a CP504 response letter PDF. Returns (pdf_bytes, "application/pdf").

    ``inputs`` maps field_key -> {"value", "source_ref", "origin"}. Provenance has
    been validated upstream; this function renders only present, non-blank values and
    never invents a figure. ``watermark_mode`` (DRAFT / SELF_CERTIFIED / NONE) is
    applied via the shared ``_apply_watermark`` so stamping matches the rest of the
    suite.

    Supported input keys (all optional; rendered gracefully when absent):
        taxpayer_name, taxpayer_address, ssn_last4, notice_number, notice_date,
        tax_year, amount_due, response_type, monthly_payment_offer, explanation.

    ``response_type`` is one of: "request_installment_agreement", "dispute_balance",
    "request_cnc_hardship", "pay_in_full_notice" (any other / missing value yields a
    safe neutral acknowledgement letter).
    """
    inputs = inputs or {}
    letter_bytes = _draw_letter(inputs, tax_year)
    return _apply_watermark(letter_bytes, watermark_mode), "application/pdf"
