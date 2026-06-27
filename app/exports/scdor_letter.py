"""South Carolina DOR notice-response letter generator.

The South Carolina Department of Revenue (SCDOR) sends notices identified by a
"Letter ID" (e.g. L0033888986) — a corporate Failure to File, a proposed
assessment, a balance-due bill, and so on. This module produces a finished,
ready-to-sign RESPONSE LETTER the taxpayer (or their representative) mails back —
the professional-substitute work product the platform delivers when a CPA, EA, or
tax attorney is not available or affordable.

It is modeled directly on ``cp504_letter.py`` (the IRS CP504 reply) and shares its
structure, helpers, and conservative posture. It is NOT legal advice. The letter is
written in the taxpayer's own voice, as a self-represented filer, and asks the
Department to act rather than asserting legal conclusions. A separate integration
layer (app/api/work_products.py) stores the row and re-renders this PDF on demand,
so the watermark always reflects the current status.

ANTI-FABRICATION
----------------
Every figure and identifier printed comes from the caller-provided ``inputs`` (each
value already validated upstream to carry a ``source_ref``). This module NEVER
invents a number or an ID: if ``amount_due`` is absent it phrases around it ("the
balance shown on the notice") rather than guessing, and it renders only values that
are present and non-blank — anything missing is simply left out of the letter.

Public interface (slots into the work-product renderer)::

    from app.exports.form_prefill import WatermarkMode

    def render_scdor_response(tax_year, watermark_mode, inputs) -> tuple[bytes, str]:
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

# Default SCDOR correspondence address. SCDOR routes most notice replies to its
# Columbia PO box; if the notice prints a different return address the caller can
# override it via the ``mailing_address`` input and we use that verbatim instead.
SCDOR_ADDRESS_BLOCK = (
    "South Carolina Department of Revenue",
    "PO Box 125",
    "Columbia, SC 29214-0400",
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
# Recipient address + RE: line + body composition (content depends on
# response_type)
# ---------------------------------------------------------------------------

def _recipient_block(inputs: dict[str, Any]) -> list[str]:
    """The SCDOR address the letter is mailed to.

    Uses the caller-provided ``mailing_address`` verbatim when present (the notice
    may print a unit-specific return address); otherwise falls back to the standard
    Columbia correspondence PO box. Never invents an address.
    """
    mailing = _val(inputs, "mailing_address")
    if mailing:
        lines = [ln.strip() for ln in mailing.replace("\r\n", "\n").split("\n")]
        return [ln for ln in lines if ln]
    return list(SCDOR_ADDRESS_BLOCK)


def _notice_label(inputs: dict[str, Any]) -> str:
    """Human-readable label for the notice; falls back to a plain "notice"."""
    return _val(inputs, "notice_title") or "notice"


def _letter_id(inputs: dict[str, Any]) -> str | None:
    """The SCDOR Letter ID (e.g. L0033888986), if supplied."""
    return _val(inputs, "letter_id")


def _re_line(inputs: dict[str, Any], tax_year: int | None) -> str:
    """Build the RE: subject line, citing only the facts we actually have."""
    notice = _notice_label(inputs)
    parts = [f"Response to SCDOR {notice}"]

    letter_id = _letter_id(inputs)
    if letter_id:
        parts.append(f"Letter ID {letter_id}")

    file_number = _val(inputs, "file_number")
    if file_number:
        parts.append(f"File No. {file_number}")

    account_id = _val(inputs, "account_id")
    if account_id:
        parts.append(f"Account {account_id}")

    period = _val(inputs, "tax_period")
    if period:
        parts.append(f"Period {period}")
    elif tax_year is not None:
        parts.append(f"Tax Year {tax_year}")

    amount = _val(inputs, "amount_due")
    if amount:
        parts.append(f"Balance shown: {amount}")

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


def _identifier_clause(inputs: dict[str, Any]) -> str:
    """A trailing clause naming the Letter ID / file number / period we have.

    Used inside body paragraphs so each branch references the specific notice. Only
    identifiers actually present are named; with none, returns an empty string.
    """
    bits: list[str] = []
    letter_id = _letter_id(inputs)
    if letter_id:
        bits.append(f"Letter ID {letter_id}")
    file_number = _val(inputs, "file_number")
    if file_number:
        bits.append(f"file number {file_number}")
    period = _val(inputs, "tax_period")
    if period:
        bits.append(f"the {period} period")
    if not bits:
        return ""
    if len(bits) == 1:
        return f" This notice references {bits[0]}."
    return f" This notice references {', '.join(bits[:-1])} and {bits[-1]}."


def _opening_paragraph(inputs: dict[str, Any], tax_year: int | None) -> str:
    """Shared opening: identify the writer, the notice, and the period."""
    notice = _notice_label(inputs)
    letter_id = _letter_id(inputs)
    id_clause = f" (Letter ID {letter_id})" if letter_id else ""
    period = _val(inputs, "tax_period")
    period_clause = (
        f" for the {period} period" if period
        else (f" for the {tax_year} tax year" if tax_year is not None else "")
    )
    rep = _val(inputs, "representative_name")
    taxpayer = _val(inputs, "taxpayer_name")
    if rep:
        who = (
            f"I am writing on behalf of {taxpayer} as the taxpayer's authorized "
            "representative" if taxpayer else
            "I am writing as the taxpayer's authorized representative"
        )
    else:
        who = "I am writing on my own behalf as the taxpayer"
    return (
        f"I am writing in response to the South Carolina Department of Revenue "
        f"{notice}{id_clause}, which I received regarding {_amount_phrase(inputs)}"
        f"{period_clause}. {who} and am addressing this notice promptly."
    )


def _body_paragraphs(inputs: dict[str, Any], tax_year: int | None) -> list[str]:
    """Return the ordered body paragraphs for the given ``response_type``.

    Each branch keeps a conservative, non-advice posture (the taxpayer is
    self-represented) and prints a figure only when ``inputs`` supplies it.
    """
    response_type = (_val(inputs, "response_type") or "").lower()
    paras: list[str] = [_opening_paragraph(inputs, tax_year)]

    explanation = _val(inputs, "explanation")
    monthly = _val(inputs, "monthly_payment_offer")
    file_by = _val(inputs, "file_by_date")
    id_clause = _identifier_clause(inputs)

    if response_type == "intent_to_file":
        by_clause = (
            f" The return is being prepared and will be filed by {file_by}."
            if file_by else
            " The return is being prepared and will be filed as soon as it is "
            "complete."
        )
        paras.append(
            "I acknowledge this Failure to File notice and am writing to confirm "
            "that the required return is being prepared." + by_clause + id_clause +
            " I respectfully request that the failure-to-file penalty assessed on "
            "this notice be abated once the return is filed."
        )
        paras.append(
            "I am treating this matter as a priority and intend to bring the account "
            "into full compliance. Please confirm in writing that no further "
            "collection action will be taken while the return is being completed and "
            "filed, and please advise if there is any additional information the "
            "Department needs from me in the meantime."
        )

    elif response_type == "request_abatement":
        paras.append(
            "I am requesting that the penalty assessed on this notice be abated. The "
            f"balance is small relative to my overall history of compliance, and I "
            "have otherwise filed and paid as required. I am asking the Department to "
            "consider both first-time penalty relief and reasonable-cause relief, as "
            "the circumstances that led to this notice were not the result of willful "
            "neglect." + id_clause
        )
        paras.append(
            f"I am prepared to pay {_amount_phrase(inputs)} excluding the penalty, and "
            "I respectfully request written confirmation of the corrected balance once "
            "the penalty has been removed. If documentation supporting reasonable cause "
            "would help your review, please tell me what to provide and I will respond "
            "promptly."
        )

    elif response_type == "request_installment":
        offer = (
            f" I am able to pay approximately {monthly} per month toward this balance, "
            "and I am requesting a monthly payment amount and due date that fit my "
            "circumstances." if monthly else
            " I am requesting a monthly payment amount and due date that fit my "
            "circumstances."
        )
        paras.append(
            "I am requesting a payment plan so that I can pay this balance over time "
            "rather than in a single payment." + offer + id_clause +
            " Please set up the agreement and confirm the terms in writing, including "
            "where I should send each payment."
        )
        paras.append(
            "I understand that interest and any applicable penalties may continue to "
            "accrue until the balance is paid in full, and I intend to keep all future "
            "returns filed and current while this agreement is in place. I respectfully "
            "request that collection action be held while this request is considered "
            "and while I make the agreed payments."
        )

    elif response_type == "dispute":
        paras.append(
            "I do not believe the assessment shown on this notice is correct, and I am "
            "requesting that my account for this period be reviewed. I am asking the "
            "Department to verify the amount assessed and to send me an explanation of "
            f"how {_amount_phrase(inputs)} was calculated, including any returns, "
            "payments, or credits that may not have been applied to my account." +
            id_clause
        )
        paras.append(
            "I am exercising my right to protest this assessment within the timeframe "
            "stated on the notice, and I respectfully request that no further "
            "collection action be taken while this dispute is being reviewed. If "
            "supporting documentation is needed from me, please tell me what to provide "
            "and I will respond promptly."
        )

    elif response_type == "acknowledge_paid":
        paras.append(
            f"I am writing to confirm that {_amount_phrase(inputs)} has been paid. I am "
            "treating this notice as resolved and am requesting that the Department "
            "apply my payment to this period and clear the account." + id_clause +
            " Please update your records to reflect a zero balance and confirm in "
            "writing once the account has been cleared."
        )
        paras.append(
            "If your records show any remaining balance after my payment is applied, "
            "please let me know the amount and how it was calculated so that I can "
            "address it. I intend to keep my account current going forward."
        )

    else:
        # Unknown / unspecified response_type — a safe, neutral acknowledgement that
        # never commits the taxpayer to a position or invents facts.
        paras.append(
            "I am responding to this notice to acknowledge that I have received it and "
            "that I am addressing the matter. I am reviewing the amount shown and the "
            "options available to me, and I am requesting that the Department provide "
            "any explanation of the balance and the deadlines that apply." + id_clause +
            " I respectfully ask that collection action be held while I respond."
        )

    # Taxpayer-supplied explanation, if any, is appended verbatim (already
    # provenance-checked upstream) so it carries a source_ref.
    if explanation:
        paras.append(explanation)

    # Closing request for written confirmation + contact — neutral across all types.
    contact = _contact_sentence(inputs)
    paras.append(
        "Please send your response and any further instructions to me at the address "
        "above." + contact + " I am keeping a copy of this letter and any enclosed "
        "documents for my records. Thank you for your attention to this matter."
    )
    return paras


def _contact_sentence(inputs: dict[str, Any]) -> str:
    """An optional sentence offering a phone/email contact, if provided."""
    phone = _val(inputs, "contact_phone")
    email = _val(inputs, "contact_email")
    if phone and email:
        return f" You may also reach me by phone at {phone} or by email at {email}."
    if phone:
        return f" You may also reach me by phone at {phone}."
    if email:
        return f" You may also reach me by email at {email}."
    return ""


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
            "reportlab is required for SCDOR letter generation. pip install reportlab"
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

    # --- Sender block (taxpayer name + address; representative noted if present) ---
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
    rep = _val(inputs, "representative_name")
    if rep:
        text_line(f"c/o {rep}, Authorized Representative")
    if name or address or rep:
        blank(LEADING)

    # --- Date line (generation date; the signer dates the signature by hand too) ---
    today = datetime.now(tz=timezone.utc).strftime("%B %d, %Y")
    text_line(today)
    blank(LEADING)

    # --- SCDOR recipient address block ---
    for ln in _recipient_block(inputs):
        text_line(ln)
    letter_id = _letter_id(inputs)
    if letter_id:
        text_line(f"Re: Letter ID {letter_id}")
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
    # Printed name under the signature line — the representative signs for the
    # taxpayer when one is named, otherwise the taxpayer signs.
    signer = rep or name
    if signer:
        text_line(signer, font="Helvetica-Bold", size=BODY_SIZE, color=black)
    if rep and name:
        text_line(f"Authorized Representative for {name}", size=9.0,
                  color=Color(0.35, 0.35, 0.35))
    account_id = _val(inputs, "account_id")
    if account_id:
        text_line(f"Account ID: {account_id}", size=9.0, color=Color(0.35, 0.35, 0.35))

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
        "Mail (return receipt) to the address shown on your SCDOR notice.",
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

def render_scdor_response(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Render an SCDOR notice-response letter PDF. Returns (pdf_bytes, "application/pdf").

    ``inputs`` maps field_key -> {"value", "source_ref", "origin"}. Provenance has
    been validated upstream; this function renders only present, non-blank values and
    never invents a figure or identifier. ``watermark_mode`` (DRAFT / SELF_CERTIFIED /
    NONE) is applied via the shared ``_apply_watermark`` so stamping matches the rest
    of the suite.

    Supported input keys (all optional; rendered gracefully when absent):
        taxpayer_name, taxpayer_address, representative_name, letter_id,
        notice_title, file_number, account_id, tax_period, amount_due,
        response_type, explanation, contact_phone, contact_email, file_by_date,
        monthly_payment_offer, mailing_address.

    ``response_type`` is one of: "intent_to_file", "request_abatement",
    "request_installment", "dispute", "acknowledge_paid" (any other / missing value
    yields a safe neutral acknowledgement letter). The letter is addressed to the
    South Carolina Department of Revenue, PO Box 125, Columbia, SC 29214-0400 unless
    ``mailing_address`` overrides it.
    """
    inputs = inputs or {}
    letter_bytes = _draw_letter(inputs, tax_year)
    return _apply_watermark(letter_bytes, watermark_mode), "application/pdf"
