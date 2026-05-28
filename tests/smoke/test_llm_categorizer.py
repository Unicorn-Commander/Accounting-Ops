"""Smoke tests for app.categorization.llm.LLMCategorizer.

All inputs are SYNTHETIC — no real bank descriptions, no real merchant
names, no real transaction amounts that could identify any customer's books.

Test strategy
-------------
No real network calls. Every HTTP interaction is intercepted by an
``httpx.MockTransport`` (an in-process fake that returns pre-canned JSON).
CI runs these without any LLM endpoint reachable and they must pass.

Coverage
--------
1.  Happy path — model returns a valid category from the allowed list.
2.  Hallucinated category — model returns a category NOT in allowed list;
    ``suggest()`` must return None.
3.  Timeout — httpx raises TimeoutException; ``suggest()`` must return None.
4.  Malformed JSON — completion is not valid JSON; ``suggest()`` must return
    None.
5.  Empty allowed_categories — ``suggest()`` must return None immediately
    (no HTTP call made).
6.  Markdown-fenced JSON — some models wrap their output in ```json...```;
    the parser must unwrap it.
7.  Engine integration — ``categorize_transaction_async`` with an
    ``LLMCategorizer`` falls back correctly for an unrecognized description
    and returns status="suggest".
8.  Engine integration — ``categorize_transaction_async`` does NOT call the
    LLM when Tier 1 already matched (vendor_rule).
9.  health() — returns reachable=True on 200, reachable=False on transport
    error.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.categorization.llm import LLMCategorizer, _parse_and_validate
from app.categorization.engine import categorize_transaction_async
from app.config import Settings


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_ALLOWED = [
    "Revenue · Marketplace",
    "SaaS · AI",
    "SaaS · Productivity",
    "Infrastructure · VPS",
    "Office · Supplies",
    "Banking · Fees",
    "Uncategorized",
]

# Minimal settings that point at a fake URL (never contacted in these tests).
_TEST_SETTINGS = Settings(
    llm_endpoint_url="http://fake-llm.internal/v1",
    llm_model="fake-model",
    llm_api_key="",      # no key — local/no-auth mode
    llm_timeout_s=5,
    llm_max_tokens=200,
)


def _make_completion_response(category: str, confidence: float, reasoning: str) -> dict[str, Any]:
    """Craft an OpenAI-shaped completion response."""
    content = json.dumps({
        "category": category,
        "confidence": confidence,
        "reasoning": reasoning,
    })
    return {
        "id": "cmpl-synthetic",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _mock_transport_for(response_body: dict[str, Any], status_code: int = 200) -> httpx.MockTransport:
    """Return an httpx.MockTransport that always responds with response_body."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            json=response_body,
        )
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggest_happy_path():
    """Model returns a valid, in-list category → suggestion returned."""
    body = _make_completion_response(
        category="SaaS · AI",
        confidence=0.92,
        reasoning="Synthetic AI service descriptor.",
    )
    transport = _mock_transport_for(body)

    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await categorizer.suggest(
            payee=None,
            description="CHECKCARD SYNTH-AI-SERVICE SAN FRANCISCO CA",
            memo=None,
            amount=Decimal("-29.99"),
            allowed_categories=_ALLOWED,
        )

    assert result is not None
    assert result.category == "SaaS · AI"
    assert 0.0 <= result.confidence <= 1.0
    assert result.reasoning == "Synthetic AI service descriptor."
    assert result.model == "fake-model"
    assert result.endpoint == "http://fake-llm.internal/v1"
    assert result.duration_ms >= 0


# ---------------------------------------------------------------------------
# 2. Hallucinated category
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggest_hallucinated_category_returns_none():
    """Model returns a category NOT in the allowed list → None."""
    body = _make_completion_response(
        category="Totally Made-Up Category",
        confidence=0.99,
        reasoning="Synthetic reasoning.",
    )
    transport = _mock_transport_for(body)

    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await categorizer.suggest(
            payee=None,
            description="PURCHASE SYNTH UNKNOWN VENDOR",
            memo=None,
            amount=Decimal("-15.00"),
            allowed_categories=_ALLOWED,
        )

    assert result is None


# ---------------------------------------------------------------------------
# 3. Timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggest_timeout_returns_none():
    """httpx.TimeoutException → None (not a crash)."""
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    transport = httpx.MockTransport(timeout_handler)
    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await categorizer.suggest(
            payee=None,
            description="CHECKCARD SYNTH-SERVICE ANYTOWN USA",
            memo=None,
            amount=Decimal("-9.99"),
            allowed_categories=_ALLOWED,
        )

    assert result is None


