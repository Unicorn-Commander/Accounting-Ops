"""Tests for the two-tier categorizer: settings inheritance + frontier escalation.

All inputs are SYNTHETIC. No real network calls — every HTTP interaction is
intercepted by an ``httpx.MockTransport``.

Why this file exists
--------------------
The two-tier categorizer (``feat/ai-categorize-tier2``) shipped referencing
EIGHT ``settings.categorizer_*`` attributes that existed nowhere — not in
``app/config.py``, not on the branch, not on the deploy host. Every call would
have raised ``AttributeError: 'Settings' object has no attribute
'categorizer_model'`` at runtime. The branch carried no tests, so nothing
caught it; it was found only by merging and running the suite.

These tests pin the three properties that failure violated:

1.  The ``categorizer_*`` primaries INHERIT the effective ``llm_*`` values, via
    a model validator, so EVERY construction path gets them (the inheritance
    originally lived only in ``get_settings()``, which left directly-constructed
    Settings with an empty endpoint → the URL "/chat/completions" with no base).
2.  Escalation is OFF unless a fallback model is configured — adding these
    fields must not change behavior on its own.
3.  When escalation IS configured it fires on low confidence / abstention, and
    the reported ``model`` records which rung actually answered.

Plus a guard that outbound calls carry the non-blocked User-Agent (Cloudflare
403s the openai SDK's default UA with error 1010).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.categorization.llm import LLMCategorizer
from app.config import Settings, get_settings

_ALLOWED = ["SaaS · AI", "Office · Supplies", "Uncategorized"]


def _completion(category: str, confidence: float, model: str = "m") -> dict[str, Any]:
    return {
        "id": "cmpl-synthetic",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "category": category,
                            "confidence": confidence,
                            "reasoning": "synthetic",
                        }
                    ),
                },
                "finish_reason": "stop",
            }
        ],
    }


async def _suggest(categorizer: LLMCategorizer, transport: httpx.MockTransport):
    # NB: side_effect (a factory), not return_value. _run_model opens each call
    # in `async with httpx.AsyncClient(...)`, which CLOSES the instance on exit.
    # A single shared instance therefore blows up on the escalation call with
    # "Cannot reopen a client instance, once it has been closed."
    real_client_cls = httpx.AsyncClient  # capture BEFORE patching, else recursion

    def _new_client(*_a, **_kw):
        return real_client_cls(transport=transport)

    with patch("httpx.AsyncClient", side_effect=_new_client):
        return await categorizer.suggest(
            payee=None,
            description="SYNTH-VENDOR-0001",
            memo=None,
            amount=Decimal("-10.00"),
            allowed_categories=_ALLOWED,
        )


# ---------------------------------------------------------------------------
# 1. Settings inheritance — the exact bug that shipped
# ---------------------------------------------------------------------------

def test_categorizer_settings_inherit_on_direct_construction():
    """Directly-constructed Settings must inherit llm_* into categorizer_*.

    Regression guard: when the inheritance lived only in get_settings(), a
    direct Settings(...) left categorizer_endpoint_url empty and the categorizer
    built the URL "/chat/completions" with no base -> ValueError at request time.
    """
    s = Settings(
        llm_endpoint_url="http://fake-llm.internal/v1",
        llm_model="primary-model",
        llm_api_key="secret",
        llm_timeout_s=11,
    )
    assert s.categorizer_endpoint_url == "http://fake-llm.internal/v1"
    assert s.categorizer_model == "primary-model"
    assert s.categorizer_api_key == "secret"
    assert s.categorizer_timeout_s == 11


def test_categorizer_settings_inherit_via_factory():
    s = get_settings()
    assert s.categorizer_endpoint_url == s.llm_endpoint_url
    assert s.categorizer_model == s.llm_model
    assert s.categorizer_timeout_s == s.llm_timeout_s


def test_explicit_categorizer_overrides_win():
    """An explicit categorizer_* value must NOT be clobbered by inheritance."""
    s = Settings(
        llm_endpoint_url="http://primary/v1",
        llm_model="primary-model",
        categorizer_endpoint_url="http://categorizer-only/v1",
        categorizer_model="categorizer-only-model",
    )
    assert s.categorizer_endpoint_url == "http://categorizer-only/v1"
    assert s.categorizer_model == "categorizer-only-model"
    # untouched fields still inherit
    assert s.categorizer_api_key == s.llm_api_key


def test_every_referenced_categorizer_attr_exists():
    """Every settings.categorizer_* the module reads must resolve.

    This is the guard that would have caught the original AttributeError. It
    reads the attribute names out of the source rather than hardcoding a list,
    so a newly-referenced setting fails here instead of in production.
    """
    import re
    from pathlib import Path

    src = Path("app/categorization/llm.py").read_text()
    referenced = set(re.findall(r"settings\.(categorizer_[a-z_]+)", src))
    assert referenced, "expected the module to reference categorizer_* settings"
    s = get_settings()
    missing = [a for a in sorted(referenced) if not hasattr(s, a)]
    assert not missing, f"Settings is missing referenced attributes: {missing}"


# ---------------------------------------------------------------------------
# 2. Escalation OFF by default
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_escalation_when_fallback_model_unset():
    """With no fallback model, exactly ONE call is made (single-tier behavior)."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        # Low confidence — would escalate IF a fallback were configured.
        return httpx.Response(200, json=_completion("Uncategorized", 0.10))

    s = Settings(
        llm_endpoint_url="http://fake-llm.internal/v1",
        llm_model="primary-model",
        llm_timeout_s=5,
    )
    assert s.categorizer_fallback_model == ""  # default: escalation disabled

    result = await _suggest(LLMCategorizer(settings=s), httpx.MockTransport(handler))
    assert calls == ["primary-model"], f"expected 1 primary call, got {calls}"
    assert result is not None
    assert result.model == "primary-model"


