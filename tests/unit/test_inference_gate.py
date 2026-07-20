"""Unit tests for the hosted smart-inference credit/entitlement gate.

Pure decision logic — no DB, no network. ``get_settings`` is stubbed and the
lazily-imported ``get_byok`` is injected via ``sys.modules`` so the heavy
security module is never imported.
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from app.billing import inference_gate
from app.billing.inference_gate import (
    SmartInferenceGateError,
    check_smart_inference_access,
)

ENTITLED = {"entitlements": ["accounting-ops"]}
NOT_ENTITLED = {"entitlements": ["project-ops"]}


def _settings(**over):
    base = dict(
        is_community=False,
        smart_gate_enabled=True,
        smart_gate_byok_bypass=True,
        ops_center_billing_url="https://platform.example.com/billing",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patch_settings(monkeypatch, **over):
    monkeypatch.setattr(inference_gate, "get_settings", lambda: _settings(**over))


def _patch_byok(monkeypatch, behavior):
    """Inject a fake ``app.security.workspace_llm_keys`` so the gate's lazy import
    resolves to ``behavior`` without importing the real (DB-bound) module."""
    mod = types.ModuleType("app.security.workspace_llm_keys")
    mod.get_byok = behavior  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.security.workspace_llm_keys", mod)


async def _byok_present(_wid):
    return ("sk-fake", "anthropic", None)


async def _byok_absent(_wid):
    return None


async def _byok_boom(_wid):
    raise RuntimeError("credential store unavailable")


def _run(**kw):
    return asyncio.run(check_smart_inference_access(workspace_id="ws1", **kw))


def test_community_edition_never_gates(monkeypatch):
    _patch_settings(monkeypatch, is_community=True)
    _patch_byok(monkeypatch, _byok_absent)
    # Even with no entitlement + no BYOK, community must pass.
    _run(claims=NOT_ENTITLED)  # no raise


def test_gate_disabled_allows(monkeypatch):
    _patch_settings(monkeypatch, smart_gate_enabled=False)
    _patch_byok(monkeypatch, _byok_absent)
    _run(claims=NOT_ENTITLED)  # no raise


def test_entitled_allows(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_byok(monkeypatch, _byok_absent)
    _run(claims=ENTITLED)  # no raise


def test_byok_bypasses_when_not_entitled(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_byok(monkeypatch, _byok_present)
    _run(claims=NOT_ENTITLED)  # BYOK present ⇒ allowed


def test_blocks_when_no_entitlement_no_byok(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_byok(monkeypatch, _byok_absent)
    with pytest.raises(SmartInferenceGateError) as ei:
        _run(claims=NOT_ENTITLED)
    err = ei.value
    assert err.http_status == 402
    assert err.upgrade_url == "https://platform.example.com/billing"


def test_byok_bypass_disabled_still_blocks(monkeypatch):
    _patch_settings(monkeypatch, smart_gate_byok_bypass=False)
    _patch_byok(monkeypatch, _byok_present)  # BYOK present but bypass OFF
    with pytest.raises(SmartInferenceGateError):
        _run(claims=NOT_ENTITLED)


def test_byok_lookup_error_fails_closed(monkeypatch):
    # A failing BYOK lookup must NOT fail open — it degrades to the entitlement
    # check, which (unentitled) blocks.
    _patch_settings(monkeypatch)
    _patch_byok(monkeypatch, _byok_boom)
    with pytest.raises(SmartInferenceGateError):
        _run(claims=NOT_ENTITLED)


def test_byok_error_but_entitled_allows(monkeypatch):
    # BYOK lookup blows up but the workspace is entitled ⇒ still allowed.
    _patch_settings(monkeypatch)
    _patch_byok(monkeypatch, _byok_boom)
    _run(claims=ENTITLED)  # no raise
