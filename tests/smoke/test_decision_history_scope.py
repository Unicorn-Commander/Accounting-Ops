"""AO-CLIENT-3: GET open-items/decision-history honours entity_scope (no DB).

The route had the viewer floor but no entity_scope filter (the sibling
open-items list scopes), so a client_user scoped to entity A read entity B's
decision history: reviewer ids and internal resolution_notes. The fake
session applies the route's bound scope to a fixed row set.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import open_items as oi
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS_ID = uuid.UUID("41414141-4141-4141-4141-414141414141")
ENTITY_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
ENTITY_B = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000000b")
URL = f"/api/workspaces/{WS_ID}/open-items/decision-history"
KEY = "fixture-decision"

_WHEN = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
ROWS = [
    {"id": "d-a", "entity_id": str(ENTITY_A), "form_reference": f"DECISION_RESOLVED: {KEY}|accept",
     "notes": "note for A", "decided_by": "reviewer-a", "decided_at": _WHEN, "content": {}},
    {"id": "d-b", "entity_id": str(ENTITY_B), "form_reference": f"DECISION_RESOLVED: {KEY}|other",
     "notes": "internal note for B", "decided_by": "reviewer-b", "decided_at": _WHEN, "content": {}},
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
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        rows = ROWS
        if "ANY(:entity_scope)" in sql:
            allowed = set(params["entity_scope"])
            rows = [r for r in ROWS if r["entity_id"] in allowed]
        return _Result(rows)


class _Ctx:
    async def __aenter__(self):
        return _Session()

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch):
    monkeypatch.setattr(oi, "workspace_session", lambda *a, **k: _Ctx())
    yield
    app.dependency_overrides.pop(resolve_workspace, None)


def _get(ctx: WorkspaceContext):
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    return TestClient(app).get(URL, params={"key": KEY})


def test_client_user_scoped_to_a_never_sees_entity_b_history():
    resp = _get(_ctx("client_user", (ENTITY_A,)))
    assert resp.status_code == 200
    body = resp.json()
    assert [h["id"] for h in body["history"]] == ["d-a"]
    assert "reviewer-b" not in resp.text
    assert "internal note for B" not in resp.text


@pytest.mark.parametrize("role", ["viewer", "bookkeeper", "accountant"])
def test_scoped_members_see_only_their_entity(role):
    resp = _get(_ctx(role, (ENTITY_A,)))
    assert [h["id"] for h in resp.json()["history"]] == ["d-a"]


@pytest.mark.parametrize("role", ["viewer", "client_user", "bookkeeper", "accountant", "admin", "owner"])
def test_unscoped_members_keep_full_history(role):
    resp = _get(_ctx(role))
    assert resp.status_code == 200
    assert [h["id"] for h in resp.json()["history"]] == ["d-a", "d-b"]
