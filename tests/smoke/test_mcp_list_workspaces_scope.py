"""AO114-R2-6: the MCP list_my_workspaces tool honours a token's scope.

The tool mirrors GET /api/me/workspaces but kept walking every active
membership with the raw role after AO-TOK-3 fixed the HTTP twin: a token
scoped to [A] at role_floor=viewer, or a Brigade JWT scoped to A, enumerated
the owner's other workspaces at the owner's full role over MCP. It now uses
the same tenancy.AggregateScope gate. Humans are unchanged.

No live stack: the registry SQL and the per-workspace session are faked, and
the MCP caller is set through the contextvar the MCP middleware uses.
"""
from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.mcp_auth as mcp_auth
from app.auth import AuthContext, ServiceScope
from app.tools import connectors

WS_A = str(uuid.uuid4())
WS_B = str(uuid.uuid4())
USER_ID = uuid.uuid4()
BRIGADE_ISS = "https://brigade.example.com"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return SimpleNamespace(c=1)


class _Session:
    def __init__(self, rows):
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
    common = {"is_default": False, "isolation_tier": "schema",
              "hipaa_protected": False, "last_active_at": None}
    return [
        {"id": WS_A, "slug": "alpha", "display_name": "Alpha", "role": "owner", **common},
        {"id": WS_B, "slug": "beta", "display_name": "Beta", "role": "owner", **common},
    ]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    from app.api import workspaces
    from app.platform import workspace_registry as wr

    async def _ensure(_uc, *, autoprovision=False):
        return USER_ID

    async def _resolve(workspace_id):
        return workspace_id

    @contextlib.asynccontextmanager
    async def _ws_session(_factory, _wid):
        yield _Session([])

    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", BRIGADE_ISS)
    monkeypatch.setattr(wr, "resolve_internal_workspace_id", _resolve)
    monkeypatch.setattr(workspaces, "_ensure_user_account", _ensure)
    monkeypatch.setattr(connectors, "SessionLocal", lambda: _Session(_rows()))
    monkeypatch.setattr(connectors, "workspace_session", _ws_session)


async def _list(auth: AuthContext) -> list[dict[str, Any]]:
    token = mcp_auth._current_auth.set(auth)
    try:
        return await connectors.list_my_workspaces()
    finally:
        mcp_auth._current_auth.reset(token)


def _service(scope: ServiceScope | None) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user", agent_id=None, scopes=("service",), claims={},
        service_scope=scope,
    )


async def test_scoped_token_lists_only_a_at_capped_role():
    out = await _list(_service(ServiceScope(
        workspace_ids=frozenset({uuid.UUID(WS_A)}), entity_ids=None, role_floor="viewer",
    )))
    assert [w["id"] for w in out] == [WS_A]
    assert out[0]["role"] == "viewer"
    assert out[0]["role_canonical"] == connectors.canonical_role("viewer")


async def test_brigade_jwt_lists_only_its_claim():
    auth = AuthContext(
        uc_uid="owner-user", agent_id=None, scopes=(),
        claims={"iss": BRIGADE_ISS, "sub": "jane@example.com", "workspace_id": WS_B},
    )
    out = await _list(auth)
    assert [w["id"] for w in out] == [WS_B]


async def test_human_unchanged():
    out = await _list(_service(None))
    assert [w["id"] for w in out] == [WS_A, WS_B]
    assert {w["role"] for w in out} == {"owner"}
