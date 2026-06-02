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
"""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from app.categorization.receipt_vlm import (
    ReceiptVLMExtractor,
    _normalize_amount,
    _normalize_date,
    _parse_receipt_json,
    _tier_for,
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
