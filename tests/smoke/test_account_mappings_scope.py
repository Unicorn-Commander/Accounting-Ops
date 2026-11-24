"""AO111-R3-F14: GET account-mappings applies the caller's entity scope (no DB).

On 49510b93 the GET only called require_role(workspace, "accountant"): the
mapping query and the entity query had no entity_scope filter, so an
accountant scoped to entity A read every entity's bank-account mappings and
the full entity list, over HTTP and over the MCP list_account_mappings twin
(which delegates). Both queries now bind the scope, and a live account
mapped to an out-of-scope entity is dropped from live_accounts.

The fake session applies the bound entity_scope to fixed row sets the way
Postgres would, so the assertions are on which rows come back.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.mcp_auth as mcp_auth
from app.api import account_mappings as am
from app.auth import AuthContext
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS = uuid.UUID("43434343-4343-4343-4343-434343434343")
ENTITY_A = "aaaaaaaa-0000-0000-0000-00000000002a"
ENTITY_B = "bbbbbbbb-0000-0000-0000-00000000002b"
URL = f"/api/workspaces/{WS}/account-mappings"

MAPPINGS = [
    {"id": "m-a", "entity_id": ENTITY_A, "source": "simplefin",
     "external_account_id": "acct-a", "external_org": "Fixture Bank",
     "external_account_name": "A checking", "notes": "a note",
     "entity_display_name": "Entity A", "entity_slug": "a",
     "auto_categorize": True, "label": None},
    {"id": "m-b", "entity_id": ENTITY_B, "source": "simplefin",
     "external_account_id": "acct-b", "external_org": "Fixture Bank",
     "external_account_name": "B checking", "notes": "b secret note",
     "entity_display_name": "Entity B", "entity_slug": "b",
     "auto_categorize": True, "label": None},
]
ENTITIES = [
    {"id": ENTITY_A, "slug": "a", "display_name": "Entity A", "kind": "llc"},
    {"id": ENTITY_B, "slug": "b", "display_name": "Entity B", "kind": "llc"},
]
LIVE = [
    {"source": "simplefin", "external_account_id": "acct-a",
     "external_org": "Fixture Bank", "external_account_name": "A checking"},
    {"source": "simplefin", "external_account_id": "acct-b",
     "external_org": "Fixture Bank", "external_account_name": "B checking"},
    {"source": "simplefin", "external_account_id": "acct-new",
     "external_org": "Fixture Bank", "external_account_name": "New savings"},
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
        scope = set((params or {}).get("entity_scope") or [])
        if "NOT (entity_id = ANY(:entity_scope))" in sql:
            return _Result([m for m in MAPPINGS if m["entity_id"] not in scope])
        if "FROM account_entity_mapping m" in sql:
            rows = MAPPINGS
            if "ANY(:entity_scope)" in sql:
                rows = [m for m in rows if m["entity_id"] in scope]
            return _Result([dict(r) for r in rows])
        if "FROM entity" in sql:
            rows = ENTITIES
            if "ANY(:entity_scope)" in sql:
                rows = [e for e in rows if e["id"] in scope]
            return _Result([dict(r) for r in rows])
        raise AssertionError(f"unexpected query: {sql}")


class _Ctx:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    async def fake_access_url(workspace):
        return "https://fixture:pw@bridge.example.com/simplefin"

    async def fake_discover(access_url):
        return [dict(a) for a in LIVE]

    monkeypatch.setattr(am, "_simplefin_access_url", fake_access_url)
    monkeypatch.setattr(am, "_discover_simplefin_accounts", fake_discover)
    monkeypatch.setattr(am, "workspace_session", lambda *a, **k: _Ctx())
    yield
    app.dependency_overrides.pop(resolve_workspace, None)


def _ctx(role: str, scope: tuple[uuid.UUID, ...] | None) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WS,
        workspace_slug="fixture-books",
        role=role,
        user_uc_uid="fixture-user",
        agent_id=None,
        entity_scope=scope,
        hipaa_protected=False,
    )


def _http(role: str, scope=None) -> dict[str, Any]:
    app.dependency_overrides[resolve_workspace] = lambda: _ctx(role, scope)
    resp = TestClient(app).get(URL)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _mcp(monkeypatch, role: str, scope=None) -> dict[str, Any]:
    async def _fake(uc_uid, workspace_id, *, autoprovision=False):
        if workspace_id != WS:
            raise HTTPException(status_code=403, detail="No active membership")
        return role, scope, False, "fixture-books"

    monkeypatch.setattr(mcp_auth, "_resolve_membership", _fake)
    from app.tools.connectors import list_account_mappings

    token = mcp_auth._current_auth.set(
        AuthContext(uc_uid="fixture-user", agent_id=None, scopes=("service",), claims={})
    )
    try:
        return await list_account_mappings(WS)
    finally:
        mcp_auth._current_auth.reset(token)


def _assert_scoped_to_a(out: dict[str, Any]) -> None:
    assert {m["id"] for m in out["mappings"]} == {"m-a"}
    assert {e["id"] for e in out["entities"]} == {ENTITY_A}
    live = {a["external_account_id"] for a in out["live_accounts"]}
    assert live == {"acct-a", "acct-new"}
    assert out["unmapped_count"] == 1
    text = repr(out)
    assert ENTITY_B not in text and "b secret note" not in text
    assert "B checking" not in text


def _assert_unscoped(out: dict[str, Any]) -> None:
    assert {m["id"] for m in out["mappings"]} == {"m-a", "m-b"}
    assert {e["id"] for e in out["entities"]} == {ENTITY_A, ENTITY_B}
    live = {a["external_account_id"] for a in out["live_accounts"]}
    assert live == {"acct-a", "acct-b", "acct-new"}
    assert out["unmapped_count"] == 1


def test_scoped_accountant_http_sees_only_entity_a():
    _assert_scoped_to_a(_http("accountant", (uuid.UUID(ENTITY_A),)))


def test_unscoped_owner_http_sees_all():
    _assert_unscoped(_http("owner"))


async def test_scoped_accountant_mcp_twin_sees_only_entity_a(monkeypatch):
    _assert_scoped_to_a(await _mcp(monkeypatch, "accountant", (uuid.UUID(ENTITY_A),)))


async def test_unscoped_owner_mcp_twin_sees_all(monkeypatch):
    _assert_unscoped(await _mcp(monkeypatch, "owner"))
