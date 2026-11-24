"""AO111-R2-F3: category-override POST/DELETE honour entity_scope (no DB).

Before the fix both write routes checked only the accountant floor, so an
accountant scoped to entity A could overwrite (ON CONFLICT DO UPDATE) or
delete entity B's rules. Now a concrete entity goes through
assert_entity_in_scope, and a workspace-wide rule (entity_id NULL, which
applies to every entity) needs an unscoped membership. DELETE loads the
row's entity first.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import falkordb
from app.api import category_overrides as co
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS_ID = uuid.UUID("33333333-3131-3131-3131-313131313131")
ENTITY_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000002a")
ENTITY_B = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000002b")
RULE_A = uuid.UUID("11111111-0000-0000-0000-0000000000a1")
RULE_B = uuid.UUID("11111111-0000-0000-0000-0000000000b1")
RULE_WS = uuid.UUID("11111111-0000-0000-0000-0000000000c1")
BASE = f"/api/workspaces/{WS_ID}/category-overrides"

RULE_ENTITY = {str(RULE_A): str(ENTITY_A), str(RULE_B): str(ENTITY_B), str(RULE_WS): None}


def _ctx(role: str, scope: tuple[uuid.UUID, ...] | None = None) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WS_ID,
        workspace_slug="fixture-books",
        role=role,
        user_uc_uid="fixture-user",
        agent_id=None,
        entity_scope=scope,
        hipaa_protected=False,
    )


class _Result:
    def __init__(self, row: Any = None, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def first(self) -> Any:
        return self._row


class _Session:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        params = params or {}
        if "INSERT INTO category_override" in sql:
            self.state["writes"].append(("upsert", params["entity_id"]))
            return _Result(SimpleNamespace(id="new-id", pattern=params["pattern"], category=params["category"]))
        if sql.startswith("SELECT entity_id"):
            if params["id"] not in RULE_ENTITY:
                return _Result(None)
            return _Result(SimpleNamespace(entity_id=RULE_ENTITY[params["id"]]))
        if sql.startswith("DELETE"):
            self.state["writes"].append(("delete", params["id"]))
            return _Result(rowcount=1)
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.fixture
def state(monkeypatch):
    st: dict[str, Any] = {"writes": []}

    class _Ctx:
        async def __aenter__(self):
            return _Session(st)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(co, "workspace_session", lambda *a, **k: _Ctx())
    monkeypatch.setattr(falkordb, "project", lambda coro, **k: coro.close())
    yield st
    app.dependency_overrides.pop(resolve_workspace, None)


def _client(ctx: WorkspaceContext) -> TestClient:
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    return TestClient(app)


def _post(ctx: WorkspaceContext, entity_id: uuid.UUID | None):
    body = {"pattern": "globex", "category": "SaaS", "entity_id": str(entity_id) if entity_id else None}
    return _client(ctx).post(BASE, json=body)


SCOPED_A = _ctx("accountant", (ENTITY_A,))


def test_scoped_accountant_cannot_upsert_entity_b_rule(state):
    assert _post(SCOPED_A, ENTITY_B).status_code == 403
    assert state["writes"] == []


def test_scoped_accountant_cannot_upsert_workspace_wide_rule(state):
    assert _post(SCOPED_A, None).status_code == 403
    assert state["writes"] == []


def test_scoped_accountant_can_upsert_own_entity_rule(state):
    assert _post(SCOPED_A, ENTITY_A).status_code == 200
    assert state["writes"] == [("upsert", str(ENTITY_A))]


@pytest.mark.parametrize("role", ["accountant", "owner"])
@pytest.mark.parametrize("entity", [ENTITY_B, None], ids=["entity_b", "workspace_wide"])
def test_unscoped_member_can_upsert_any_rule(state, role, entity):
    assert _post(_ctx(role), entity).status_code == 200
    assert len(state["writes"]) == 1


@pytest.mark.parametrize("role,scope", [("bookkeeper", None), ("viewer", None), ("client_user", (ENTITY_A,))])
def test_below_accountant_cannot_write(state, role, scope):
    ctx = _ctx(role, scope)
    assert _post(ctx, ENTITY_A).status_code == 403
    assert _client(ctx).delete(f"{BASE}/{RULE_A}").status_code == 403
    assert state["writes"] == []


@pytest.mark.parametrize("rule", [RULE_B, RULE_WS], ids=["entity_b", "workspace_wide"])
def test_scoped_accountant_cannot_delete_other_rules(state, rule):
    assert _client(SCOPED_A).delete(f"{BASE}/{rule}").status_code == 403
    assert state["writes"] == []


def test_scoped_accountant_can_delete_own_entity_rule(state):
    assert _client(SCOPED_A).delete(f"{BASE}/{RULE_A}").status_code == 200
    assert state["writes"] == [("delete", str(RULE_A))]


def test_unknown_rule_is_404_without_delete(state):
    missing = uuid.UUID("11111111-0000-0000-0000-0000000000ff")
    assert _client(_ctx("owner")).delete(f"{BASE}/{missing}").status_code == 404
    assert state["writes"] == []
