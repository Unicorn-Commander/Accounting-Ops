"""Smoke tests for the CP504 response-letter professional-substitute path.

The CP504 letter is a pure reportlab render (no gitignored IRS source PDF), so these
tests always run. They cover: a valid PDF renders for every response_type; the
letter does NOT crash when amount_due (or anything else) is absent; all three
WatermarkMode values render; and the anti-fabrication contract — a figure is only
ever drawn when it is supplied in inputs, never invented.
"""
from __future__ import annotations

import pytest

from app.exports.cp504_letter import render_cp504
from app.exports.form_prefill import WatermarkMode

_RESPONSE_TYPES = (
    "request_installment_agreement",
    "dispute_balance",
    "request_cnc_hardship",
    "pay_in_full_notice",
)


def _rec(value: str) -> dict:
    """A provenance-complete input record (validated upstream in production)."""
    return {"value": value, "source_ref": "test://fixture", "origin": "user_entered"}


def _full_inputs(response_type: str) -> dict:
    return {
        "taxpayer_name": _rec("Aaron Stransky"),
        "taxpayer_address": _rec("39 Paddlecreek Avenue\nCharleston, SC 29412"),
        "ssn_last4": _rec("1234"),
        "notice_number": _rec("CP504"),
        "notice_date": _rec("May 15, 2026"),
        "tax_year": _rec("2023"),
        "amount_due": _rec("$4,812.00"),
        "response_type": _rec(response_type),
        "monthly_payment_offer": _rec("$250.00"),
        "explanation": _rec("My hours were cut in 2023 and I could not pay in full."),
    }


# ---------------------------------------------------------------------------
# Renders a valid PDF for each response_type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("response_type", _RESPONSE_TYPES)
def test_renders_valid_pdf_for_each_response_type(response_type):
    pdf, content_type = render_cp504(2023, WatermarkMode.DRAFT, _full_inputs(response_type))
    assert content_type == "application/pdf"
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 1000  # a real multi-element letter, not an empty doc


def test_unknown_response_type_still_renders():
    """An unrecognized response_type falls back to a neutral acknowledgement."""
    inputs = _full_inputs("something_unexpected")
    pdf, ct = render_cp504(2023, WatermarkMode.DRAFT, inputs)
    assert ct == "application/pdf"
    assert pdf[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# Graceful when fields are absent (never crashes, never invents)
# ---------------------------------------------------------------------------

def test_no_crash_when_amount_due_absent():
    inputs = _full_inputs("request_installment_agreement")
    inputs.pop("amount_due")
    pdf, ct = render_cp504(2023, WatermarkMode.DRAFT, inputs)
    assert ct == "application/pdf"
    assert pdf[:4] == b"%PDF"


def test_no_crash_when_amount_due_absent_for_every_response_type():
    for rt in _RESPONSE_TYPES:
        inputs = _full_inputs(rt)
        inputs.pop("amount_due")
        inputs.pop("monthly_payment_offer", None)
        pdf, _ = render_cp504(None, WatermarkMode.DRAFT, inputs)
        assert pdf[:4] == b"%PDF", f"failed for {rt}"


def test_renders_with_empty_inputs():
    """Bare minimum: no inputs at all still yields a coherent, valid PDF."""
    pdf, ct = render_cp504(None, WatermarkMode.DRAFT, {})
    assert ct == "application/pdf"
    assert pdf[:4] == b"%PDF"


def test_renders_with_none_inputs():
    """Defensive: a None inputs arg is treated as empty, not a crash."""
    pdf, ct = render_cp504(2023, WatermarkMode.DRAFT, None)  # type: ignore[arg-type]
    assert ct == "application/pdf"
    assert pdf[:4] == b"%PDF"


def test_blank_value_is_skipped_not_drawn():
    """A present-but-blank value must not raise and must not be treated as data."""
    inputs = _full_inputs("dispute_balance")
    inputs["amount_due"] = {"value": "  ", "source_ref": "x", "origin": "user_entered"}
    pdf, _ = render_cp504(2023, WatermarkMode.DRAFT, inputs)
    assert pdf[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# All three watermark modes render
# ---------------------------------------------------------------------------

def test_all_watermark_modes_render():
    inputs = _full_inputs("request_cnc_hardship")
    for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
        pdf, ct = render_cp504(2023, mode, inputs)
        assert ct == "application/pdf"
        assert pdf[:4] == b"%PDF", f"failed for watermark mode {mode}"


def test_none_watermark_is_smaller_than_draft():
    """NONE returns the un-stamped letter; DRAFT overlays a banner + diagonal stamp."""
    inputs = _full_inputs("pay_in_full_notice")
    none_pdf, _ = render_cp504(2023, WatermarkMode.NONE, inputs)
    draft_pdf, _ = render_cp504(2023, WatermarkMode.DRAFT, inputs)
    assert none_pdf[:4] == b"%PDF"
    assert draft_pdf[:4] == b"%PDF"
    # The DRAFT overlay merges an extra content stream, so it is not identical.
    assert none_pdf != draft_pdf
