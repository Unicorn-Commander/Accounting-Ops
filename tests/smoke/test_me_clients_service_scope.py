"""AO114-R2-2..5: the /api/me/clients* aggregates honour a token's scope.

GET /api/me/clients, /api/me/cpa-recent-activity, /api/me/clients/triage and
/api/me/persona walked every active membership of the caller and never
consulted ``service_scope`` or the Brigade workspace claim: a token declaring
workspace_ids=[A] (or a Brigade JWT scoped to A) listed the owner's other
workspaces, their roles and counts, and read their decision_audit rows. Each
membership row now passes tenancy.AggregateScope. Humans are unchanged.

No live stack: the registry SQL and the per-workspace sessions are faked.
"""
from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api import clients
from app.auth import AuthContext, ServiceScope

WS_A = str(uuid.uuid4())
WS_B = str(uuid.uuid4())
USER_ID = uuid.uuid4()
BRIGADE_ISS = "https://brigade.example.com"


def _auth(service_scope: ServiceScope | None = None) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=("service",),
        claims={},
        service_scope=service_scope,
    )


def _scoped_a(role_floor: str | None = None) -> AuthContext:
    return _auth(
        ServiceScope(
            workspace_ids=frozenset({uuid.UUID(WS_A)}),
            entity_ids=None,
            role_floor=role_floor,
        )
    )


def _brigade_a() -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=(),
        claims={"iss": BRIGADE_ISS, "sub": "jane@example.com", "workspace_id": WS_A},
    )


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        # count(*) queries; also the onboarded count (both workspaces).
        return SimpleNamespace(c=2, email="jane@example.com")


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


def _rows() -> list[dict[str, Any]]:
    # The owner holds a cpa seat in A and is platform_admin in B; it
    # onboarded both.
    common = {
        "isolation_tier": "schema",
        "hipaa_protected": False,
        "entity_scope": None,
        "last_active_at": None,
        "onboarded_by_me": True,
        "postgres_schema": "ws_x",
    }
    return [
        {"id": WS_A, "name": "Alpha", "slug": "alpha", "display_name": "Alpha",
         "role": "cpa", **common},
        {"id": WS_B, "name": "Beta", "slug": "beta", "display_name": "Beta",
         "role": "platform_admin", **common},
    ]


@pytest.fixture
def opened(monkeypatch):
    """Records every workspace whose schema the handler opened."""
    seen: list[str] = []

    @contextlib.asynccontextmanager
    async def _ws_session(_factory, workspace_id):
        seen.append(str(workspace_id))
        audit = [{"id": f"a-{workspace_id}", "tool": "post_journal_entry",
                  "outcome": "ok", "model_version": None, "created_at": "2026-11-01"}]
        yield _Session(audit)

    async def _user_id_for(_uc):
        return USER_ID

    from app.platform import workspace_registry as wr

    async def _resolve(workspace_id):
        return workspace_id

    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", BRIGADE_ISS)
    monkeypatch.setattr(wr, "resolve_internal_workspace_id", _resolve)
    monkeypatch.setattr(clients, "_user_id_for", _user_id_for)
    monkeypatch.setattr(clients, "SessionLocal", lambda: _Session(_rows()))
    monkeypatch.setattr(clients, "workspace_session", _ws_session)
    return seen


# ---------------------------------------------------------------------------
# AO114-R2-2: GET /api/me/clients
# ---------------------------------------------------------------------------


async def test_my_clients_scoped_token_lists_only_a(opened):
    body = await clients.list_my_clients(auth=_scoped_a(role_floor="viewer"))
    assert [w["id"] for w in body["workspaces"]] == [WS_A]
    assert body["workspaces"][0]["role"] == "viewer"
    assert opened == [WS_A]
    # B's platform_admin seat must not leak through the persona either.
    assert body["persona"] == "cpa"


async def test_my_clients_brigade_jwt_lists_only_its_claim(opened):
    body = await clients.list_my_clients(auth=_brigade_a())
    assert [w["id"] for w in body["workspaces"]] == [WS_A]
    assert opened == [WS_A]


async def test_my_clients_human_unchanged(opened):
    body = await clients.list_my_clients(auth=_auth(None))
    assert sorted(w["id"] for w in body["workspaces"]) == sorted([WS_A, WS_B])
    assert body["persona"] == "platform_admin"


# ---------------------------------------------------------------------------
# AO114-R2-3: GET /api/me/cpa-recent-activity
# ---------------------------------------------------------------------------


async def test_recent_activity_scoped_token_reads_only_a(opened):
    body = await clients.cpa_recent_activity(days=30, auth=_scoped_a())
    assert {i["workspace_id"] for i in body["items"]} == {WS_A}
    assert opened == [WS_A]


async def test_recent_activity_brigade_jwt_reads_only_its_claim(opened):
    body = await clients.cpa_recent_activity(days=30, auth=_brigade_a())
    assert {i["workspace_id"] for i in body["items"]} == {WS_A}
    assert opened == [WS_A]


async def test_recent_activity_human_unchanged(opened):
    body = await clients.cpa_recent_activity(days=30, auth=_auth(None))
    assert {i["workspace_id"] for i in body["items"]} == {WS_A, WS_B}


# ---------------------------------------------------------------------------
# AO114-R2-4: GET /api/me/clients/triage
# ---------------------------------------------------------------------------


async def test_triage_scoped_token_lists_only_a(opened):
    body = await clients.list_my_clients_triage(auth=_scoped_a(role_floor="viewer"))
    assert [w["id"] for w in body["workspaces"]] == [WS_A]
    assert body["workspaces"][0]["role"] == "viewer"
    assert opened == [WS_A]
    assert body["persona"] == "cpa"


async def test_triage_brigade_jwt_lists_only_its_claim(opened):
    body = await clients.list_my_clients_triage(auth=_brigade_a())
    assert [w["id"] for w in body["workspaces"]] == [WS_A]


async def test_triage_human_unchanged(opened):
    body = await clients.list_my_clients_triage(auth=_auth(None))
    assert sorted(w["id"] for w in body["workspaces"]) == sorted([WS_A, WS_B])
    assert body["persona"] == "platform_admin"


# ---------------------------------------------------------------------------
# AO114-R2-5: GET /api/me/persona (_caller_account, also the add-client gate)
# ---------------------------------------------------------------------------


@pytest.fixture
def persona(opened, monkeypatch):
    from app.api import workspaces

    async def _ensure(_uc, *, autoprovision=False):
        return USER_ID

    monkeypatch.setattr(workspaces, "_ensure_user_account", _ensure)


async def test_persona_scoped_token_sees_only_capped_roles_in_a(persona):
    body = await clients.my_persona(auth=_scoped_a(role_floor="viewer"))
    assert body["roles"] == ["viewer"]
    assert body["persona"] == "cpa"  # onboarded A, the only kept row
    assert body["clients_onboarded"] == 1
    assert body["can_add_clients"] is False


async def test_persona_brigade_jwt_sees_only_its_claim(persona):
    body = await clients.my_persona(auth=_brigade_a())
    assert body["roles"] == ["cpa"]
    assert body["persona"] == "cpa"


async def test_persona_human_unchanged(persona):
    body = await clients.my_persona(auth=_auth(None))
    assert body["roles"] == ["cpa", "platform_admin"]
    assert body["persona"] == "platform_admin"
    assert body["clients_onboarded"] == 2
