"""AO-CLIENT-1: GET account-mappings has the accountant floor (no DB).

Before the fix the GET depended only on ``resolve_workspace``: any member
(viewer, a client_user scoped to entity A) got every entity's bank mappings
and the full entity list, and the call dialled the workspace's SimpleFIN
access URL. The fix gates it at ``accountant`` like the PUT, BEFORE the
credential read, so a refused caller never triggers discovery.

The credential read, the discovery call and the workspace session are all
faked; a refused role must reach none of them.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import account_mappings as am
from app.main import app
from app.tenancy import WorkspaceContext, resolve_workspace

WS_ID = uuid.UUID("21212121-2121-2121-2121-212121212121")
ENTITY_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
URL = f"/api/workspaces/{WS_ID}/account-mappings"


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
    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict[str, Any]]:
        return []


class _Session:
    async def execute(self, *a: Any, **k: Any) -> _Result:
        return _Result()


class _SessionCtx:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture
def calls(monkeypatch):
    seen: dict[str, int] = {"credential": 0, "discover": 0, "session": 0}

    async def fake_access_url(workspace):
        seen["credential"] += 1
        return "https://fixture:secret@bridge.example.com/simplefin"

    async def fake_discover(access_url):
        seen["discover"] += 1
        return []

    def fake_session(*a, **k):
        seen["session"] += 1
        return _SessionCtx()

    monkeypatch.setattr(am, "_simplefin_access_url", fake_access_url)
    monkeypatch.setattr(am, "_discover_simplefin_accounts", fake_discover)
    monkeypatch.setattr(am, "workspace_session", fake_session)
    yield seen
    app.dependency_overrides.pop(resolve_workspace, None)


def _get(ctx: WorkspaceContext):
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    return TestClient(app).get(URL)


@pytest.mark.parametrize(
    "ctx",
    [
        _ctx("viewer"),
        _ctx("client_user", (ENTITY_A,)),
        _ctx("bookkeeper"),
    ],
    ids=["viewer", "client_user_scoped_A", "bookkeeper"],
)
def test_below_accountant_is_refused_before_any_simplefin_call(calls, ctx):
    resp = _get(ctx)
    assert resp.status_code == 403
    # No credential read, no live discovery, no mapping/entity query.
    assert calls == {"credential": 0, "discover": 0, "session": 0}


@pytest.mark.parametrize("role", ["accountant", "cpa", "admin", "owner"])
def test_accountant_and_above_still_read(calls, role):
    resp = _get(_ctx(role))
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"live_accounts", "mappings", "entities", "unmapped_count"}
    assert calls["credential"] == 1 and calls["discover"] == 1


def test_pat_with_read_scope_only_is_refused(calls):
    ctx = WorkspaceContext(
        workspace_id=WS_ID,
        workspace_slug="fixture-books",
        role="owner",
        user_uc_uid="fixture-user",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
        pat_scopes=("accounting:read",),
    )
    assert _get(ctx).status_code == 403
    assert calls["credential"] == 0
