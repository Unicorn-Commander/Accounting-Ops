"""Smoke tests for /api/test/brigade-jwt-verify.

Confirms:
  - Endpoint is admin-only (non-admin caller -> 403).
  - With a valid Brigade JWT in the body, returns {ok: true, claims: {...}}.
  - With an invalid Brigade JWT, returns 400.
  - Without a bearer, returns 401 (the normal auth middleware kicks in first).
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

import app.api.test_brigade_jwt as endpoint
import app.auth as auth
import app.security.brigade_jwt as bj
from app.auth import AuthContext

from tests.smoke.test_brigade_jwt import (
    _ED_JWK,
    _ED_PRIV,
    _RS_JWK,
    _base_claims,
    _mint_ed25519,
)


@pytest.fixture
def app_under_test(monkeypatch):
    """Build a FastAPI app that mounts only the test_brigade_jwt router."""
    app = FastAPI()
    app.include_router(endpoint.router)
    return app


@pytest.fixture(autouse=True)
def _serve_brigade_jwks(monkeypatch):
    bj._reset_cache_for_tests()

    async def _jwks():
        return {"keys": [_ED_JWK, _RS_JWK]}

    monkeypatch.setattr(bj, "_fetch_jwks", _jwks)
    monkeypatch.setattr(bj, "_fetch_jwks_force_refresh", _jwks)
    yield
    bj._reset_cache_for_tests()


def _override_auth(app: FastAPI, ctx: AuthContext | None):
    """Stub validate_bearer_token to yield a specific AuthContext (or 401)."""
    from fastapi import HTTPException, status

    async def _fake():
        if ctx is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="no bearer")
        return ctx

    from app.auth import validate_bearer_token

    app.dependency_overrides[validate_bearer_token] = _fake


async def test_endpoint_requires_admin(app_under_test):
    """Non-admin caller -> 403, even with a valid Brigade JWT in body."""
    _override_auth(
        app_under_test,
        AuthContext(uc_uid="viewer-jane", agent_id=None, scopes=("openid",), claims={}),
    )
    token = _mint_ed25519(_ED_PRIV, _base_claims())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_under_test), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/test/brigade-jwt-verify",
            json={"token": token},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 403


async def test_endpoint_admin_scope_passes(app_under_test):
    """A caller with 'admin' scope can use it."""
    _override_auth(
        app_under_test,
        AuthContext(uc_uid="some-admin", agent_id=None, scopes=("admin",), claims={}),
    )
    token = _mint_ed25519(_ED_PRIV, _base_claims())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_under_test), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/test/brigade-jwt-verify",
            json={"token": token},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["claims"]["sub"] == "aaron"
    assert body["resolved_user"] == "aaron"


async def test_endpoint_uc_uid_allowlist(app_under_test, monkeypatch):
    """A uc_uid listed in ACCTOPS_ADMIN_UC_UIDS bypasses the scope check."""
    monkeypatch.setenv("ACCTOPS_ADMIN_UC_UIDS", "opsadmin,otheradmin")
    _override_auth(
        app_under_test,
        AuthContext(uc_uid="opsadmin", agent_id=None, scopes=("openid",), claims={}),
    )
    token = _mint_ed25519(_ED_PRIV, _base_claims())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_under_test), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/test/brigade-jwt-verify",
            json={"token": token},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 200


async def test_endpoint_invalid_brigade_jwt_returns_400(app_under_test):
    """A garbage JWT body -> 400 (not 500)."""
    _override_auth(
        app_under_test,
        AuthContext(uc_uid="aaron", agent_id=None, scopes=("admin",), claims={}),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_under_test), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/test/brigade-jwt-verify",
            json={"token": "not-a-jwt"},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 400


async def test_endpoint_wrong_audience_returns_400(app_under_test):
    """A Brigade-signed JWT for a different audience -> 400.

    Confirms the verifier's aud gate is in force here (replay prevention).
    """
    _override_auth(
        app_under_test,
        AuthContext(uc_uid="aaron", agent_id=None, scopes=("admin",), claims={}),
    )
    token = _mint_ed25519(_ED_PRIV, _base_claims(aud="other-app"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_under_test), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/test/brigade-jwt-verify",
            json={"token": token},
            headers={"Authorization": "Bearer x"},
        )
    assert r.status_code == 400


async def test_endpoint_missing_bearer_returns_401(app_under_test):
    """No bearer header -> the auth middleware 401s us before the endpoint runs."""
    _override_auth(app_under_test, None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_under_test), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/test/brigade-jwt-verify",
            json={"token": "anything"},
        )
    assert r.status_code == 401
