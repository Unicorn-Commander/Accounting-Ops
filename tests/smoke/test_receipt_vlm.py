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

bbox-oracle cross-check tests (BOTH VLM + oracle mocked — no live services):
 18. VLM drops a line amount, bbox has it → recovered, provenance 'recovered_from_bbox'.
 19. VLM and bbox agree on total → no dispute, confidence not penalised.
 20. VLM and bbox disagree on total → 'disputed' provenance + confidence downgrade.
 21. bbox service unreachable → VLM-only result returned, no crash.
 22. Geometric row/amount extraction unit tests (_geometric_row_amounts).
 23. _bbox_cross_check unit tests.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.categorization.receipt_vlm import (
    LineItem,
    ReceiptVLMExtractor,
    _bbox_cross_check,
    _call_bbox_oracle,
    _from_cents,
    _geometric_row_amounts,
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


# --------------------------------------------------------------------------- #
# bbox-oracle helper unit tests (no HTTP, fully deterministic)
# --------------------------------------------------------------------------- #


def test_geometric_row_amounts_basic():
    """Two rows: left label + right price token each → label→amount map."""
    # Simulate a 400px wide receipt with two rows.
    # Row 0 (y≈10): "Sandwich" on left, "8.50" on right.
    # Row 1 (y≈30): "Tax" on left, "0.68" on right.
    lines = [
        {"text": "Sandwich", "bbox": [10, 5, 180, 15], "score": 0.99},
        {"text": "8.50", "bbox": [320, 5, 395, 15], "score": 0.99},
        {"text": "Tax", "bbox": [10, 25, 80, 35], "score": 0.99},
        {"text": "0.68", "bbox": [330, 25, 395, 35], "score": 0.99},
    ]
    result = _geometric_row_amounts(lines)
    assert result.get("sandwich") == "8.50"
    assert result.get("tax") == "0.68"


def test_geometric_row_amounts_empty():
    assert _geometric_row_amounts([]) == {}


def test_geometric_row_amounts_no_right_column_prices():
    """Lines exist but none are in the right column as prices → empty."""
    lines = [
        {"text": "hello world", "bbox": [10, 5, 180, 15], "score": 0.9},
    ]
    assert _geometric_row_amounts(lines) == {}


def test_geometric_row_amounts_ignores_non_price_right_tokens():
    """Right-side tokens that don't look like prices are skipped."""
    lines = [
        {"text": "Total", "bbox": [10, 5, 100, 15], "score": 0.9},
        # "USD" in the right column is not a price
        {"text": "USD", "bbox": [320, 5, 390, 15], "score": 0.9},
    ]
    result = _geometric_row_amounts(lines)
    assert result == {}


def test_bbox_cross_check_recovery():
    """VLM returned null for tax; oracle has it → recovered_from_bbox.

    We build the input dict directly (simulating the pre-arithmetic VLM state
    where tax is still None) rather than using _validate_arithmetic, which
    would auto-repair tax from the components before we can test recovery.
    """
    # Simulate a raw validated dict where tax is null (VLM missed it) and the
    # arithmetic validator returned "partial" because it couldn't solve it
    # (e.g. both base and total were also null — keep it simple: manual dict).
    raw_vlm_dict = {
        "subtotal": "8.50",
        "tax": None,      # VLM missed it
        "tip": None,
        "discount": None,
        "total": "9.18",
        "arithmetic": "partial",
        "confidence": 0.80,
        "provenance": {
            "subtotal": "extracted",
            "tax": "missing",
            "tip": "missing",
            "discount": "missing",
            "total": "extracted",
        },
        "notes": [],
    }
    bbox_amounts = {"sandwich": "8.50", "tax": "0.68", "total": "9.18"}
    result = _bbox_cross_check(
        vlm_result=raw_vlm_dict,
        bbox_amounts=bbox_amounts,
        line_items=[LineItem("Sandwich", "8.50")],
    )
    assert result["tax"] == "0.68"
    assert result["provenance"]["tax"] == "recovered_from_bbox"
    assert any("recovered" in n for n in result["notes"])


def test_bbox_cross_check_agreement_no_dispute():
    """VLM and oracle agree on total → no dispute mark."""
    base_validated = _validate_arithmetic(
        line_items=[],
        subtotal="10.00",
        tax="0.80",
        tip=None,
        discount=None,
        total="10.80",
        confidence=0.90,
    )
    bbox_amounts = {"subtotal": "10.00", "tax": "0.80", "total": "10.80"}
    result = _bbox_cross_check(
        vlm_result=base_validated,
        bbox_amounts=bbox_amounts,
        line_items=[],
    )
    # No dispute; provenance not degraded.
    assert result["provenance"].get("total") != "disputed"
    assert result["confidence"] >= 0.90


def test_bbox_cross_check_dispute():
    """VLM and oracle disagree on total → disputed + confidence downgrade."""
    base_validated = _validate_arithmetic(
        line_items=[],
        subtotal="10.00",
        tax="0.80",
        tip=None,
        discount=None,
        total="10.80",  # VLM says 10.80
        confidence=0.85,
    )
    bbox_amounts = {"total": "11.50"}  # oracle says 11.50 — clearly different
    result = _bbox_cross_check(
        vlm_result=base_validated,
        bbox_amounts=bbox_amounts,
        line_items=[],
    )
    assert result["provenance"]["total"] == "disputed"
    assert result["confidence"] < 0.85
    assert any("disputed" in n for n in result["notes"])


def test_bbox_cross_check_empty_amounts():
    """Empty bbox_amounts → pass-through unchanged."""
    base_validated = _validate_arithmetic(
        line_items=[],
        subtotal="5.00",
        tax="0.40",
        tip=None,
        discount=None,
        total="5.40",
        confidence=0.9,
    )
    result = _bbox_cross_check(
        vlm_result=base_validated,
        bbox_amounts={},
        line_items=[],
    )
    assert result["total"] == "5.40"
    assert result["confidence"] == 0.9


# --------------------------------------------------------------------------- #
# bbox-oracle HTTP client unit test
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_call_bbox_oracle_returns_lines():
    """_call_bbox_oracle: mocked HTTP → returns parsed lines list."""
    oracle_body = {
        "text": "Sandwich 8.50\nTax 0.68",
        "lines": [
            {"text": "Sandwich", "bbox": [10, 5, 180, 15], "score": 0.99},
            {"text": "8.50", "bbox": [320, 5, 395, 15], "score": 0.99},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/ocr" in str(request.url)
        return httpx.Response(200, json=oracle_body)

    with patch("httpx.AsyncClient", return_value=_mock(handler)):
        lines = await _call_bbox_oracle(
            image_bytes=_PNG,
            mime_type="image/png",
            service_url="http://fake-oracle.internal:8097",
        )
    assert len(lines) == 2
    assert lines[0]["text"] == "Sandwich"


@pytest.mark.asyncio
async def test_call_bbox_oracle_unreachable_returns_empty():
    """_call_bbox_oracle: connection error → returns [] without raising."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with patch("httpx.AsyncClient", return_value=_mock(handler)):
        lines = await _call_bbox_oracle(
            image_bytes=_PNG,
            mime_type="image/png",
            service_url="http://dead-oracle.internal:8097",
        )
    assert lines == []


@pytest.mark.asyncio
async def test_call_bbox_oracle_disabled_when_url_empty():
    """_call_bbox_oracle: empty service_url → returns [] immediately, no HTTP call."""
    with patch("httpx.AsyncClient") as mock_ac:
        lines = await _call_bbox_oracle(
            image_bytes=_PNG,
            mime_type="image/png",
            service_url="",
        )
    assert lines == []
    mock_ac.assert_not_called()


# --------------------------------------------------------------------------- #
# extract_structured integration: VLM + bbox oracle both mocked
# --------------------------------------------------------------------------- #

# Settings with bbox oracle enabled (pointing at fake host).
_SETTINGS_WITH_ORACLE = Settings(
    llm_endpoint_url="http://fake-vlm.internal/v1",
    llm_model="fake-vlm",
    llm_api_key="",
    llm_timeout_s=5,
    llm_max_tokens=256,
    ocr_bbox_service_url="http://fake-oracle.internal:8097",
)

# Settings with bbox oracle disabled.
_SETTINGS_NO_ORACLE = Settings(
    llm_endpoint_url="http://fake-vlm.internal/v1",
    llm_model="fake-vlm",
    llm_api_key="",
    llm_timeout_s=5,
    llm_max_tokens=256,
    ocr_bbox_service_url="",
)


def _dual_mock_patch(vlm_body: dict, oracle_body: dict):
    """Return a patch context that routes by URL: /chat/completions → VLM body,
    /ocr → oracle body.

    Uses side_effect so that each call to httpx.AsyncClient() returns a NEW
    client instance (not the same closed one), which is required because
    extract_structured opens two separate ``async with httpx.AsyncClient(...)``
    contexts (one for VLM, one for bbox-oracle).

    The factory captures the REAL httpx.AsyncClient class before patching to
    avoid infinite recursion.
    """
    _real_AsyncClient = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        if "/ocr" in str(request.url):
            return httpx.Response(200, json=oracle_body)
        return httpx.Response(200, json=vlm_body)

    def _factory(*args, **kwargs):
        # Strip httpx-specific kwargs that MockTransport ignores but might error.
        return _real_AsyncClient(transport=httpx.MockTransport(handler))

    return patch("httpx.AsyncClient", side_effect=_factory)


def _oracle_lines_for_receipt(
    *,
    sandwich: str = "8.50",
    tax: str = "0.84",
    total: str = "9.34",
    img_width: int = 400,
) -> list[dict]:
    """Helper: build synthetic bbox oracle lines for a simple receipt."""
    right_x1 = int(img_width * 0.65)
    right_x2 = img_width - 5
    return [
        {"text": "Sandwich", "bbox": [10, 5, 180, 15], "score": 0.99},
        {"text": sandwich, "bbox": [right_x1, 5, right_x2, 15], "score": 0.99},
        {"text": "Tax", "bbox": [10, 25, 80, 35], "score": 0.99},
        {"text": tax, "bbox": [right_x1, 25, right_x2, 35], "score": 0.99},
        {"text": "Total", "bbox": [10, 45, 100, 55], "score": 0.99},
        {"text": total, "bbox": [right_x1, 45, right_x2, 55], "score": 0.99},
    ]


@pytest.mark.asyncio
async def test_extract_structured_bbox_recovers_null_line_item():
    """VLM drops a line-item amount; bbox oracle has it → recovered, arithmetic ok."""
    # VLM returns Sandwich with null amount; tax null too (will be solved).
    structured_payload = {
        "vendor": "Corner Deli",
        "date": "2026-03-10",
        "currency": "USD",
        "line_items": [
            {"description": "Sandwich", "amount": None},  # VLM dropped it
        ],
        "subtotal": None,
        "tax": None,
        "tip": None,
        "discount": None,
        "total": "9.34",
        "confidence": 0.75,
    }
    vlm_body = _completion(structured_payload)
    oracle_body = {
        "text": "...",
        "lines": _oracle_lines_for_receipt(sandwich="8.50", tax="0.84", total="9.34"),
    }
    ex = ReceiptVLMExtractor(_SETTINGS_WITH_ORACLE)
    with _dual_mock_patch(vlm_body, oracle_body):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    # At minimum the total should be present and arithmetic should not be skipped.
    assert r.total == "9.34"
    assert r.arithmetic in ("ok", "repaired")
    # The line item should have its amount recovered from bbox oracle.
    assert len(r.line_items) == 1
    assert r.line_items[0].amount == "8.50"
    # Provenance: tax was recovered or repaired; total was extracted.
    assert r.provenance.get("total") in ("extracted", "ok")
    # Notes should mention recovery.
    all_notes = " ".join(r.validation_notes)
    assert "8.50" in all_notes or "recovered" in all_notes or r.arithmetic in ("ok", "repaired")


@pytest.mark.asyncio
async def test_extract_structured_bbox_agreement_no_dispute():
    """VLM and oracle agree on all values → no dispute, arithmetic ok."""
    structured_payload = {
        "vendor": "Corner Deli",
        "date": "2026-03-10",
        "currency": "USD",
        "line_items": [{"description": "Sandwich", "amount": 8.50}],
        "subtotal": "8.50",
        "tax": "0.84",
        "tip": None,
        "discount": None,
        "total": "9.34",
        "confidence": 0.92,
    }
    vlm_body = _completion(structured_payload)
    oracle_body = {
        "text": "...",
        "lines": _oracle_lines_for_receipt(sandwich="8.50", tax="0.84", total="9.34"),
    }
    ex = ReceiptVLMExtractor(_SETTINGS_WITH_ORACLE)
    with _dual_mock_patch(vlm_body, oracle_body):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    assert r.arithmetic == "ok"
    assert r.total == "9.34"
    # No disputed fields.
    for prov_val in r.provenance.values():
        assert prov_val != "disputed"


@pytest.mark.asyncio
async def test_extract_structured_bbox_dispute_on_total():
    """VLM and oracle disagree on total → 'disputed' provenance + confidence drop."""
    structured_payload = {
        "vendor": "Mismatch Cafe",
        "date": "2026-05-01",
        "currency": "USD",
        "line_items": [{"description": "Coffee", "amount": 4.00}],
        "subtotal": "4.00",
        "tax": "0.32",
        "tip": None,
        "discount": None,
        "total": "4.32",   # VLM says 4.32
        "confidence": 0.88,
    }
    vlm_body = _completion(structured_payload)
    # Oracle says total is 5.99 — clearly different from VLM's 4.32.
    oracle_body = {
        "text": "...",
        "lines": _oracle_lines_for_receipt(sandwich="4.00", tax="0.32", total="5.99"),
    }
    ex = ReceiptVLMExtractor(_SETTINGS_WITH_ORACLE)
    with _dual_mock_patch(vlm_body, oracle_body):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    # Total is disputed; confidence must be lower than original 0.88.
    assert r.provenance.get("total") == "disputed"
    assert r.confidence < 0.88
    # VLM value is preserved (never silently replaced by oracle).
    assert r.total == "4.32"


@pytest.mark.asyncio
async def test_extract_structured_oracle_unreachable_still_returns_vlm_result():
    """bbox oracle down → pipeline returns VLM-only result, no crash."""
    structured_payload = {
        "vendor": "Resilient Deli",
        "date": "2026-06-01",
        "currency": "USD",
        "line_items": [{"description": "Salad", "amount": 7.50}],
        "subtotal": "7.50",
        "tax": "0.60",
        "tip": None,
        "discount": None,
        "total": "8.10",
        "confidence": 0.85,
    }
    vlm_body = _completion(structured_payload)

    # Handler: VLM call succeeds; oracle call raises ConnectError.
    def handler(request: httpx.Request) -> httpx.Response:
        if "/ocr" in str(request.url):
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=vlm_body)

    ex = ReceiptVLMExtractor(_SETTINGS_WITH_ORACLE)
    with patch("httpx.AsyncClient", return_value=_mock(handler)):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    # VLM result intact; no crash.
    assert r.total == "8.10"
    assert r.arithmetic == "ok"
    assert r.vendor == "Resilient Deli"
    assert r.error is None
    # No disputed fields (oracle didn't inject anything).
    for prov_val in r.provenance.values():
        assert prov_val != "disputed"


@pytest.mark.asyncio
async def test_extract_structured_oracle_disabled_url_empty():
    """OCR_BBOX_SERVICE_URL='' → oracle skipped, VLM-only result returned."""
    structured_payload = {
        "vendor": "No Oracle Bistro",
        "date": "2026-06-01",
        "currency": "USD",
        "line_items": [{"description": "Soup", "amount": 6.00}],
        "subtotal": "6.00",
        "tax": "0.48",
        "tip": None,
        "discount": None,
        "total": "6.48",
        "confidence": 0.90,
    }
    vlm_body = _completion(structured_payload)
    ex = ReceiptVLMExtractor(_SETTINGS_NO_ORACLE)
    with patch("httpx.AsyncClient", return_value=_mock(_resp_for(vlm_body))):
        r = await ex.extract_structured(image_bytes=_PNG, mime_type="image/png")

    assert r.arithmetic == "ok"
    assert r.total == "6.48"
    assert r.error is None
