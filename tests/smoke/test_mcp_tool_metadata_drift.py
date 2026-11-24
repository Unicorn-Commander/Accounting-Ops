"""AO-MCP-2: discovery role floors match the tools; get_current_rule needs a caller."""
from __future__ import annotations

import pytest

from app.api.discovery import _TOOL_METADATA
from app.tools import approvals as approvals_tools
from app.tools import tax_get_current_rule as rule_mod


class _Reached(Exception):
    """Raised by the stubbed DB session: the caller got past the role floor."""


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "accountant", "admin", "owner"])
async def test_list_pending_approvals_floor_matches_by_behaviour(monkeypatch, role):
    """AO110-R2-P3-2: call the tool as each role. It must admit exactly the
    roles at or above the floor discovery advertises."""
    from uuid import uuid4

    from app import mcp_auth
    from app.auth import AuthContext
    from app.tenancy import role_at_least

    floor = _TOOL_METADATA["list_pending_approvals"]["min_role"]

    async def _membership(_uid, _wid, autoprovision=False):
        return role, None, False, "ws-example"

    def _session(*_a, **_k):
        raise _Reached()

    monkeypatch.setattr(mcp_auth, "_resolve_membership", _membership)
    monkeypatch.setattr(approvals_tools, "workspace_session", _session)
    token = mcp_auth._current_auth.set(
        AuthContext(uc_uid="user-example", agent_id=None, scopes=(), claims={})
    )
    try:
        if role_at_least(role, floor):
            with pytest.raises(_Reached):
                await approvals_tools.list_pending_approvals(workspace_id=uuid4())
        else:
            with pytest.raises(PermissionError):
                await approvals_tools.list_pending_approvals(workspace_id=uuid4())
    finally:
        mcp_auth._current_auth.reset(token)
    assert floor == "accountant"


@pytest.mark.asyncio
async def test_get_current_rule_refuses_without_a_bound_caller(monkeypatch):
    def _no_db(*a, **k):
        raise AssertionError("no DB session may open for an unauthenticated caller")

    monkeypatch.setattr(rule_mod, "SessionLocal", _no_db)
    monkeypatch.setattr("app.tools.tax_knowledge.current_auth", lambda: None)
    with pytest.raises(PermissionError):
        await rule_mod.get_current_rule(jurisdiction="US", topic="174")
