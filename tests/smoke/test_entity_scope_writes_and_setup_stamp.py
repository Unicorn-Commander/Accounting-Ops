"""AO-MASS-3: scope on POST /coa/link and PUT /account-mappings; setup stamp.

* POST /coa/link and PUT /account-mappings took entity_id(s) from the body
  with only the accountant floor; the sibling COA routes enforce entity_scope
  (Audit 2026-05-31 Bug 1). Now a scoped member gets 403 before any session.
* PATCH /setup-progress persisted the caller's completed_at string verbatim.
  Now any non-empty value is replaced by the server's own timestamp.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import account_mappings as am
from app.api import journal as jr
from app.api import setup as su
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS_ID = uuid.UUID("51515151-5151-5151-5151-515151515151")
ENTITY_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
ENTITY_B = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000000b")


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
    def __init__(self, rows: list[tuple] | None = None, first: Any = None) -> None:
        self._rows = rows or []
        self._first = first

    def all(self):
        return self._rows

    def first(self):
        return self._first


class _Begin:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, seen: dict[str, Any]) -> None:
        self.seen = seen

    def begin(self) -> "_Begin":
        return _Begin()

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        sql = str(stmt)
        self.seen.setdefault("sql", []).append((sql, params))
        if "FROM entity" in sql:
            return _Result(rows=[(str(ENTITY_A),), (str(ENTITY_B),)])
        if "setup_progress FROM public.workspace" in sql:
            return _Result(first=type("R", (), {"setup_progress": None})())
        return _Result()


@pytest.fixture
def seen(monkeypatch):
    state: dict[str, Any] = {"sessions": 0, "links": 0}

    class _Ctx:
        async def __aenter__(self):
            state["sessions"] += 1
            return _Session(state)

        async def __aexit__(self, *exc):
            return False

    async def fake_link(session, **kw):
        state["links"] += 1
        return True

    monkeypatch.setattr(am, "workspace_session", lambda *a, **k: _Ctx())
    monkeypatch.setattr(jr, "workspace_session", lambda *a, **k: _Ctx())
    monkeypatch.setattr(jr, "link_external_account", fake_link)
    monkeypatch.setattr(su, "SessionLocal", lambda *a, **k: _Ctx())
    yield state
    app.dependency_overrides.pop(resolve_workspace, None)


def _client(ctx: WorkspaceContext) -> TestClient:
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    return TestClient(app)


def _link_body(entity: uuid.UUID) -> dict:
    return {"entity_id": str(entity), "account_number": "1010",
            "source": "simplefin", "external_account_id": "ACC-fixture-0001"}


def _mapping_body(*entities: uuid.UUID) -> dict:
    return {"mappings": [{"entity_id": str(e), "external_account_id": f"ACC-{i}"}
                         for i, e in enumerate(entities)]}


# --- POST /coa/link -------------------------------------------------------
def test_coa_link_refuses_out_of_scope_entity(seen):
    resp = _client(_ctx("accountant", (ENTITY_A,))).post(
        f"/api/workspaces/{WS_ID}/coa/link", json=_link_body(ENTITY_B))
    assert resp.status_code == 403
    assert seen["sessions"] == 0 and seen["links"] == 0


@pytest.mark.parametrize("ctx", [_ctx("accountant", (ENTITY_A,)), _ctx("owner")],
                         ids=["scoped_in_scope", "unscoped_owner"])
def test_coa_link_still_allowed_in_scope(seen, ctx):
    resp = _client(ctx).post(f"/api/workspaces/{WS_ID}/coa/link", json=_link_body(ENTITY_A))
    assert resp.status_code == 200
    assert seen["links"] == 1


@pytest.mark.parametrize("role", ["viewer", "client_user", "bookkeeper"])
def test_coa_link_floor_unchanged(seen, role):
    resp = _client(_ctx(role, (ENTITY_A,))).post(
        f"/api/workspaces/{WS_ID}/coa/link", json=_link_body(ENTITY_A))
    assert resp.status_code == 403


# --- PUT /account-mappings ------------------------------------------------
def test_mapping_put_refuses_batch_with_any_out_of_scope_entity(seen):
    resp = _client(_ctx("accountant", (ENTITY_A,))).put(
        f"/api/workspaces/{WS_ID}/account-mappings", json=_mapping_body(ENTITY_A, ENTITY_B))
    assert resp.status_code == 403
    assert seen["sessions"] == 0


@pytest.mark.parametrize("ctx", [_ctx("accountant", (ENTITY_A,)), _ctx("owner")],
                         ids=["scoped_in_scope", "unscoped_owner"])
def test_mapping_put_in_scope_still_upserts(seen, ctx):
    resp = _client(ctx).put(f"/api/workspaces/{WS_ID}/account-mappings",
                            json=_mapping_body(ENTITY_A))
    assert resp.status_code == 200
    assert resp.json() == {"upserted": 1}


@pytest.mark.parametrize("role", ["viewer", "client_user", "bookkeeper"])
def test_mapping_put_floor_unchanged(seen, role):
    resp = _client(_ctx(role, (ENTITY_A,))).put(
        f"/api/workspaces/{WS_ID}/account-mappings", json=_mapping_body(ENTITY_A))
    assert resp.status_code == 403


# --- PATCH /setup-progress completed_at ----------------------------------
def _persisted(seen) -> dict:
    for sql, params in seen["sql"]:
        if sql.startswith("UPDATE public.workspace"):
            return json.loads(params["p"])
    raise AssertionError("no UPDATE issued")


async def _patch_setup(completed_at: str) -> dict:
    # Called directly: the setup router carries a bind_http_caller dependency
    # that needs a real bearer, and the merge logic is what is under test.
    return await su.patch_setup_progress(
        WS_ID, su.SetupProgressPatch(completed_at=completed_at), workspace=_ctx("accountant"))


@pytest.mark.asyncio
async def test_setup_completed_at_is_server_stamped(seen):
    before = dt.datetime.now(dt.timezone.utc)
    result = await _patch_setup("1999-01-01T00:00:00+00:00")
    stamped = _persisted(seen)["completed_at"]
    assert stamped != "1999-01-01T00:00:00+00:00"
    assert dt.datetime.fromisoformat(stamped) >= before
    assert result["completed_at"] == stamped


@pytest.mark.asyncio
async def test_setup_completed_at_empty_string_clears(seen):
    await _patch_setup("")
    assert _persisted(seen)["completed_at"] is None
