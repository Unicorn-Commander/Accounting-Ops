"""Smoke tests for app.categorization.receipt_vlm.ReceiptVLMExtractor.

All inputs SYNTHETIC. No real network — httpx is intercepted by MockTransport
(same strategy as test_llm_categorizer). Covers:

  1. can_extract — True for images, False for PDF/CSV.
  2. Happy path — model returns clean JSON → tier='high', fields parsed.
  3. Partial extraction (missing date) → tier='low', other fields kept.
  4. Non-receipt / all-null → tier='none', has_any_field False.
  5. Timeout → tier='none', error set, NEVER raises.
  6. Transport error → tier='none', error set.
  7. Malformed JSON → tier='none'.
  8. Markdown-fenced JSON is unwrapped.
  9. Amount normalization (currency symbols, commas, negatives, junk).
 10. Date normalization (ISO only; slashes/datetimes rejected).
 11. Non-image mime is declined without a model call.

Validator unit tests (all deterministic, no model call):
 12. Clean reconcile → arithmetic='ok', confidence unchanged.
 13. Single missing component (tax) → auto-repair, arithmetic='repaired'.
 14. All present but mismatch → arithmetic='mismatch', confidence downgraded.
 15. Missing total but all other components present → total repaired.
 16. extract_structured happy path — mock model, check structured fields.
 17. Integer-cents helpers (_to_cents / _from_cents).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.categorization.receipt_vlm import (
    LineItem,
    ReceiptVLMExtractor,
    _from_cents,
    _normalize_amount,
    _normalize_date,
    _parse_receipt_json,
    _tier_for,
    _to_cents,
    _validate_arithmetic,
)
from app.config import Settings

_SETTINGS = Settings(
    llm_endpoint_url="http://fake-vlm.internal/v1",
    llm_model="fake-vlm",
    llm_api_key="",
    llm_timeout_s=5,
    llm_max_tokens=256,
)

# 1x1 PNG header bytes — enough to be "an image" for these tests.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _completion(payload: dict) -> dict:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps(payload)}}
        ]
    }


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _resp_for(body: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


# --------------------------------------------------------------------------- #
def test_can_extract():
    ex = ReceiptVLMExtractor(_SETTINGS)
    assert ex.can_extract("image/png") is True
    assert ex.can_extract("image/jpeg") is True
    assert ex.can_extract("application/pdf") is False
    assert ex.can_extract("text/csv") is False
    assert ex.can_extract("") is False


@pytest.mark.asyncio
async def test_extract_happy_path_high_tier():
    body = _completion(
        {"vendor": "Synth Coffee", "amount": "12.50", "date": "2026-01-15",
         "currency": "USD", "confidence": 0.9}
    )
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(body))):
        r = await ex.extract(image_bytes=_PNG, mime_type="image/png")
    assert r.tier == "high"
    assert r.vendor == "Synth Coffee"
    assert r.amount == "12.50"
    assert r.date == "2026-01-15"
    assert r.currency == "USD"
    assert r.error is None
    assert r.has_any_field is True


@pytest.mark.asyncio
async def test_extract_partial_is_low_tier():
    body = _completion(
        {"vendor": "Synth Store", "amount": "9.99", "date": None, "confidence": 0.8}
    )
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(body))):
        r = await ex.extract(image_bytes=_PNG, mime_type="image/png")
    assert r.tier == "low"  # date missing → not high
    assert r.vendor == "Synth Store"
    assert r.amount == "9.99"
    assert r.date is None


@pytest.mark.asyncio
async def test_extract_all_null_is_none_tier():
    body = _completion(
        {"vendor": None, "amount": None, "date": None, "confidence": 0.1}
    )
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(body))):
        r = await ex.extract(image_bytes=_PNG, mime_type="image/png")
    assert r.tier == "none"
    assert r.has_any_field is False


@pytest.mark.asyncio
async def test_extract_timeout_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(handler)):
        r = await ex.extract(image_bytes=_PNG, mime_type="image/png")
    assert r.tier == "none"
    assert r.error == "vlm timeout"


@pytest.mark.asyncio
async def test_extract_transport_error_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(handler)):
        r = await ex.extract(image_bytes=_PNG, mime_type="image/png")
    assert r.tier == "none"
    assert r.error and "transport" in r.error


@pytest.mark.asyncio
async def test_extract_malformed_json_is_none_tier():
    body = {"choices": [{"message": {"content": "not json at all"}}]}
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(body))):
        r = await ex.extract(image_bytes=_PNG, mime_type="image/png")
    assert r.tier == "none"
    assert r.error == "unparseable json"


@pytest.mark.asyncio
async def test_extract_declines_non_image_without_call():
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient") as mock_ac:
        r = await ex.extract(image_bytes=b"%PDF-1.4", mime_type="application/pdf")
    assert r.tier == "none"
    mock_ac.assert_not_called()


def test_parse_handles_markdown_fence():
    raw = '```json\n{"vendor":"X","amount":"5.00","date":"2026-02-02","confidence":0.7}\n```'
    parsed = _parse_receipt_json(raw)
    assert parsed is not None
    vendor, amount, date, currency, conf = parsed
    assert vendor == "X"
    assert amount == "5.00"
    assert date == "2026-02-02"


def test_parse_isolates_json_in_prose():
    raw = 'Here is the data: {"vendor":"Y","amount":"3","date":null,"confidence":0.6} hope it helps'
    parsed = _parse_receipt_json(raw)
    assert parsed is not None
    assert parsed[0] == "Y"
    assert parsed[1] == "3.00"


def test_normalize_amount():
    assert _normalize_amount("$1,234.56") == "1234.56"
    assert _normalize_amount("42.99") == "42.99"
    assert _normalize_amount(12.5) == "12.50"
    assert _normalize_amount("€10") == "10.00"
    assert _normalize_amount("-5.00") is None  # a total is never negative
    assert _normalize_amount("not a number") is None
    assert _normalize_amount(None) is None
    assert _normalize_amount(True) is None  # bool guard


def test_normalize_date():
    assert _normalize_date("2026-03-04") == "2026-03-04"
    assert _normalize_date("03/04/2026") is None  # not ISO
    assert _normalize_date("2026-03-04T10:00:00") is None  # datetime, not date
    assert _normalize_date("") is None
    assert _normalize_date(None) is None


def test_tier_for():
    assert _tier_for(vendor="V", amount="1.00", date="2026-01-01", confidence=0.9) == "high"
    assert _tier_for(vendor="V", amount="1.00", date="2026-01-01", confidence=0.3) == "low"
    assert _tier_for(vendor="V", amount=None, date=None, confidence=0.9) == "low"
    assert _tier_for(vendor=None, amount=None, date=None, confidence=0.9) == "none"


# --------------------------------------------------------------------------- #
# Integer-cents helpers
# --------------------------------------------------------------------------- #


def test_to_cents_basic():
    assert _to_cents("12.50") == 1250
    assert _to_cents("0.99") == 99
    assert _to_cents("100.00") == 10000
    assert _to_cents("0.00") == 0
    assert _to_cents(None) is None
    assert _to_cents("-1.00") is None  # negatives rejected
    assert _to_cents("not-a-number") is None


def test_to_cents_sub_cent_rounds():
    # A VLM might return an extra decimal; round to nearest cent.
    assert _to_cents("12.505") == 1251  # rounds up
    assert _to_cents("12.504") == 1250  # rounds down


def test_from_cents():
    assert _from_cents(1250) == "12.50"
    assert _from_cents(99) == "0.99"
    assert _from_cents(10000) == "100.00"
    assert _from_cents(0) == "0.00"
    assert _from_cents(5) == "0.05"


# --------------------------------------------------------------------------- #
# Validator: clean reconcile
# --------------------------------------------------------------------------- #


def test_validate_arithmetic_ok():
    """line_items + tax + tip − discount == total → arithmetic='ok'."""
    items = [LineItem("Coffee", "3.00"), LineItem("Muffin", "2.50")]
    result = _validate_arithmetic(
        line_items=items,
        subtotal="5.50",
        tax="0.44",
        tip=None,
        discount=None,
        total="5.94",
        confidence=0.85,
    )
    assert result["arithmetic"] == "ok"
    assert result["confidence"] == 0.85  # unchanged
    assert result["total"] == "5.94"
    assert result["provenance"]["total"] == "extracted"
    assert result["provenance"]["tax"] == "extracted"


def test_validate_arithmetic_ok_with_tip_and_discount():
    """subtotal=10.00, tax=0.80, tip=2.00, discount=1.00, total=11.80."""
    result = _validate_arithmetic(
        line_items=[],
        subtotal="10.00",
        tax="0.80",
        tip="2.00",
        discount="1.00",
        total="11.80",
        confidence=0.9,
    )
    assert result["arithmetic"] == "ok"
    assert result["total"] == "11.80"


# --------------------------------------------------------------------------- #
# Validator: single-missing auto-repair
# --------------------------------------------------------------------------- #


def test_validate_arithmetic_repair_tax():
    """Tax is null; total − subtotal = tax. Validator should solve it."""
    # subtotal=10.00, tax=?, tip=None, discount=None, total=10.80
    # → tax = 10.80 − 10.00 = 0.80
    items = [LineItem("Widget", "10.00")]
    result = _validate_arithmetic(
        line_items=items,
        subtotal=None,
        tax=None,
        tip=None,
        discount=None,
        total="10.80",
        confidence=0.75,
    )
    assert result["arithmetic"] == "repaired"
    assert result["tax"] == "0.80"
    assert result["provenance"]["tax"] == "repaired"
    assert result["total"] == "10.80"
    # Confidence should be nudged UP (or at least not downgraded).
    assert result["confidence"] >= 0.75


def test_validate_arithmetic_repair_missing_total():
    """Total absent; compute from base + tax."""
    result = _validate_arithmetic(
        line_items=[],
        subtotal="20.00",
        tax="1.60",
        tip=None,
        discount=None,
        total=None,
        confidence=0.8,
    )
    assert result["arithmetic"] == "repaired"
    assert result["total"] == "21.60"
    assert result["provenance"]["total"] == "repaired"


def test_validate_arithmetic_repair_discount():
    """Discount is null; solver finds: discount = base + tax − total."""
    # base=10.00, tax=0.80, discount=?, total=9.80
    # discount = 10.00 + 0.80 − 9.80 = 1.00
    result = _validate_arithmetic(
        line_items=[],
        subtotal="10.00",
        tax="0.80",
        tip=None,
        discount=None,
        total="9.80",
        confidence=0.7,
    )
    assert result["arithmetic"] == "repaired"
    assert result["discount"] == "1.00"
    assert result["provenance"]["discount"] == "repaired"


# --------------------------------------------------------------------------- #
# Validator: all-present mismatch → flag, downgrade confidence
# --------------------------------------------------------------------------- #


def test_validate_arithmetic_mismatch():
    """VLM hallucinated a total that doesn't match the components."""
    # base=10.00, tax=0.80, total=12.00 (VLM computed it wrong)
    result = _validate_arithmetic(
        line_items=[],
        subtotal="10.00",
        tax="0.80",
        tip=None,
        discount=None,
        total="12.00",  # wrong: should be 10.80
        confidence=0.85,
    )
    assert result["arithmetic"] == "mismatch"
    # Confidence must be downgraded.
    assert result["confidence"] < 0.85
    # All extracted money fields should be marked disputed.
    assert result["provenance"]["subtotal"] == "disputed"
    assert result["provenance"]["total"] == "disputed"
    # Original values preserved (not silently overwritten).
    assert result["total"] == "12.00"
    assert result["subtotal"] == "10.00"


