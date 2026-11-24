"""AO111-R2-F8: entity_scope on coa/seed and the open-items marker writes.

* POST /coa/seed seeded every active entity of the workspace; a member with a
  restricted entity_scope now seeds only its own entities.
* POST /open-items/document-received and /open-items/decision filed their
  marker row under the first entity of the WORKSPACE by display name, which
  for a scoped member could be another entity. The pick is now restricted to
  the member's entity_scope.

The fake session applies the bound entity_scope to a fixed entity list, so
the assertions are on which entity is seeded or written, not on SQL text.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import journal as jr
from app.api import open_items as oi
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS_ID = uuid.UUID("52525252-5252-5252-5252-525252525252")
# Display-name order puts B ("Alpha Books") before A ("Zulu Books"), so the
# unrestricted pick lands on B: exactly the case a scoped A member must avoid.
ENTITY_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000005a")
ENTITY_B = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000005b")
ENTITIES = [
    {"id": str(ENTITY_B), "display_name": "Alpha Books"},
    {"id": str(ENTITY_A), "display_name": "Zulu Books"},
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

    def first(self) -> Any:
        return (self._rows[0]["id"],) if self._rows else None


class _Session:
    def __init__(self, seen: dict[str, Any]) -> None:
        self.seen = seen

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        params = params or {}
        if "FROM entity" in sql:
            rows = ENTITIES
            if "ANY(:entity_scope)" in sql:
                allowed = set(params["entity_scope"])
                rows = [e for e in ENTITIES if e["id"] in allowed]
            return _Result(rows)
        if "INSERT INTO tax_review_queue" in sql:
            self.seen["markers"].append(params["eid"])
            return _Result([])
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


@pytest.fixture
def seen(monkeypatch):
    st: dict[str, Any] = {"markers": [], "seeded": []}

    class _Ctx:
        async def __aenter__(self):
            return _Session(st)

        async def __aexit__(self, *exc):
            return False

    async def fake_seed(session: Any, *, workspace_id: str, entity_id: str) -> int:
        st["seeded"].append(entity_id)
        return 1

    monkeypatch.setattr(jr, "workspace_session", lambda *a, **k: _Ctx())
    monkeypatch.setattr(oi, "workspace_session", lambda *a, **k: _Ctx())
    monkeypatch.setattr(jr, "seed_coa_for_entity", fake_seed)
    yield st
    app.dependency_overrides.pop(resolve_workspace, None)


def _client(ctx: WorkspaceContext) -> TestClient:
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    return TestClient(app)


BASE = f"/api/workspaces/{WS_ID}"
SCOPED_A = _ctx("accountant", (ENTITY_A,))


def test_scoped_seed_only_touches_own_entity(seen):
    resp = _client(SCOPED_A).post(f"{BASE}/coa/seed")
    assert resp.status_code == 200
    assert seen["seeded"] == [str(ENTITY_A)]
    assert [e["entity_id"] for e in resp.json()["entities"]] == [str(ENTITY_A)]


@pytest.mark.parametrize("role", ["accountant", "owner"])
def test_unscoped_seed_still_covers_every_entity(seen, role):
    assert _client(_ctx(role)).post(f"{BASE}/coa/seed").status_code == 200
    assert sorted(seen["seeded"]) == sorted([str(ENTITY_A), str(ENTITY_B)])


MARKER_CALLS = [
    pytest.param("open-items/document-received", {"key": "w2-2026"}, id="document_received"),
    pytest.param("open-items/decision", {"key": "s-corp", "verdict": "accept"}, id="decision"),
]


@pytest.mark.parametrize("path,body", MARKER_CALLS)
def test_scoped_marker_is_filed_under_own_entity(seen, path, body):
    resp = _client(SCOPED_A).post(f"{BASE}/{path}", json=body)
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert seen["markers"] == [str(ENTITY_A)]


@pytest.mark.parametrize("path,body", MARKER_CALLS)
def test_scoped_member_with_no_visible_entity_writes_nothing(seen, path, body):
    ctx = _ctx("accountant", (uuid.UUID("cccccccc-0000-0000-0000-00000000005c"),))
    resp = _client(ctx).post(f"{BASE}/{path}", json=body)
    assert resp.json()["ok"] is False
    assert seen["markers"] == []


@pytest.mark.parametrize("path,body", MARKER_CALLS)
def test_unscoped_marker_keeps_first_entity_pick(seen, path, body):
    assert _client(_ctx("owner")).post(f"{BASE}/{path}", json=body).status_code == 200
    assert seen["markers"] == [str(ENTITY_B)]


@pytest.mark.parametrize("role,scope", [("bookkeeper", None), ("viewer", None), ("client_user", (ENTITY_A,))])
def test_below_accountant_is_refused(seen, role, scope):
    client = _client(_ctx(role, scope))
    assert client.post(f"{BASE}/coa/seed").status_code == 403
    for path, body in (("open-items/document-received", {"key": "k"}),
                       ("open-items/decision", {"key": "k", "verdict": "accept"})):
        assert client.post(f"{BASE}/{path}", json=body).status_code == 403
    assert seen["markers"] == [] and seen["seeded"] == []
