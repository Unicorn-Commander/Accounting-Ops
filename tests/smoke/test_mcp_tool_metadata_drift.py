"""AO-MCP-2: discovery role floors match the tools; get_current_rule needs a caller."""
from __future__ import annotations

import inspect

import pytest

from app.api.discovery import _TOOL_METADATA
from app.tools import approvals as approvals_tools
from app.tools import tax_get_current_rule as rule_mod


def test_list_pending_approvals_floor_matches_the_tool():
    src = inspect.getsource(approvals_tools.list_pending_approvals)
    assert 'min_role="accountant"' in src
    assert _TOOL_METADATA["list_pending_approvals"]["min_role"] == "accountant"


@pytest.mark.asyncio
async def test_get_current_rule_refuses_without_a_bound_caller(monkeypatch):
    def _no_db(*a, **k):
        raise AssertionError("no DB session may open for an unauthenticated caller")

    monkeypatch.setattr(rule_mod, "SessionLocal", _no_db)
    monkeypatch.setattr("app.tools.tax_knowledge.current_auth", lambda: None)
    with pytest.raises(PermissionError):
        await rule_mod.get_current_rule(jurisdiction="US", topic="174")
