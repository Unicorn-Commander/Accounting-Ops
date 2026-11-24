"""AO-CLIENT-2: GET category-overrides has a role floor and entity_scope (no DB).

Before the fix the GET had no require_role and no entity_scope filter: any
member read every entity's categorization rules plus the created_by_uc_uid
staff ids. Now: bookkeeper floor, and a scoped member sees only its own
entities' rules plus the workspace-wide (entity_id NULL) ones.

The fake session applies the route's bound entity_scope to a fixed row set,
so the assertion is on which rows come back, not on the SQL text alone.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import category_overrides as co
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS_ID = uuid.UUID("31313131-3131-3131-3131-313131313131")
ENTITY_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
ENTITY_B = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000000b")
URL = f"/api/workspaces/{WS_ID}/category-overrides"

ROWS = [
    {"id": "r-a", "entity_id": str(ENTITY_A), "pattern": "acme", "created_by_uc_uid": "staff-1"},
    {"id": "r-b", "entity_id": str(ENTITY_B), "pattern": "globex", "created_by_uc_uid": "staff-2"},
    {"id": "r-ws", "entity_id": None, "pattern": "initech", "created_by_uc_uid": "staff-3"},
]


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
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Session:
    def __init__(self, seen: dict[str, Any]) -> None:
        self.seen = seen

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        params = params or {}
        self.seen["sql"] = sql
        rows = ROWS
        if "ANY(:entity_scope)" in sql:
            allowed = set(params["entity_scope"])
            rows = [r for r in ROWS if r["entity_id"] is None or r["entity_id"] in allowed]
        return _Result(rows)


@pytest.fixture
def seen(monkeypatch):
    state: dict[str, Any] = {"sessions": 0}

    class _Ctx:
        async def __aenter__(self):
            state["sessions"] += 1
            return _Session(state)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(co, "workspace_session", lambda *a, **k: _Ctx())
    yield state
    app.dependency_overrides.pop(resolve_workspace, None)


def _get(ctx: WorkspaceContext):
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    return TestClient(app).get(URL)


@pytest.mark.parametrize(
    "ctx",
    [_ctx("viewer"), _ctx("client_user", (ENTITY_A,))],
    ids=["viewer", "client_user_scoped_A"],
)
def test_below_bookkeeper_is_refused_without_a_query(seen, ctx):
    assert _get(ctx).status_code == 403
    assert seen["sessions"] == 0


@pytest.mark.parametrize("role", ["bookkeeper", "accountant", "admin", "owner"])
def test_unscoped_members_at_or_above_bookkeeper_see_all(seen, role):
    resp = _get(_ctx(role))
    assert resp.status_code == 200
    assert {r["id"] for r in resp.json()["overrides"]} == {"r-a", "r-b", "r-ws"}


@pytest.mark.parametrize("role", ["bookkeeper", "accountant"])
def test_scoped_member_never_sees_entity_b_rules(seen, role):
    resp = _get(_ctx(role, (ENTITY_A,)))
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["overrides"]}
    assert ids == {"r-a", "r-ws"}
    assert "staff-2" not in resp.text
