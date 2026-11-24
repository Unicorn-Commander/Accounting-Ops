"""AO114-R3-1: only an interactive human session may manage PATs.

``_require_keycloak`` refused only PAT callers and ``scopes == ('service',)``.
A Brigade federation JWT pinned to workspace A carries neither, so it could
list every workspace of its mapped user on ``_meta`` and mint an owner-wide
``accounting:admin`` PAT, escaping its pin. The same held for an agent
acting on a user's behalf, a trusted M2M caller and a UC API key. Every such
bearer now gets 403 on all five PAT routes; a Keycloak or CE-local human
session is unchanged.

No live stack: every DB touch is stubbed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import personal_access_tokens as pats
from app.auth import AuthContext, ServiceScope, validate_bearer_token
from app.security.pat import MintedPAT

OWNER_ID = uuid.uuid4()
WS_A = str(uuid.uuid4())
BRIGADE_ISS = "https://brigade.example.com"
HUMAN_MSG = "only an interactive user session"


def _human() -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=(),
        claims={"iss": "https://identity.example.com", "sub": "owner-user"},
        may_autoprovision=True,
    )


def _brigade() -> AuthContext:
    # The Brigade path never sets may_autoprovision (app.auth._try_brigade).
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=("accounting:read",),
        claims={"iss": BRIGADE_ISS, "sub": "jane@example.com", "workspace_id": WS_A},
    )


def _brigade_with_autoprovision() -> AuthContext:
    # Defence in depth: the issuer alone refuses it.
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=(),
        claims={"iss": BRIGADE_ISS, "sub": "jane@example.com", "workspace_id": WS_A},
        may_autoprovision=True,
    )


def _agent() -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id="fixture-agent",
        scopes=(),
        claims={"iss": "https://identity.example.com", "act": {"sub": "fixture-agent"}},
        may_autoprovision=True,
    )


def _m2m() -> AuthContext:
    return AuthContext(
        uc_uid="m2m:fixture-client",
        agent_id=None,
        scopes=(),
        claims={"iss": "https://identity.example.com", "azp": "fixture-client"},
        is_trusted_m2m=True,
        may_autoprovision=True,
    )


def _uc_api_key() -> AuthContext:
    # The UC API key path leaves may_autoprovision False (app.auth).
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=("mcp:accounting-ops",),
        claims={"sub": "owner-user", "uc_uid": "owner-user"},
    )


def _scoped_session() -> AuthContext:
    return AuthContext(
        uc_uid="owner-user",
        agent_id=None,
        scopes=(),
        claims={"iss": "https://identity.example.com"},
        service_scope=ServiceScope(workspace_ids=frozenset({uuid.UUID(WS_A)})),
        may_autoprovision=True,
    )


REFUSED = {
    "brigade": _brigade,
    "brigade_autoprovision": _brigade_with_autoprovision,
    "agent": _agent,
    "m2m": _m2m,
    "uc_api_key": _uc_api_key,
    "service_scope": _scoped_session,
}


class _Result:
    def all(self):
        return [{"id": WS_A, "slug": "alpha", "display_name": "Alpha", "role": "owner"}]

    def mappings(self):
        return self

    def first(self):
        return None

    @property
    def rowcount(self):
        return 1


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self

    async def execute(self, *_a, **_kw):
        return _Result()


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch):
    minted: list[dict[str, object]] = []

    async def _ensure(uc_uid: str) -> uuid.UUID:
        return OWNER_ID

    async def _max_scopes(owner_user_id: uuid.UUID) -> set[str]:
        return {"accounting:read", "accounting:write", "accounting:admin"}

    async def _count(session, owner_user_id) -> int:
        return 0

    async def _create(session, **kwargs) -> MintedPAT:
        minted.append(kwargs)
        return MintedPAT(
            id=uuid.uuid4(),
            name=kwargs["name"],
            secret="aops_pat_" + "x" * 43,
            prefix="aops_pat_xxx",
            scopes=kwargs["scopes"],
            workspace_scope=kwargs["workspace_scope"],
            expires_at=kwargs["expires_at"],
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(pats, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(pats, "_ensure_user_account", _ensure)
    monkeypatch.setattr(pats, "_owner_max_scopes", _max_scopes)
    monkeypatch.setattr(pats, "count_active_pats", _count)
    monkeypatch.setattr(pats, "create_pat", _create)
    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", BRIGADE_ISS)

    def _make(auth_factory):
        app = FastAPI()
        app.include_router(pats.router)
        app.dependency_overrides[validate_bearer_token] = auth_factory
        c = TestClient(app, raise_server_exceptions=False)
        c.minted = minted  # type: ignore[attr-defined]
        return c

    return _make


MINT_BODY = {"name": "escape", "scopes": ["accounting:admin"], "ttl_days": 90}


@pytest.mark.parametrize("kind", sorted(REFUSED))
def test_require_keycloak_refuses_non_human(kind: str, monkeypatch) -> None:
    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", BRIGADE_ISS)
    with pytest.raises(pats.HTTPException) as exc:
        pats._require_keycloak(REFUSED[kind]())
    assert exc.value.status_code == 403
    assert HUMAN_MSG in str(exc.value.detail).lower()


@pytest.mark.parametrize("kind", ["brigade", "agent", "uc_api_key"])
def test_non_human_cannot_mint_or_list(kind: str, make_client) -> None:
    c = make_client(REFUSED[kind])
    pat_id = uuid.uuid4()
    calls = [
        c.post("/api/me/personal-access-tokens", json=MINT_BODY),
        c.get("/api/me/personal-access-tokens/_meta"),
        c.get("/api/me/personal-access-tokens"),
        c.post(f"/api/me/personal-access-tokens/{pat_id}/revoke"),
        c.delete(f"/api/me/personal-access-tokens/{pat_id}"),
    ]
    assert [r.status_code for r in calls] == [403] * 5, [r.text for r in calls]
    assert c.minted == []  # type: ignore[attr-defined]


def test_human_session_still_mints(make_client) -> None:
    c = make_client(_human)
    r = c.post("/api/me/personal-access-tokens", json=MINT_BODY)
    assert r.status_code == 201, r.text
    assert len(c.minted) == 1  # type: ignore[attr-defined]
    meta = c.get("/api/me/personal-access-tokens/_meta")
    assert meta.status_code == 200, meta.text


def test_pat_and_service_token_messages_unchanged() -> None:
    pat = AuthContext("owner-user", None, ("accounting:read",), {"pat_id": "x"})
    svc = AuthContext("owner-user", None, ("service",), {})
    with pytest.raises(pats.HTTPException) as e1:
        pats._require_keycloak(pat)
    with pytest.raises(pats.HTTPException) as e2:
        pats._require_keycloak(svc)
    assert "personal access tokens cannot" in str(e1.value.detail).lower()
    assert "service tokens cannot" in str(e2.value.detail).lower()
