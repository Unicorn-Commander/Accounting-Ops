"""Chat-inference resolver tests — the DEFAULT-SAFETY proof for routing frontier
inference through the UC LiteLLM gateway. Pure-python, mocked; no network/DB.

The critical guarantee: with the feature flag OFF and no per-workspace key, the
resolver returns the same (base_url, api_key, model) the chat runner uses today,
so wiring it into chat.py changes nothing until an operator opts in.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

import agents.chat.inference_config as ic
from agents.chat.inference_config import resolve_chat_inference
from agents.chat.persona_registry import (
    GATEWAY_MODEL_OPUS,
    GATEWAY_MODEL_SONNET,
    get_persona,
)

# These literals are the shared contract with agents/chat/runner.py's
# DEFAULT_OPENROUTER_URL + DEFAULT_MODEL — the OFF path must reproduce them.
_OR_URL = "https://openrouter.ai/api/v1"
_OR_MODEL = "anthropic/claude-sonnet-4.5"


def _clear_settings():
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Default each test to: no BYOK, no virtual key, fresh settings cache."""
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(wk, "get_byok", AsyncMock(return_value=None))
    monkeypatch.setattr(wk, "get_or_mint_virtual_key", AsyncMock(return_value=None))
    _clear_settings()
    yield
    _clear_settings()


def _flag_off(monkeypatch):
    monkeypatch.delenv("AO_ROUTE_CHAT_THROUGH_GATEWAY", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    monkeypatch.delenv("CHAT_DEFAULT_MODEL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _clear_settings()


def _flag_on(monkeypatch):
    monkeypatch.setenv("AO_ROUTE_CHAT_THROUGH_GATEWAY", "1")
    monkeypatch.setenv("LLM_GATEWAY_BASE_URL", "https://llm.unicorncommander.ai")
    monkeypatch.setenv("LLM_PLATFORM_VIRTUAL_KEY", "sk-platform")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _clear_settings()


# -------------------- DEFAULT SAFETY --------------------


@pytest.mark.asyncio
async def test_default_safe_when_flag_off(monkeypatch):
    _flag_off(monkeypatch)
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "openrouter_default"
    assert r.base_url == _OR_URL          # == runner.DEFAULT_OPENROUTER_URL
    assert r.model == _OR_MODEL           # == runner.DEFAULT_MODEL (no override)
    assert r.api_key == "sk-or-test"      # == os.environ["OPENROUTER_API_KEY"]


@pytest.mark.asyncio
async def test_requested_model_always_wins(monkeypatch):
    _flag_off(monkeypatch)
    r = await resolve_chat_inference(
        workspace_id="w1", persona=get_persona("cfo-agent"), requested_model="x/y"
    )
    assert r.model == "x/y"


def test_resolver_never_reads_llm_api_key_from_env():
    # The categorizer's LLM_API_KEY must never leak into the chat path. We check
    # for the env-READ pattern (quoted key name, as os.environ/getenv would use)
    # — not the bare word, which the module docstring legitimately references.
    src = inspect.getsource(ic)
    assert '"LLM_API_KEY"' not in src
    assert "'LLM_API_KEY'" not in src


@pytest.mark.asyncio
async def test_resolver_ignores_llm_api_key_at_runtime(monkeypatch):
    _flag_off(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-CATEGORIZER")  # must NOT be picked up
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    _clear_settings()
    r = await resolve_chat_inference(workspace_id="w1")
    assert r.api_key == "sk-or"


# -------------------- PRECEDENCE --------------------


@pytest.mark.asyncio
async def test_byok_through_gateway_highest(monkeypatch):
    _flag_on(monkeypatch)
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(wk, "get_byok", AsyncMock(return_value=("sk-cust", "anthropic", None)))
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert (r.source, r.api_key, r.base_url, r.model) == (
        "byok",
        "sk-cust",
        "https://llm.unicorncommander.ai",
        GATEWAY_MODEL_OPUS,  # CFO tier
    )


@pytest.mark.asyncio
async def test_byok_direct_endpoint(monkeypatch):
    _flag_on(monkeypatch)
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(
        wk, "get_byok", AsyncMock(return_value=("sk-cust", "anthropic", "https://api.anthropic.com"))
    )
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "byok"
    assert r.base_url == "https://api.anthropic.com"
    assert r.model == _OR_MODEL  # direct endpoint → today's model, not the gateway alias


@pytest.mark.asyncio
async def test_platform_virtual_when_on(monkeypatch):
    _flag_on(monkeypatch)
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(wk, "get_or_mint_virtual_key", AsyncMock(return_value="sk-virtual"))
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert (r.source, r.api_key, r.base_url, r.model) == (
        "platform_virtual",
        "sk-virtual",
        "https://llm.unicorncommander.ai",
        GATEWAY_MODEL_OPUS,
    )


@pytest.mark.asyncio
async def test_platform_falls_back_to_settings_key(monkeypatch):
    _flag_on(monkeypatch)  # LLM_PLATFORM_VIRTUAL_KEY=sk-platform; mint returns None
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cpa-tax-resolver"))
    assert r.source == "platform_virtual"
    assert r.api_key == "sk-platform"
    assert r.model == GATEWAY_MODEL_SONNET  # non-CFO → default tier


@pytest.mark.asyncio
async def test_consult_forces_sonnet_even_for_cfo(monkeypatch):
    _flag_on(monkeypatch)
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(wk, "get_or_mint_virtual_key", AsyncMock(return_value="sk-virtual"))
    r = await resolve_chat_inference(
        workspace_id="w1", persona=get_persona("cfo-agent"), is_consult=True
    )
    assert r.model == GATEWAY_MODEL_SONNET


# -------------------- FAIL-SAFE --------------------


@pytest.mark.asyncio
async def test_failsafe_falls_through_on_exception(monkeypatch):
    _flag_on(monkeypatch)
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(wk, "get_byok", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(wk, "get_or_mint_virtual_key", AsyncMock(side_effect=RuntimeError("gw down")))
    monkeypatch.delenv("LLM_PLATFORM_VIRTUAL_KEY", raising=False)
    _clear_settings()
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "openrouter_default"  # never breaks live chat


# -------------------- PERSONA MODEL MAP --------------------


def test_persona_model_map():
    assert get_persona("cfo-agent").chat_model == GATEWAY_MODEL_OPUS
    assert get_persona("cpa-tax-resolver").chat_model == GATEWAY_MODEL_SONNET
    assert get_persona("form-1120-preparer").chat_model == GATEWAY_MODEL_SONNET
