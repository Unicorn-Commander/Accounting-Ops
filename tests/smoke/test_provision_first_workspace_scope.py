"""AO114-R3-3: provision-first-workspace stays inside the token's scope.

When the caller already had a workspace, the route returned the first
membership by display name over ALL of the user's workspaces. A token scoped
to workspace A (service_scope, PAT workspace_scope or a Brigade workspace
claim) was handed the id and slug of workspace B whenever B sorted first.
The existing-workspace lookup now passes every row through the shared
tenancy.AggregateScope; a restricted token that sees none of its user's
workspaces gets 403. Humans get the same first row as before.

No live stack: the registry SQL, the account lookup and the entitled path
are faked.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api import onboarding
from app.auth import AuthContext, ServiceScope

WS_A = str(uuid.uuid4())
WS_B = str(uuid.uuid4())
USER_ID = uuid.uuid4()
BRIGADE_ISS = "https://brigade.example.com"

# B sorts first by display name, exactly the disclosure shape.
ROWS = [
    {"id": WS_B, "slug": "alpha-books", "display_name": "Alpha Books", "role": "owner"},
    {"id": WS_A, "slug": "beta-books", "display_name": "Beta Books", "role": "viewer"},
]


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_a, **_kw):
        return _Result(self._rows)


@pytest.fixture
def rows(monkeypatch):
    current: list[dict[str, Any]] = list(ROWS)
    provisioned: list[str] = []

    async def _entitled(_auth, _bearer, *_a, **_kw):
        return onboarding.EntitledProvisioningResult("disabled")

    async def _ensure(_uc, *, autoprovision=False):
        return USER_ID

    async def _provision(**kwargs):
        provisioned.append(kwargs["display_name"])
        return "books", "new-slug"

    async def _backfill(*_a, **_kw):
        return None

    from app.platform import workspace_registry as wr

    async def _resolve(workspace_id):
        return workspace_id

    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", BRIGADE_ISS)
    monkeypatch.setattr(wr, "resolve_internal_workspace_id", _resolve)
    monkeypatch.setattr(onboarding, "ensure_entitled_workspace", _entitled)
    monkeypatch.setattr(onboarding, "_ensure_user_account", _ensure)
    monkeypatch.setattr(onboarding, "_backfill_user_profile", _backfill)
    monkeypatch.setattr(
        onboarding, "_provision_workspace_with_membership", _provision
    )
    monkeypatch.setattr(onboarding, "SessionLocal", lambda: _Session(current))
    return SimpleNamespace(current=current, provisioned=provisioned)


def _request():
    return SimpleNamespace(headers={"authorization": "Bearer fictional"})


def _body():
    return onboarding.ProvisionFirstWorkspaceRequest(display_name="Fictional Co")


def _scoped(*wids: str) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=("service",),
        claims={},
        service_scope=ServiceScope(workspace_ids=frozenset(uuid.UUID(w) for w in wids)),
    )


def _brigade(claim: str) -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=(),
        claims={"iss": BRIGADE_ISS, "sub": "jane@example.com", "workspace_id": claim},
    )


def _human() -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=(),
        claims={"iss": "https://identity.example.com"},
        may_autoprovision=True,
    )


async def _call(auth: AuthContext) -> dict[str, Any]:
    return await onboarding.provision_first_workspace(
        body=_body(), request=_request(), auth=auth
    )


async def test_scoped_token_gets_its_own_workspace_not_b(rows) -> None:
    out = await _call(_scoped(WS_A))
    assert out["workspace_id"] == WS_A
    assert out["slug"] == "beta-books"
    assert out["already_existed"] is True


async def test_brigade_jwt_gets_its_claim_not_b(rows) -> None:
    out = await _call(_brigade(WS_A))
    assert out["workspace_id"] == WS_A
    assert out["slug"] == "beta-books"


async def test_scoped_token_outside_every_seat_is_403(rows) -> None:
    with pytest.raises(HTTPException) as exc:
        await _call(_scoped(str(uuid.uuid4())))
    assert exc.value.status_code == 403
    assert WS_B not in str(exc.value.detail)
    assert rows.provisioned == []


async def test_human_unchanged(rows) -> None:
    out = await _call(_human())
    assert out["workspace_id"] == WS_B
    assert out["slug"] == "alpha-books"


async def test_human_first_workspace_still_provisions(rows) -> None:
    rows.current.clear()
    out = await _call(_human())
    assert out["already_existed"] is False
    assert rows.provisioned == ["Fictional Co"]
