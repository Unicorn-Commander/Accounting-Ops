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
    return {
        "iss": "ops-center",
        "aud": "accounting-ops",
        "iat": _NOW,
        "exp": _NOW + 60,
        "jti": str(uuid4()),
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "other"},
        {"aud": "other"},
        {"aud": ["accounting-ops"]},
        {"exp": _NOW},
        {"exp": _NOW - 1},
        {"exp": _NOW + 61},
        {"iat": _NOW + 6},
        {"iat": None},
        {"exp": None},
        {"jti": None},
        {"jti": ""},
        {"jti": "x" * 256},
        {"iat": True},
        {"iat": str(_NOW)},
        {"nbf": _NOW + 1},
        {"iat": _NOW + 5, "exp": _NOW + 5},
    ],
)
def test_invalid_claims(change):
    with pytest.raises(HTTPException) as exc:
        tokens.verify_claims(
            jwt.encode(claims(**change), _SECRET, algorithm="HS256"),
            _SECRET,
            "accounting-ops",
            _NOW,
        )
    assert exc.value.status_code == 401


@pytest.mark.parametrize("missing", ["iss", "aud", "iat", "exp", "jti"])
def test_required_claims(missing):
    body = claims()
    del body[missing]
    with pytest.raises(HTTPException):
        tokens.verify_claims(
            jwt.encode(body, _SECRET, algorithm="HS256"), _SECRET, "accounting-ops", _NOW
        )


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_algorithm_is_fixed(algorithm):
    with pytest.raises(HTTPException):
        tokens.verify_claims(
            jwt.encode(claims(), _SECRET, algorithm=algorithm), _SECRET, "accounting-ops", _NOW
        )


def test_future_iat_boundary_and_strict_expiry():
    token = jwt.encode(claims(iat=_NOW + 5, exp=_NOW + 65), _SECRET, algorithm="HS256")
    assert tokens.verify_claims(token, _SECRET, "accounting-ops", _NOW)["iat"] == _NOW + 5
    with pytest.raises(HTTPException):
        tokens.verify_claims(token, _SECRET, "accounting-ops", _NOW + 65)


@pytest.mark.parametrize("enabled,secret", [("false", _SECRET), ("true", "short"), ("true", "")])
@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", ""),
        ("GET", "/00000000-0000-0000-0000-000000000001"),
        ("PATCH", "/00000000-0000-0000-0000-000000000001"),
        ("DELETE", "/00000000-0000-0000-0000-000000000001/members/alex@example.com"),
    ],
)
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://accounting.example.com"
    ) as client:
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
        org_id=str(uuid4()),
        slug="fictional-books",
        display_name="Fictional Books",
        plan_tier="fixture",
        entitlements=["accounting_ops_access"],
        owner=owner,
        timezone="UTC",
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
    auth = AuthContext(
        "alex", None, (), {"iss": "https://identity.example.com"}, may_autoprovision=True
    )
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


@pytest.mark.parametrize("replayed,count,expected", [(True, 1, 401), (False, 10000, 503)])
async def test_replay_store_fails_closed(monkeypatch, replayed, count, expected):
    class FakeSession:
        def __init__(self):
            self.statements = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def begin(self):
            return self

        async def execute(self, statement, params=None):
            self.statements.append(str(statement))
            return SimpleNamespace(
                first=lambda: (1,) if replayed else None, scalar_one=lambda: count
            )

    session = FakeSession()
    monkeypatch.setattr(tokens, "SessionLocal", lambda: session)
    with pytest.raises(HTTPException) as exc:
        await tokens.consume_jti("fixture-jti", _NOW + 60, _NOW)
    assert exc.value.status_code == expected
    assert not any("INSERT" in sql for sql in session.statements)


