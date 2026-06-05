"""Smoke tests for the uc-registry workspace bridge (federation id resolution).

Pure-logic + mocked-DB tests — no live database. Pins:
  * is_federation_token only fires for the Brigade trusted issuer
  * federation_claim_workspace_id reads the token's workspace_id claim
  * resolve_internal_workspace_id maps registry->internal, returns the input
    UNCHANGED when unmapped, and FAILS SAFE (returns input) on any DB error
    (never widen access).
"""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.platform import workspace_registry as wr

_BRIGADE = "https://brigade.unicorncommander.ai"


def _auth(claims):
    return SimpleNamespace(claims=claims)


def test_is_federation_token_true_for_brigade_issuer(monkeypatch):
    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", _BRIGADE)
    assert wr.is_federation_token(_auth({"iss": _BRIGADE})) is True


def test_is_federation_token_false_for_others(monkeypatch):
    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", _BRIGADE)
    assert wr.is_federation_token(_auth({"iss": "https://auth.unicorncommander.ai/realms/uchub"})) is False
    assert wr.is_federation_token(_auth({})) is False
    assert wr.is_federation_token(SimpleNamespace(claims=None)) is False


def test_federation_claim_workspace_id():
    wid = str(uuid4())
    assert wr.federation_claim_workspace_id(_auth({"workspace_id": wid})) == wid
    assert wr.federation_claim_workspace_id(_auth({})) is None


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return _FakeResult(self._row)


def test_resolve_maps_registry_to_internal(monkeypatch):
    internal = uuid4()
    monkeypatch.setattr(wr, "SessionLocal", lambda: _FakeSession((internal,)))
    out = asyncio.run(wr.resolve_internal_workspace_id(uuid4()))
    assert out == internal


def test_resolve_returns_input_when_unmapped(monkeypatch):
    registry = uuid4()
    monkeypatch.setattr(wr, "SessionLocal", lambda: _FakeSession(None))
    assert asyncio.run(wr.resolve_internal_workspace_id(registry)) == registry


def test_resolve_fail_safe_on_error(monkeypatch):
    registry = uuid4()

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(wr, "SessionLocal", boom)
    # Never widen access on error — return the input unchanged (membership 403s).
    assert asyncio.run(wr.resolve_internal_workspace_id(registry)) == registry
