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


# -------------------- LOCAL-FIRST TIER --------------------


def _local_on(monkeypatch, prefer: bool = True):
    """Configure a local endpoint + stub the per-workspace prefer_local flag."""
    monkeypatch.setenv("AO_CHAT_LOCAL_BASE_URL", "http://bigboy:8085/v1")
    monkeypatch.setenv("AO_CHAT_LOCAL_MODEL", "Qwen3.6-35B-A3B")
    monkeypatch.setenv("AO_CHAT_LOCAL_API_KEY", "sk-local")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _clear_settings()
    import app.engagement.policy as ep

    monkeypatch.setattr(ep, "get_prefer_local", AsyncMock(return_value=prefer))


@pytest.mark.asyncio
async def test_local_tier_when_flag_on(monkeypatch):
    _flag_off(monkeypatch)
    _local_on(monkeypatch, prefer=True)
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "local"
    assert r.base_url == "http://bigboy:8085/v1"
    assert r.model == "Qwen3.6-35B-A3B"
    assert r.api_key == "sk-local"


@pytest.mark.asyncio
async def test_local_off_falls_through_to_openrouter(monkeypatch):
    _flag_off(monkeypatch)
    _local_on(monkeypatch, prefer=False)  # endpoint configured but workspace opts out
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "openrouter_default"
    assert r.base_url == _OR_URL


@pytest.mark.asyncio
async def test_local_requested_model_wins(monkeypatch):
    _flag_off(monkeypatch)
    _local_on(monkeypatch, prefer=True)
    r = await resolve_chat_inference(
        workspace_id="w1", persona=get_persona("cfo-agent"), requested_model="x/y"
    )
    assert r.source == "local"
    assert r.model == "x/y"


@pytest.mark.asyncio
async def test_byok_still_overrides_local(monkeypatch):
    _flag_off(monkeypatch)
    _local_on(monkeypatch, prefer=True)
    import app.security.workspace_llm_keys as wk

    monkeypatch.setattr(
        wk, "get_byok",
        AsyncMock(return_value=("sk-cust", "anthropic", "https://api.anthropic.com")),
    )
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "byok"  # a customer's explicit endpoint wins over local-first


@pytest.mark.asyncio
async def test_local_failsafe_on_flag_error(monkeypatch):
    _flag_off(monkeypatch)
    monkeypatch.setenv("AO_CHAT_LOCAL_BASE_URL", "http://bigboy:8085/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _clear_settings()
    import app.engagement.policy as ep

    monkeypatch.setattr(ep, "get_prefer_local", AsyncMock(side_effect=RuntimeError("db down")))
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "openrouter_default"  # never breaks live chat


@pytest.mark.asyncio
async def test_instance_default_forces_local_without_per_workspace_flag(monkeypatch):
    # AO_CHAT_LOCAL_DEFAULT=1 ⇒ every workspace is local-first even though the
    # per-workspace flag is OFF (and without ever reading the policy store).
    _flag_off(monkeypatch)
    _local_on(monkeypatch, prefer=False)  # per-workspace flag explicitly OFF
    monkeypatch.setenv("AO_CHAT_LOCAL_DEFAULT", "1")
    _clear_settings()
    import app.engagement.policy as ep

    # The DB read must NOT be consulted when the instance default is on.
    spy = AsyncMock(side_effect=AssertionError("get_prefer_local should be skipped"))
    monkeypatch.setattr(ep, "get_prefer_local", spy)

    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "local"
    assert r.base_url == "http://bigboy:8085/v1"
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_instance_default_noop_without_local_endpoint(monkeypatch):
    # The default is gated on a configured endpoint — on with no base_url ⇒ the
    # resolver still behaves exactly as today (no accidental local routing).
    _flag_off(monkeypatch)
    monkeypatch.delenv("AO_CHAT_LOCAL_BASE_URL", raising=False)
    monkeypatch.setenv("AO_CHAT_LOCAL_DEFAULT", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _clear_settings()
    r = await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
    assert r.source == "openrouter_default"


# -------------------- PERSONA MODEL MAP --------------------


def test_persona_model_map():
    assert get_persona("cfo-agent").chat_model == GATEWAY_MODEL_OPUS
    assert get_persona("cpa-tax-resolver").chat_model == GATEWAY_MODEL_SONNET
    assert get_persona("form-1120-preparer").chat_model == GATEWAY_MODEL_SONNET