def test_short_secret_warning_is_once_and_does_not_expose_value(monkeypatch, caplog):
    tokens._warn_secret.cache_clear()
    monkeypatch.setenv("PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("PROVISIONING_JWT_SECRET", "fictional-short-value")
    assert not tokens.provisioning_available()
    assert not tokens.provisioning_available()
    assert caplog.text.count("PROVISIONING_JWT_SECRET") == 1
    assert "fictional-short-value" not in caplog.text


async def test_paused_upsert_guard_only_when_enabled(monkeypatch):
    statements = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def begin(self):
            return self

        async def execute(self, sql, params):
            statements.append(str(sql))

    monkeypatch.setattr(onboarding, "SessionLocal", FakeSession)
    monkeypatch.setenv("PROVISIONING_ENABLED", "true")
    await onboarding._upsert_workspace_membership(
        uuid4(), uuid4(), "owner", preserve_existing_role=False
    )
    assert "WHERE workspace_membership.status = 'active'" in statements[-1]
    monkeypatch.setenv("PROVISIONING_ENABLED", "false")
    await onboarding._upsert_workspace_membership(
        uuid4(), uuid4(), "owner", preserve_existing_role=False
    )
    assert "WHERE workspace_membership.status" not in statements[-1]


def test_wrong_signature_fails():
    token = jwt.encode(claims(), _SECRET + "different", algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        tokens.verify_claims(token, _SECRET, "accounting-ops", _NOW)
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "answer,stored,status,member_role,expected",
    [
        # The control plane decides who administers, whatever is stored here.
        ("owner", None, None, "", "owner"),
        (" Admin ", "viewer", "active", "", "admin"),
        ("owner", "accountant", "revoked", "", "owner"),
        # Not an administrator: a bookkeeping role an owner set here survives...
        ("member", "accountant", "active", "", "accountant"),
        ("manager", "cpa", "active", "", "cpa"),
        ("billing_admin", "bookkeeper", "active", "viewer", "bookkeeper"),
        ("unknown-role", "client_user", "active", "", "client_user"),
        # ...and an administrator the control plane no longer names goes to the least privilege.
        ("member", "owner", "active", "", "viewer"),
        ("member", "admin", "active", "accountant", "viewer"),
        ("member", "client_owner", "active", "", "viewer"),
        # No seat, or a removed one: nothing from the answer alone, or the role the deployment names.
        ("member", None, None, "", None),
        ("member", "accountant", "revoked", "", None),
        ("", None, None, "", None),
        ("member", None, None, "viewer", "viewer"),
        ("member", "accountant", "revoked", "viewer", "viewer"),
        ("manager", None, None, " Accountant ", "accountant"),
        # The setting names a bookkeeping role or nothing: never an administrator.
        ("member", None, None, "owner", None),
        ("member", None, None, "admin", None),
        ("member", None, None, "true", None),
    ],
)
def test_role_after_an_answer(monkeypatch, answer, stored, status, member_role, expected):
    monkeypatch.setenv("PROVISIONING_MEMBER_ROLE", member_role)
    assert provisioning.role_after(answer, stored, status) == expected


async def test_a_remembered_no_needs_neither_the_database_nor_the_control_plane(monkeypatch):
    from app import tenancy

    provisioning.forget_answers()
    monkeypatch.setattr(tenancy, "_ensure_user_account", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(
        provisioning, "SessionLocal", lambda: pytest.fail("a remembered answer opened a session")
    )
    fetch = AsyncMock(side_effect=AssertionError("a remembered answer asked again"))
    auth = AuthContext(
        "alex", None, (), {"iss": "https://identity.example.com"}, may_autoprovision=True
    )
    provisioning._remember("alex", "not_entitled", 60.0)
    result = await provisioning.ensure_jit_membership(
        auth, "fictional-bearer", SimpleNamespace(fetch=fetch)
    )
    assert result.state == "not_entitled"
    fetch.assert_not_called()
    provisioning.forget_answers()


@pytest.mark.parametrize(
    "auth,bearer",
    [
        (AuthContext("alex", None, (), {"iss": "https://identity.example.com"}), "fictional"),
        (AuthContext("alex", None, (), {"iss": "ao-local"}, may_autoprovision=True), "fictional"),
        (
            AuthContext(
                "alex", None, (), {"iss": "https://identity.example.com"}, may_autoprovision=True
            ),
            None,
        ),
    ],
)
async def test_a_caller_who_cannot_be_provisioned_is_never_looked_up(monkeypatch, auth, bearer):
    from app import tenancy

    monkeypatch.setattr(
        tenancy, "_ensure_user_account", AsyncMock(side_effect=AssertionError("looked up"))
    )
    fetch = AsyncMock(side_effect=AssertionError("asked the control plane"))
    result = await provisioning.ensure_jit_membership(auth, bearer, SimpleNamespace(fetch=fetch))
    assert result.state == "disabled"


def test_answers_are_remembered_within_a_bound(monkeypatch):
    provisioning.forget_answers()
    monkeypatch.setattr(provisioning, "_ANSWER_LIMIT", 4)
    clock = [1000.0]
    monkeypatch.setattr(provisioning, "_clock", lambda: clock[0])
    for index in range(4):
        provisioning._remember(f"person-{index}", "not_entitled", 60.0)
    # Full of answers that are still fresh: they all go, the new one stays.
    provisioning._remember("person-4", "not_entitled", 60.0)
    assert set(provisioning._answers) == {"person-4"}
    # Full of answers that have aged out: only those go.
    for index in range(5, 8):
        provisioning._remember(f"person-{index}", "not_entitled", 1.0)
    clock[0] += 5
    provisioning._remember("person-8", "not_entitled", 60.0)
    assert set(provisioning._answers) == {"person-4", "person-8"}
    provisioning.forget_answers()


@pytest.mark.parametrize(
    "payload",
    [
        # An organization with no owner row, and a plan tier that is null in the control plane's table.
        {"owner": {"subject": "", "email": ""}, "plan_tier": None},
        # Only what this app uses.
        {},
        # Fields the control plane may add later, and ones it sends in another shape.
        {"owner": {"subject": "alex-subject", "phone": "fictional"}, "region": "example", "slug": 7},
        # A long organization name: accepted here, cut where the workspace is named.
        {"display_name": "Fictional " * 25},
    ],
)
def test_the_payload_is_never_refused_over_a_field_this_app_does_not_use(payload):
    body = provisioning.WorkspaceCreate(
        **{"org_id": str(uuid4()), "display_name": "Fictional Books", **payload}
    )
    assert body.owner.subject == payload.get("owner", {}).get("subject", "")
    assert set(body.model_dump()) == {"org_id", "display_name", "owner"}


@pytest.mark.parametrize("payload", [{"org_id": ""}, {"display_name": ""}, {"display_name": None}])
def test_the_two_fields_this_app_needs_are_still_required(payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        provisioning.WorkspaceCreate(
            **{"org_id": str(uuid4()), "display_name": "Fictional Books", **payload}
        )