def test_validate_arithmetic_mismatch_preserves_original_values():
    """On mismatch, the raw extracted values must be returned unchanged."""
    result = _validate_arithmetic(
        line_items=[],
        subtotal="5.00",
        tax="0.40",
        tip="1.00",
        discount=None,
        total="9.99",  # clearly wrong
        confidence=0.9,
    )
    assert result["arithmetic"] == "mismatch"
    assert result["subtotal"] == "5.00"
    assert result["tax"] == "0.40"
    assert result["tip"] == "1.00"
    assert result["total"] == "9.99"


# --------------------------------------------------------------------------- #
# Validator: partial (not enough fields)
# --------------------------------------------------------------------------- #


def test_validate_arithmetic_partial_no_fields():
    """No components at all → partial, nothing changed."""
    result = _validate_arithmetic(
        line_items=[],
        subtotal=None,
        tax=None,
        tip=None,
        discount=None,
        total=None,
        confidence=0.3,
    )
    assert result["arithmetic"] == "partial"
    assert result["total"] is None


# --------------------------------------------------------------------------- #
# extract_structured integration (mocked VLM)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_extract_structured_happy_path():
    """extract_structured: clean receipt, arithmetic ok, returns structured result."""
    structured_payload = {
        "vendor": "Corner Deli",
        "date": "2026-03-10",
        "currency": "USD",
        "line_items": [
            {"description": "Sandwich", "amount": 8.50},
            {"description": "Soda", "amount": 2.00},
        ],
        "subtotal": "10.50",
        "tax": "0.84",
        "tip": None,
        "discount": None,
        "total": "11.34",
        "confidence": 0.92,
    }
    body = _completion(structured_payload)
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(body))):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    assert r.arithmetic == "ok"
    assert r.vendor == "Corner Deli"
    assert r.date == "2026-03-10"
    assert r.currency == "USD"
    assert r.total == "11.34"
    assert r.amount == "11.34"  # canonical amount = validated total
    assert len(r.line_items) == 2
    assert r.line_items[0].description == "Sandwich"
    assert r.line_items[0].amount == "8.50"
    assert r.error is None
    assert r.tier in ("high", "low")


