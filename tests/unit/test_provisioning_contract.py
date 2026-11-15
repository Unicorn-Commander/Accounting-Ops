# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""Offline contract checks. Database/lock proofs live in the PostgreSQL suite."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi import FastAPI, HTTPException

from app.api import onboarding, provisioning
from app.auth import AuthContext
from app.security import provisioning_token as tokens

# Fictional signing material, generated in memory and never printed.
_SECRET = "fictional-provisioning-test-only-" * 2
_NOW = 1700000000


def claims(**changes):
    return {"iss": "ops-center", "aud": "accounting-ops", "iat": _NOW,
            "exp": _NOW + 60, "jti": str(uuid4()), **changes}


@pytest.mark.parametrize("change", [
    {"iss": "other"}, {"aud": "other"}, {"aud": ["accounting-ops"]},
    {"exp": _NOW}, {"exp": _NOW - 1}, {"exp": _NOW + 61},
    {"iat": _NOW + 6}, {"iat": None}, {"exp": None}, {"jti": None},
    {"jti": ""}, {"jti": "x" * 256}, {"iat": True}, {"iat": str(_NOW)},
    {"nbf": _NOW + 1}, {"iat": _NOW + 5, "exp": _NOW + 5},
])
def test_invalid_claims(change):
    with pytest.raises(HTTPException) as exc:
        tokens.verify_claims(jwt.encode(claims(**change), _SECRET, algorithm="HS256"),
                             _SECRET, "accounting-ops", _NOW)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("missing", ["iss", "aud", "iat", "exp", "jti"])
def test_required_claims(missing):
    body = claims()
    del body[missing]
    with pytest.raises(HTTPException):
        tokens.verify_claims(jwt.encode(body, _SECRET, algorithm="HS256"),
                             _SECRET, "accounting-ops", _NOW)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_algorithm_is_fixed(algorithm):
    with pytest.raises(HTTPException):
        tokens.verify_claims(jwt.encode(claims(), _SECRET, algorithm=algorithm),
                             _SECRET, "accounting-ops", _NOW)


def test_future_iat_boundary_and_strict_expiry():
    token = jwt.encode(claims(iat=_NOW + 5, exp=_NOW + 65), _SECRET, algorithm="HS256")
    assert tokens.verify_claims(token, _SECRET, "accounting-ops", _NOW)["iat"] == _NOW + 5
    with pytest.raises(HTTPException):
        tokens.verify_claims(token, _SECRET, "accounting-ops", _NOW + 65)


@pytest.mark.parametrize("enabled,secret", [("false", _SECRET), ("true", "short"),
                                           ("true", "")])
@pytest.mark.parametrize("method,path", [
    ("POST", ""), ("GET", "/00000000-0000-0000-0000-000000000001"),
    ("PATCH", "/00000000-0000-0000-0000-000000000001"),
    ("DELETE", "/00000000-0000-0000-0000-000000000001/members/alex@example.com"),
])
async def test_disabled_routes_no_queries(monkeypatch, enabled, secret, method, path):
    monkeypatch.setenv("PROVISIONING_ENABLED", enabled)
    monkeypatch.setenv("PROVISIONING_JWT_SECRET", secret)
    def forbidden():
        pytest.fail("disabled provisioning touched the database")
    monkeypatch.setattr(tokens, "SessionLocal", forbidden)
    monkeypatch.setattr(provisioning, "SessionLocal", forbidden)
    app = FastAPI()
    if tokens.provisioning_available():
        app.include_router(provisioning.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="https://accounting.example.com") as client:
        reply = await client.request(method, "/api/v1/provisioning/workspaces" + path)
    assert reply.status_code == 404
    assert reply.json() == {"detail": "Not Found"}


async def test_dependency_reads_clock_once_and_passes_snapshot(monkeypatch):
    from starlette.requests import Request
    monkeypatch.setenv("PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("PROVISIONING_JWT_SECRET", _SECRET)
    readings = []
    def now():
        readings.append(1)
        return _NOW
    monkeypatch.setattr(tokens.time, "time", now)
    consume = AsyncMock()
    monkeypatch.setattr(tokens, "consume_jti", consume)
    body = claims()
    token = jwt.encode(body, _SECRET, algorithm="HS256")
    request = Request({"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]})
    await tokens.require_provisioning_token(request)
    assert readings == [1]
    consume.assert_awaited_once_with(body["jti"], body["exp"], _NOW)


@pytest.mark.parametrize("email", [None, "", "ignored@example.com"])
def test_full_payload_ignores_owner_email(email):
    owner = {"subject": "alex-subject"}
    if email is not None:
        owner["email"] = email
    body = provisioning.WorkspaceCreate(
        org_id=str(uuid4()), slug="fictional-books", display_name="Fictional Books",
        plan_tier="fixture", entitlements=["accounting_ops_access"],
        owner=owner, timezone="UTC",
    )
    assert body.owner.model_dump() == {"subject": "alex-subject"}


@pytest.mark.parametrize("state", ["active", "suspended", "archived"])
def test_erasure_status_wins(state):
    row = SimpleNamespace(id=uuid4(), status="suspended", control_plane_state=state)
    assert provisioning.workspace_status(row)["status"] == "suspended"


async def test_flag_off_keeps_existing_workspace_early_return(monkeypatch):
    monkeypatch.delenv("PROVISIONING_ENABLED", raising=False)
    monkeypatch.setenv("AO_ENTITLED_PROVISIONING", "true")
    wid, uid = uuid4(), uuid4()
    monkeypatch.setattr(onboarding, "_ensure_user_account", AsyncMock(return_value=uid))
    monkeypatch.setattr(onboarding, "_first_active_workspace", AsyncMock(return_value={"id": wid}))
    fetch = AsyncMock(side_effect=AssertionError("flag off fetched new entitlement"))
    auth = AuthContext("alex", None, (), {"iss": "https://identity.example.com"},
                       may_autoprovision=True)
    result = await onboarding.ensure_entitled_workspace(
        auth, "fictional-bearer", entitlement_client=SimpleNamespace(fetch=fetch)
    )
    assert result.state == "existing" and result.workspace_id == wid
    fetch.assert_not_called()


async def test_flag_on_delegates_even_without_old_flag(monkeypatch):
    monkeypatch.setenv("PROVISIONING_ENABLED", "true")
    monkeypatch.delenv("AO_ENTITLED_PROVISIONING", raising=False)
    jit = AsyncMock(return_value=onboarding.EntitledProvisioningResult("existing"))
    monkeypatch.setattr(provisioning, "ensure_jit_membership", jit)
    auth = AuthContext("alex", None, (), {})
    await onboarding.ensure_entitled_workspace(auth, "fictional-bearer")
    jit.assert_awaited_once_with(auth, "fictional-bearer", None)


async def test_own_delete_flag_off_does_not_call_new_policy(monkeypatch):
    from app.api import members
    from app.security import member_removal
    from app.tenancy import WorkspaceContext
    monkeypatch.delenv("PROVISIONING_ENABLED", raising=False)
    policy = AsyncMock(side_effect=AssertionError("new policy invoked"))
    monkeypatch.setattr(member_removal, "remove_person", policy)
    ctx = WorkspaceContext(uuid4(), "fictional", "viewer", "alex", None, None, False)
    with pytest.raises(HTTPException) as exc:
        await members.remove_member(ctx.workspace_id, uuid4(), ctx)
    assert exc.value.status_code == 403
    policy.assert_not_called()