# ---------------------------------------------------------------------------
# 4. Malformed JSON
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggest_malformed_json_returns_none():
    """Completion is not valid JSON → None."""
    bad_body = {
        "choices": [
            {"message": {"role": "assistant", "content": "This is not JSON at all!"}}
        ]
    }
    transport = _mock_transport_for(bad_body)
    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await categorizer.suggest(
            payee=None,
            description="PURCHASE SYNTH-VENDOR CHARLESTON SC",
            memo=None,
            amount=Decimal("-55.00"),
            allowed_categories=_ALLOWED,
        )

    assert result is None


# ---------------------------------------------------------------------------
# 5. Empty allowed_categories
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_suggest_empty_allowed_categories_returns_none_immediately():
    """No HTTP call is made when allowed_categories is empty."""
    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    # Patch AsyncClient to assert it is NOT instantiated.
    with patch("httpx.AsyncClient") as mock_client:
        result = await categorizer.suggest(
            payee=None,
            description="CHECKCARD SYNTH-VENDOR ANYWHERE",
            memo=None,
            amount=Decimal("-1.00"),
            allowed_categories=[],
        )

    assert result is None
    mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Markdown-fenced JSON
# ---------------------------------------------------------------------------

def test_parse_and_validate_handles_markdown_fence():
    """_parse_and_validate strips ```json ... ``` fences before parsing."""
    fenced = "```json\n{\"category\":\"Banking · Fees\",\"confidence\":0.8,\"reasoning\":\"Synthetic fee.\"}\n```"
    allowed = ["Banking · Fees", "Uncategorized"]
    result = _parse_and_validate(fenced, allowed)
    assert result is not None
    cat, conf, reason = result
    assert cat == "Banking · Fees"
    assert 0.0 <= conf <= 1.0


def test_parse_and_validate_rejects_unknown_category():
    """Category not in allowed list → None."""
    raw = json.dumps({"category": "Not In List", "confidence": 0.9, "reasoning": "x"})
    result = _parse_and_validate(raw, ["Banking · Fees", "Uncategorized"])
    assert result is None


def test_parse_and_validate_case_insensitive_match():
    """Category that differs only in case is accepted and normalized."""
    raw = json.dumps({"category": "banking · fees", "confidence": 0.7, "reasoning": "synth"})
    result = _parse_and_validate(raw, ["Banking · Fees", "Uncategorized"])
    assert result is not None
    cat, _, _ = result
    assert cat == "Banking · Fees"   # normalized to canonical form


# ---------------------------------------------------------------------------
# 7. Engine integration — LLM fallback fires for unknown description
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_engine_async_falls_back_to_llm_for_unknown_description():
    """categorize_transaction_async uses LLM when Tier 1 returns Uncategorized."""
    body = _make_completion_response(
        category="Office · Supplies",
        confidence=0.75,
        reasoning="Synthetic unknown vendor looks like office supplies.",
    )
    transport = _mock_transport_for(body)
    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)

    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await categorize_transaction_async(
            payee=None,
            description="PURCHASE SYNTH-UNKNOWN-VENDOR-XYZ CHARLESTON SC",
            memo=None,
            amount=-12.00,
            overrides=None,
            llm_fallback=categorizer,
            allowed_categories=_ALLOWED,
        )

    assert result.category == "Office · Supplies"
    assert result.status == "suggest"
    assert result.source.startswith("llm:")


# ---------------------------------------------------------------------------
# 8. Engine integration — LLM NOT called when Tier 1 matched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_engine_async_does_not_call_llm_when_rule_matched():
    """When a vendor rule fires, the LLM is never invoked."""
    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    with patch("httpx.AsyncClient") as mock_client:
        result = await categorize_transaction_async(
            payee=None,
            description="CHECKCARD 0101 CLAUDE.AI SUBSCRI CA SYNTHETIC RECURRING",
            memo=None,
            amount=-20.00,
            overrides=None,
            llm_fallback=categorizer,
        )

    # vendor rule "claude.ai" should have fired
    assert result.category == "SaaS · AI"
    assert result.status == "auto"
    mock_client.assert_not_called()


# ---------------------------------------------------------------------------
# 9. health() probe
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_returns_reachable_true_on_200():
    """health() returns reachable=True when /models returns 200."""
    models_body = {"object": "list", "data": [{"id": "fake-model"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=models_body)

    transport = httpx.MockTransport(handler)
    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await categorizer.health()

    assert result["reachable"] is True
    assert result["endpoint"] == "http://fake-llm.internal/v1"
    assert result["model"] == "fake-model"


@pytest.mark.asyncio
async def test_health_returns_reachable_false_on_connection_error():
    """health() returns reachable=False when the endpoint is unreachable."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(handler)
    categorizer = LLMCategorizer(settings=_TEST_SETTINGS)
    with patch("httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)):
        result = await categorizer.health()

    assert result["reachable"] is False
    assert "error" in result