@pytest.mark.asyncio
async def test_extract_structured_repaired_tax():
    """extract_structured: tax absent, validator repairs it."""
    structured_payload = {
        "vendor": "Repair Shop",
        "date": "2026-04-01",
        "currency": "USD",
        "line_items": [{"description": "Part", "amount": 50.00}],
        "subtotal": None,
        "tax": None,
        "tip": None,
        "discount": None,
        "total": "53.00",
        "confidence": 0.80,
    }
    body = _completion(structured_payload)
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(body))):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    assert r.arithmetic == "repaired"
    assert r.tax == "3.00"
    assert r.provenance.get("tax") == "repaired"
    assert r.total == "53.00"
    assert r.amount == "53.00"


@pytest.mark.asyncio
async def test_extract_structured_mismatch_downgrades_confidence():
    """extract_structured: VLM total doesn't reconcile → mismatch + lower conf."""
    structured_payload = {
        "vendor": "Bad Math Cafe",
        "date": "2026-05-01",
        "currency": "USD",
        "line_items": [{"description": "Coffee", "amount": 4.00}],
        "subtotal": "4.00",
        "tax": "0.32",
        "tip": None,
        "discount": None,
        "total": "9.99",  # wrong: should be 4.32
        "confidence": 0.88,
    }
    body = _completion(structured_payload)
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(body))):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    assert r.arithmetic == "mismatch"
    assert r.confidence < 0.88
    assert r.total == "9.99"  # raw value preserved, not silently fixed
    assert r.tier in ("low", "none")  # downgraded confidence should drop tier


@pytest.mark.asyncio
async def test_extract_structured_declines_non_image():
    """extract_structured declines non-image mime without a model call."""
    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient") as mock_ac:
        r = await ex.extract_structured(image_bytes=b"%PDF-1.4", mime_type="application/pdf")
    assert r.tier == "none"
    assert r.arithmetic == "skipped"
    mock_ac.assert_not_called()


@pytest.mark.asyncio
async def test_extract_structured_timeout_never_raises():
    """extract_structured: timeout → tier='none', arithmetic='skipped', no raise."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    ex = ReceiptVLMExtractor(_SETTINGS)
    with patch("httpx.AsyncClient", return_value=_mock(handler)):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")
    assert r.tier == "none"
    assert r.arithmetic == "skipped"
    assert r.error == "vlm timeout"
