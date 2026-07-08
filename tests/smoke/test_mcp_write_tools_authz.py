"""Smoke tests: the MCP WRITE tools are gated by the token, not their arguments.

These run WITHOUT a live stack (no DB, no docker) by monkeypatching the
membership lookup the auth bridge uses, exactly like tests/smoke/test_mcp_auth.py.
They prove — per write tool — that ``authorize_workspace`` denies the call BEFORE
any database work when:

  * the caller's role is below the write floor (viewer cannot write),
  * the caller has no membership in the requested workspace (cross-workspace),
  * the caller's membership is entity-scoped and the requested entity is out of
    scope.

`get_runway` is a read tool (viewer floor); we assert a viewer is NOT denied at
the authz layer (it fails later, only because there's no DB here — proving the
gate let it through). `propose_payment` MUST remain unimplemented.

Each negative control asserts a PermissionError raised by the auth bridge, so the
denial is provably at the token-binding layer and never reaches the ledger.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

import app.mcp_auth as mcp_auth
from app.auth import AuthContext

WS_A = uuid.uuid4()
WS_B = uuid.uuid4()
ENT_IN = uuid.uuid4()
ENT_OUT = uuid.uuid4()

# Args common to the entity-scoped write tools (values are deliberately valid so
# the ONLY thing that can stop the call is authorization).
_TX_ID = uuid.uuid4()
_RECEIPT_ID = uuid.uuid4()
_RULE_KIND = "expense_cap"
_RULE_PARAMS = {"max_usd": 500}


def _auth(uc_uid: str = "tester") -> AuthContext:
    return AuthContext(uc_uid=uc_uid, agent_id=None, scopes=("service",), claims={})


def _set_auth(ctx: AuthContext | None):
    return mcp_auth._current_auth.set(ctx)


def _membership(role: str, entity_scope=None):
    """Fake _resolve_membership: member of WS_A only, with the given role/scope."""
    async def _fake(uc_uid, workspace_id):
        if workspace_id != WS_A:
            raise HTTPException(status_code=403, detail="No active membership")
        return role, entity_scope, False, "ws-a"
    return _fake


# Each write tool invoked with WS_A / ENT_IN and otherwise-valid args. Returns the
# coroutine factory so every test can call the tool fresh under its own auth.
async def _call_categorize():
    from app.tools import categorize_transaction
    return await categorize_transaction(WS_A, ENT_IN, _TX_ID, "SaaS · AI", "because")


async def _call_link_receipt():
    from app.tools import link_receipt_to_transaction
    return await link_receipt_to_transaction(WS_A, ENT_IN, _RECEIPT_ID, _TX_ID)


async def _call_add_rule():
    from app.tools import add_specialty_rule
    return await add_specialty_rule(WS_A, ENT_IN, _RULE_KIND, _RULE_PARAMS, "because")


WRITE_TOOLS = [
    pytest.param(_call_categorize, id="categorize"),
    pytest.param(_call_link_receipt, id="link_receipt"),
    pytest.param(_call_add_rule, id="add_rule"),
]


# --------------------------------------------------------------------------
# Role denial: a viewer cannot drive any write tool.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("call", WRITE_TOOLS)
async def test_write_tool_denies_viewer(monkeypatch, call):
    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("viewer"))
    token = _set_auth(_auth())
    try:
        with pytest.raises(PermissionError):
            await call()
    finally:
        mcp_auth._current_auth.reset(token)


# --------------------------------------------------------------------------
# Cross-workspace denial: member of A asking to write into B (a valid arg).
# --------------------------------------------------------------------------
@pytest.mark.parametrize("call", WRITE_TOOLS)
async def test_write_tool_denies_cross_workspace(monkeypatch, call):
    # Caller is an owner — but only of WS_A. We rebind every tool's workspace to
    # WS_B by patching the tool args via a thin wrapper.
    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("owner"))
    token = _set_auth(_auth())
    try:
        with pytest.raises(PermissionError):
            await _rebind_to_ws_b(call)
    finally:
        mcp_auth._current_auth.reset(token)


async def _rebind_to_ws_b(call):
    """Re-invoke the same tool but targeting WS_B (no membership) — proves the
    workspace ARGUMENT can't grant reach the token doesn't have."""
    from app.tools import (
        add_specialty_rule,
        categorize_transaction,
        link_receipt_to_transaction,
    )
    name = call.__name__
    if name == "_call_categorize":
        return await categorize_transaction(WS_B, ENT_IN, _TX_ID, "SaaS · AI", "x")
    if name == "_call_link_receipt":
        return await link_receipt_to_transaction(WS_B, ENT_IN, _RECEIPT_ID, _TX_ID)
    return await add_specialty_rule(WS_B, ENT_IN, _RULE_KIND, _RULE_PARAMS, "x")


# --------------------------------------------------------------------------
# Entity-scope denial: a scoped accountant/admin writing OUTSIDE their scope.
# --------------------------------------------------------------------------
async def _call_categorize_out():
    from app.tools import categorize_transaction
    return await categorize_transaction(WS_A, ENT_OUT, _TX_ID, "SaaS · AI", "x")


async def _call_link_receipt_out():
    from app.tools import link_receipt_to_transaction
    return await link_receipt_to_transaction(WS_A, ENT_OUT, _RECEIPT_ID, _TX_ID)


async def _call_add_rule_out():
    from app.tools import add_specialty_rule
    return await add_specialty_rule(WS_A, ENT_OUT, _RULE_KIND, _RULE_PARAMS, "x")


@pytest.mark.parametrize(
    "call,role",
    [
        pytest.param(_call_categorize_out, "accountant", id="categorize"),
        pytest.param(_call_link_receipt_out, "accountant", id="link_receipt"),
        pytest.param(_call_add_rule_out, "admin", id="add_rule"),
    ],
)
async def test_write_tool_denies_out_of_scope_entity(monkeypatch, call, role):
    # Member has the REQUIRED write role, but scoped to ENT_IN only.
    monkeypatch.setattr(
        mcp_auth, "_resolve_membership", _membership(role, (ENT_IN,))
    )
    token = _set_auth(_auth())
    try:
        with pytest.raises(PermissionError):
            await call()
    finally:
        mcp_auth._current_auth.reset(token)


# --------------------------------------------------------------------------
# get_runway is a READ tool: a viewer passes the gate (it only fails later for
# lack of a DB here — which is proof the authz layer let it through).
# --------------------------------------------------------------------------
async def test_get_runway_viewer_passes_authz_then_fails_on_db(monkeypatch):
    from app.tools import get_runway

    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("viewer"))
    token = _set_auth(_auth())
    try:
        # Not a PermissionError: the viewer is authorized. It raises later when it
        # tries to reach the (absent) database — i.e. the gate already passed.
        with pytest.raises(Exception) as excinfo:
            await get_runway(WS_A, ENT_IN, {"window_months": 3})
        assert not isinstance(excinfo.value, PermissionError)
    finally:
        mcp_auth._current_auth.reset(token)


async def test_get_runway_denies_cross_workspace(monkeypatch):
    from app.tools import get_runway

    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("viewer"))
    token = _set_auth(_auth())
    try:
        with pytest.raises(PermissionError):
            await get_runway(WS_B, ENT_IN, {})
    finally:
        mcp_auth._current_auth.reset(token)


# --------------------------------------------------------------------------
# propose_payment MUST stay unimplemented (deliberate money-movement guardrail).
# --------------------------------------------------------------------------
async def test_propose_payment_remains_unimplemented(monkeypatch):
    from decimal import Decimal

    from app.tools import propose_payment

    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership("owner"))
    token = _set_auth(_auth())
    try:
        with pytest.raises(NotImplementedError):
            await propose_payment(
                WS_A, ENT_IN, Decimal("1.00"), {"to": "x"}, "should never run"
            )
    finally:
        mcp_auth._current_auth.reset(token)
