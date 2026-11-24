"""AO111-R2-F1: MCP list_category_overrides carries the HTTP floor + scope (no DB).

Before the fix the MCP twin ran its own unfiltered SELECT at min_role viewer,
so a viewer or a client_user read every entity's rules and the
created_by_uc_uid staff ids even though the HTTP GET refused them. The twin
now authorizes at bookkeeper and delegates to the HTTP handler, which applies
the membership's entity_scope.

The membership lookup is faked the way tests/smoke/test_mcp_write_tools_authz.py
does it; the fake session applies the bound entity_scope to a fixed row set,
so the assertion is on which rows come back.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException

import app.mcp_auth as mcp_auth
from app.api import category_overrides as co
from app.auth import AuthContext

WS = uuid.UUID("32323232-3232-3232-3232-323232323232")
ENTITY_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000001a")
ENTITY_B = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000001b")

ROWS = [
    {"id": "r-a", "entity_id": str(ENTITY_A), "pattern": "acme", "created_by_uc_uid": "staff-1"},
    {"id": "r-b", "entity_id": str(ENTITY_B), "pattern": "globex", "created_by_uc_uid": "staff-2"},
    {"id": "r-ws", "entity_id": None, "pattern": "initech", "created_by_uc_uid": "staff-3"},
]


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Session:
    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        params = params or {}
        rows = ROWS
        if "ANY(:entity_scope)" in sql:
            allowed = set(params["entity_scope"])
            rows = [r for r in ROWS if r["entity_id"] is None or r["entity_id"] in allowed]
        return _Result(rows)


@pytest.fixture
def sessions(monkeypatch):
    state = {"opened": 0}

    class _Ctx:
        async def __aenter__(self):
            state["opened"] += 1
            return _Session()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(co, "workspace_session", lambda *a, **k: _Ctx())
    return state


def _member(monkeypatch, role: str, scope=None) -> None:
    async def _fake(uc_uid, workspace_id, *, autoprovision=False):
        if workspace_id != WS:
            raise HTTPException(status_code=403, detail="No active membership")
        return role, scope, False, "fixture-books"

    monkeypatch.setattr(mcp_auth, "_resolve_membership", _fake)


async def _call():
    from app.tools.connectors import list_category_overrides

    token = mcp_auth._current_auth.set(
        AuthContext(uc_uid="fixture-user", agent_id=None, scopes=("service",), claims={})
    )
    try:
        return await list_category_overrides(WS)
    finally:
        mcp_auth._current_auth.reset(token)


@pytest.mark.parametrize(
    "role,scope",
    [("viewer", None), ("client_user", (ENTITY_A,))],
    ids=["viewer", "client_user_scoped_A"],
)
async def test_below_bookkeeper_is_refused_before_any_query(monkeypatch, sessions, role, scope):
    _member(monkeypatch, role, scope)
    with pytest.raises(PermissionError):
        await _call()
    assert sessions["opened"] == 0


@pytest.mark.parametrize("role", ["bookkeeper", "accountant"])
async def test_scoped_member_sees_only_own_entity_and_workspace_wide(monkeypatch, sessions, role):
    _member(monkeypatch, role, (ENTITY_A,))
    out = await _call()
    assert {r["id"] for r in out["overrides"]} == {"r-a", "r-ws"}
    assert "staff-2" not in repr(out)


@pytest.mark.parametrize("role", ["bookkeeper", "owner"])
async def test_unscoped_member_sees_all(monkeypatch, sessions, role):
    _member(monkeypatch, role)
    out = await _call()
    assert {r["id"] for r in out["overrides"]} == {"r-a", "r-b", "r-ws"}
