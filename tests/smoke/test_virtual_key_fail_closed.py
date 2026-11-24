"""AO-SW-2: a failed per-workspace key mint never falls back to the platform key."""
from __future__ import annotations

import sys
import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import app.security.workspace_llm_keys as wk
from agents.chat.inference_config import resolve_chat_inference
from agents.chat.persona_registry import get_persona


def _clear():
    from app.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("AO_ROUTE_CHAT_THROUGH_GATEWAY", "1")
    monkeypatch.setenv("LLM_GATEWAY_BASE_URL", "https://llm.example.com")
    monkeypatch.setenv("LLM_PLATFORM_VIRTUAL_KEY", "sk-platform")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _clear()
    yield
    _clear()


def _stub_store(monkeypatch, stored: list):
    @asynccontextmanager
    async def _session():
        s = types.SimpleNamespace(commit=AsyncMock())
        yield s

    monkeypatch.setattr(wk, "SessionLocal", _session)
    monkeypatch.setattr(wk, "get_credential_plaintext", AsyncMock(return_value=None))

    async def _store(*a, **k):
        stored.append(a)

    monkeypatch.setattr(wk, "store_credential", _store)


def _stub_admin(monkeypatch, mint):
    mod = types.ModuleType("app.security.litellm_admin")
    mod.DEFAULT_ALLOWED_MODELS = ["m"]  # type: ignore[attr-defined]
    mod.mint_virtual_key = mint  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.security.litellm_admin", mod)
    monkeypatch.setenv("LLM_GATEWAY_ADMIN_KEY", "sk-admin-test")
    _clear()
    from app.config import get_settings

    assert get_settings().llm_gateway_admin_key


@pytest.mark.asyncio
async def test_mint_failure_raises_instead_of_returning_none(monkeypatch):
    stored: list = []
    _stub_store(monkeypatch, stored)
    _stub_admin(monkeypatch, AsyncMock(side_effect=RuntimeError("gateway 502")))
    monkeypatch.setattr(wk, "resolve_billing_customer_id", AsyncMock(return_value="org_paid"))
    with pytest.raises(wk.VirtualKeyUnavailableError):
        await wk.get_or_mint_virtual_key(str(uuid.uuid4()))
    assert stored == []


@pytest.mark.asyncio
async def test_unconfigured_mint_still_returns_none(monkeypatch):
    stored: list = []
    _stub_store(monkeypatch, stored)
    monkeypatch.delenv("LLM_GATEWAY_ADMIN_KEY", raising=False)
    _clear()
    from app.config import get_settings

    assert not get_settings().llm_gateway_admin_key
    assert await wk.get_or_mint_virtual_key(str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_resolver_does_not_fall_back_to_platform_key(monkeypatch):
    monkeypatch.setattr(wk, "get_byok", AsyncMock(return_value=None))
    monkeypatch.setattr(
        wk, "get_or_mint_virtual_key",
        AsyncMock(side_effect=wk.VirtualKeyUnavailableError("w1")),
    )
    with pytest.raises(wk.VirtualKeyUnavailableError):
        await resolve_chat_inference(workspace_id="w1", persona=get_persona("cfo-agent"))
