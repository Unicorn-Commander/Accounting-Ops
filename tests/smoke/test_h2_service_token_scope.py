"""H2: service-token scope constraints.

Unit-level coverage for the new optional scope fields on
``ACCTOPS_SERVICE_TOKENS`` and the role-cap helper used by
``resolve_workspace``. Integration coverage of the actual
``resolve_workspace`` flow (workspace gate, entity-scope intersection,
role floor) lives in ``tests/integration/test_tenant_isolation.py``
which has a real database.
"""
from __future__ import annotations

from uuid import UUID

import pytest

import app.auth as auth_mod
from app.auth import (
    AuthContext,
    ServiceScope,
    _load_service_tokens,
    authenticate_token,
)
from app.tenancy import _ROLE_ORDER, _cap_role


WID_A = "11111111-1111-1111-1111-111111111111"
WID_B = "22222222-2222-2222-2222-222222222222"
EID_X = "33333333-3333-3333-3333-333333333333"
EID_Y = "44444444-4444-4444-4444-444444444444"


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Each test starts with a fresh service-tokens cache."""
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)
    yield
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_legacy_token_format_keeps_no_scope(monkeypatch):
    """A token without scope fields parses with None scope (backward compat)."""
    token = "a" * 40
    monkeypatch.setenv(
        "ACCTOPS_SERVICE_TOKENS",
        '{"%s": {"uc_uid": "u1", "agent_id": "worker"}}' % token,
    )
    tokens = _load_service_tokens()
    assert token in tokens
    ident = tokens[token]
    assert ident["uc_uid"] == "u1"
    assert ident["agent_id"] == "worker"
    assert ident["workspace_ids"] is None
    assert ident["entity_ids"] is None
    assert ident["role_floor"] is None


def test_token_with_workspace_ids_parses_as_frozenset(monkeypatch):
    token = "b" * 40
    payload = (
        '{"%s": {"uc_uid":"u1","agent_id":"w","workspace_ids":["%s","%s"]}}'
        % (token, WID_A, WID_B)
    )
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", payload)
    tokens = _load_service_tokens()
    wsids = tokens[token]["workspace_ids"]
    assert isinstance(wsids, frozenset)
    assert wsids == frozenset({UUID(WID_A), UUID(WID_B)})


def test_token_with_entity_ids_parses_as_frozenset(monkeypatch):
    token = "c" * 40
    payload = (
        '{"%s": {"uc_uid":"u1","agent_id":"w","entity_ids":["%s"]}}'
        % (token, EID_X)
    )
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", payload)
    tokens = _load_service_tokens()
    eids = tokens[token]["entity_ids"]
    assert eids == frozenset({UUID(EID_X)})


def test_token_with_role_floor_normalizes_and_validates(monkeypatch):
    token = "d" * 40
    payload = (
        '{"%s": {"uc_uid":"u1","agent_id":"w","role_floor":"Accountant"}}'
        % token
    )
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", payload)
    tokens = _load_service_tokens()
    # The parser lowercases and validates against _ROLE_ORDER.
    assert tokens[token]["role_floor"] == "accountant"


def test_token_with_invalid_role_floor_drops_field(monkeypatch):
    """An unknown role_floor parses as None (safer than crashing the token)."""
    token = "e" * 40
    payload = (
        '{"%s": {"uc_uid":"u1","agent_id":"w","role_floor":"god-mode"}}'
        % token
    )
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", payload)
    tokens = _load_service_tokens()
    assert tokens[token]["role_floor"] is None


def test_token_with_invalid_uuid_drops_only_that_entry(monkeypatch):
    """One malformed UUID in workspace_ids doesn't poison the whole token."""
    token = "f" * 40
    payload = (
        '{"%s": {"uc_uid":"u1","agent_id":"w","workspace_ids":["%s","not-a-uuid"]}}'
        % (token, WID_A)
    )
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", payload)
    tokens = _load_service_tokens()
    assert tokens[token]["workspace_ids"] == frozenset({UUID(WID_A)})


def test_token_with_empty_workspace_ids_means_deny_all(monkeypatch):
    """An explicit empty list parses to an empty frozenset (intentional deny-all)."""
    token = "g" * 40
    payload = (
        '{"%s": {"uc_uid":"u1","agent_id":"w","workspace_ids":[]}}' % token
    )
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", payload)
    tokens = _load_service_tokens()
    assert tokens[token]["workspace_ids"] == frozenset()


# ---------------------------------------------------------------------------
# authenticate_token: AuthContext.service_scope is built when scope fields exist
# ---------------------------------------------------------------------------


async def test_authenticate_legacy_service_token_yields_no_service_scope(monkeypatch):
    token = "h" * 40
    monkeypatch.setenv(
        "ACCTOPS_SERVICE_TOKENS",
        '{"%s": {"uc_uid": "u1", "agent_id": "w"}}' % token,
    )
    ctx = await authenticate_token(token)
    assert isinstance(ctx, AuthContext)
    assert ctx.service_scope is None
    assert ctx.scopes == ("service",)


async def test_authenticate_scoped_service_token_carries_servicescope(monkeypatch):
    token = "i" * 40
    payload = (
        '{"%s": {"uc_uid":"u1","agent_id":"w",'
        '"workspace_ids":["%s"],"entity_ids":["%s"],"role_floor":"viewer"}}'
        % (token, WID_A, EID_X)
    )
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKENS", payload)
    ctx = await authenticate_token(token)
    assert isinstance(ctx, AuthContext)
    assert ctx.service_scope is not None
    ss = ctx.service_scope
    assert ss.workspace_ids == frozenset({UUID(WID_A)})
    assert ss.entity_ids == frozenset({UUID(EID_X)})
    assert ss.role_floor == "viewer"


# ---------------------------------------------------------------------------
# _cap_role: min of two roles in the hierarchy
# ---------------------------------------------------------------------------


def test_cap_role_returns_actual_when_floor_is_higher():
    # actual=accountant, ceiling=admin → keep accountant (lower)
    assert _cap_role("accountant", "admin") == "accountant"


def test_cap_role_caps_actual_to_floor():
    # actual=admin, ceiling=viewer → cap to viewer
    assert _cap_role("admin", "viewer") == "viewer"


def test_cap_role_equal_returns_either():
    assert _cap_role("viewer", "viewer") == "viewer"


def test_cap_role_unknown_actual_uses_ceiling():
    assert _cap_role("badrole", "viewer") == "viewer"


def test_cap_role_unknown_ceiling_keeps_actual():
    assert _cap_role("admin", "badrole") == "admin"


def test_servicescope_is_frozen():
    ss = ServiceScope()
    with pytest.raises(Exception):
        ss.role_floor = "admin"  # type: ignore[misc]


def test_role_order_in_sync_between_modules():
    """auth._ROLE_ORDER and tenancy._ROLE_ORDER must match exactly."""
    assert auth_mod._ROLE_ORDER == _ROLE_ORDER