# ---------------------------------------------------------------------------
# 3. Escalation ON — fires on abstention and on low confidence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("primary_category", "primary_confidence", "why"),
    [
        ("Uncategorized", 0.99, "abstention escalates regardless of confidence"),
        ("SaaS · AI", 0.10, "confidence below threshold escalates"),
    ],
)
async def test_escalates_to_frontier(primary_category, primary_confidence, why):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "frontier-model":
            return httpx.Response(200, json=_completion("Office · Supplies", 0.95))
        return httpx.Response(200, json=_completion(primary_category, primary_confidence))

    s = Settings(
        llm_endpoint_url="http://fake-llm.internal/v1",
        llm_model="primary-model",
        llm_timeout_s=5,
        categorizer_fallback_model="frontier-model",
        categorizer_escalate_below_confidence=0.60,
    )

    result = await _suggest(LLMCategorizer(settings=s), httpx.MockTransport(handler))
    assert calls == ["primary-model", "frontier-model"], why
    assert result is not None
    # The escalated answer wins, and records WHICH model produced it.
    assert result.category == "Office · Supplies"
    assert result.model == "frontier-model"


@pytest.mark.asyncio
async def test_confident_primary_does_not_escalate():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=_completion("SaaS · AI", 0.97))

    s = Settings(
        llm_endpoint_url="http://fake-llm.internal/v1",
        llm_model="primary-model",
        llm_timeout_s=5,
        categorizer_fallback_model="frontier-model",
        categorizer_escalate_below_confidence=0.60,
    )

    result = await _suggest(LLMCategorizer(settings=s), httpx.MockTransport(handler))
    assert calls == ["primary-model"], "a confident primary must not burn a frontier call"
    assert result is not None and result.model == "primary-model"


# ---------------------------------------------------------------------------
# 4. Cloudflare-safe User-Agent on outbound calls
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_outbound_calls_carry_non_sdk_user_agent():
    """Cloudflare 403s (error 1010) the openai SDK's default "OpenAI/Python" UA.

    Both rungs route through _run_model, so one assertion covers both.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return httpx.Response(200, json=_completion("SaaS · AI", 0.95))

    s = Settings(
        llm_endpoint_url="http://fake-llm.internal/v1",
        llm_model="primary-model",
        llm_timeout_s=5,
    )
    await _suggest(LLMCategorizer(settings=s), httpx.MockTransport(handler))

    assert seen, "expected at least one outbound request"
    for ua in seen:
        assert ua, "User-Agent must be set explicitly"
        assert not ua.startswith("OpenAI/"), f"CF-blocked SDK UA leaked: {ua!r}"
        assert not ua.startswith("python-httpx"), f"default httpx UA leaked: {ua!r}"
