"""AO-TOK-3: /api/me/work-inbox and /api/me/workspaces honour a token's scope.

Both aggregates walk every active membership of the caller instead of
resolving one workspace, so they never consulted ``service_scope``: a service
token / PAT declaring workspace_ids=[A] listed every workspace (and every open
item) of its owner, with the raw membership role. Each row now passes
``apply_service_scope``; a row outside the scope is skipped and the role floor
caps the role. Humans are unchanged.

No live stack: the membership SQL, the per-workspace session and the item
pulls are faked, so what is under test is the per-row gate.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api import work_inbox, workspaces
from app.auth import AuthContext, ServiceScope

WS_A = str(uuid.uuid4())
WS_B = str(uuid.uuid4())
ENT_A1 = str(uuid.uuid4())
USER_ID = uuid.uuid4()


def _auth(service_scope: ServiceScope | None = None) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=("service",),
        claims={},
        service_scope=service_scope,
    )


def _scoped_a(role_floor: str | None = None, entity_ids=None) -> AuthContext:
    return _auth(
        ServiceScope(
            workspace_ids=frozenset({uuid.UUID(WS_A)}),
            entity_ids=frozenset(uuid.UUID(e) for e in entity_ids) if entity_ids else None,
            role_floor=role_floor,
        )
    )


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return SimpleNamespace(c=3)


class _Session:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self

    async def execute(self, *_a, **_k):
        return _Result(self._rows)


def _membership_rows(**extra) -> list[dict[str, Any]]:
    # The owner is the OWNER of both A and B, no entity restriction.
    return [
        {"id": WS_A, "display_name": "Alpha Books", "role": "owner", "entity_scope": None, **extra},
        {"id": WS_B, "display_name": "Beta Books", "role": "owner", "entity_scope": None, **extra},
    ]


@contextlib.asynccontextmanager
async def _fake_ws_session(_factory, _workspace_id):
    yield _Session([])


# ---------------------------------------------------------------------------
# /api/me/work-inbox
# ---------------------------------------------------------------------------


@pytest.fixture
def inbox(monkeypatch):
    seen: list[tuple[str, list[str] | None]] = []

    async def _user_id_for(_uc):
        return USER_ID

    async def _approvals(ws, *, workspace_id, workspace_name, scope, now):
        seen.append((workspace_id, scope))
        return [
            {
                "id": f"appr-{workspace_id}",
                "workspace_id": workspace_id,
                "workspace_display_name": workspace_name,
                "entity_id": None,
                "item_type": "approval",
                "subject_type": "bank_transaction_import",
                "title": "x",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "age_hours": 1.0,
                "deep_link": f"/approvals?workspace={workspace_id}",
            }
        ]

    async def _tax(ws, **_k):
        return []

    monkeypatch.setattr(work_inbox, "_user_id_for", _user_id_for)
    monkeypatch.setattr(work_inbox, "SessionLocal", lambda: _Session(_membership_rows()))
    monkeypatch.setattr(work_inbox, "workspace_session", _fake_ws_session)
    monkeypatch.setattr(work_inbox, "_approvals_for_workspace", _approvals)
    monkeypatch.setattr(work_inbox, "_tax_reviews_for_workspace", _tax)
    return seen


async def _inbox(auth: AuthContext) -> dict[str, Any]:
    return await work_inbox.work_inbox(max_total=100, max_per_workspace=50, auth=auth)


def _inbox_workspaces(body: dict[str, Any]) -> set[str]:
    return {i["workspace_id"] for i in body["items"]}


async def test_work_inbox_scoped_token_lists_only_a(inbox):
    body = await _inbox(_scoped_a())
    assert _inbox_workspaces(body) == {WS_A}
    assert [w for w, _ in inbox] == [WS_A]


async def test_work_inbox_role_floor_below_actor_floor_lists_nothing(inbox):
    body = await _inbox(_scoped_a(role_floor="viewer"))
    assert body["items"] == []
    assert inbox == []


async def test_work_inbox_token_entity_scope_narrows_pull(inbox):
    await _inbox(_scoped_a(entity_ids=[ENT_A1]))
    assert inbox == [(WS_A, [ENT_A1])]


async def test_work_inbox_human_unchanged(inbox):
    body = await _inbox(_auth(None))
    assert _inbox_workspaces(body) == {WS_A, WS_B}
    assert sorted(inbox) == sorted([(WS_A, None), (WS_B, None)])


# ---------------------------------------------------------------------------
# /api/me/workspaces
# ---------------------------------------------------------------------------


@pytest.fixture
def my_workspaces(monkeypatch):
    async def _entitled(_auth, _bearer):
        return SimpleNamespace(state="ok", detail=None)

    async def _ensure(_uc, *, autoprovision=False):
        return USER_ID

    rows = [
        {
            "id": r["id"],
            "slug": r["display_name"].lower().replace(" ", "-"),
            "display_name": r["display_name"],
            "role": r["role"],
            "is_default": r["id"] == WS_B,
            "isolation_tier": "schema",
            "hipaa_protected": False,
            "postgres_schema": "ws_x",
            "last_active_at": None,
        }
        for r in _membership_rows()
    ]
    monkeypatch.setattr(workspaces, "ensure_entitled_workspace", _entitled)
    monkeypatch.setattr(workspaces, "_ensure_user_account", _ensure)
    monkeypatch.setattr(workspaces, "SessionLocal", lambda: _Session(rows))
    monkeypatch.setattr(workspaces, "workspace_session", _fake_ws_session)


def _request() -> Any:
    return SimpleNamespace(headers={"authorization": "Bearer fixture"})


async def test_my_workspaces_scoped_token_lists_only_a(my_workspaces):
    out = await workspaces.list_my_workspaces(_request(), auth=_scoped_a(role_floor="viewer"))
    assert [w["id"] for w in out] == [WS_A]
    assert out[0]["role"] == "viewer"
    assert out[0]["role_canonical"] == workspaces.canonical_role("viewer")


async def test_my_workspaces_human_unchanged(my_workspaces):
    out = await workspaces.list_my_workspaces(_request(), auth=_auth(None))
    assert sorted(w["id"] for w in out) == sorted([WS_A, WS_B])
    assert {w["role"] for w in out} == {"owner"}
