"""Invitation delivery gates: no database, SMTP, or real invitation writes."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import members
from app.tenancy import WorkspaceContext


def context(role="owner", **kw):
    return WorkspaceContext(workspace_id=uuid4(), workspace_slug="synthetic", role=role,
                            user_uc_uid="synthetic-owner", agent_id=None, entity_scope=None,
                            hipaa_protected=False, **kw)


class Session:
    def __init__(self, rows):
        self.rows = iter(rows)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    @asynccontextmanager
    async def begin(self):
        yield self

    async def execute(self, sql, params):
        self.calls.append((str(sql), params))
        if "set_config('app.current_workspace'" in str(sql):
            return SimpleNamespace(first=lambda: None)
        if "INSERT INTO public.membership_audit" in str(sql):
            return SimpleNamespace(first=lambda: None)
        row = next(self.rows)
        return SimpleNamespace(first=lambda: row)


def setup(monkeypatch, rows):
    session = Session(rows)
    monkeypatch.setattr(members, "SessionLocal", lambda: session)
    mail = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr(members, "send_email", mail)
    monkeypatch.setattr(members, "_accept_url", lambda token: "https://test.example/invite/accept?token=" + token)
    return session, mail


@pytest.mark.asyncio
async def test_manual_owner_hash_only_and_no_outbox(monkeypatch):
    ctx = context()
    session, mail = setup(monkeypatch, [None, SimpleNamespace(display_name="Synthetic"), None])
    before = datetime.now(timezone.utc)
    result = await members.create_invitation(ctx.workspace_id, members.InvitationCreate(
        email="member@example.test", role="viewer", delivery_mode="manual_link", ttl_days=2), ctx)
    mail.assert_not_awaited()
    raw = parse_qs(urlsplit(result["delivery"]["accept_url"]).query)["token"][0]
    insert = next(params for sql, params in session.calls if "workspace_invitation" in sql and "INSERT" in sql)
    assert insert["token_hash"] == members._hash_token(raw)
    assert raw not in repr(session.calls)
    assert insert["wid"] == str(ctx.workspace_id)
    assert before + timedelta(days=2) <= insert["expires_at"] <= datetime.now(timezone.utc) + timedelta(days=2)
    assert result["delivery"]["status"] == "manual"
    assert "/invite/accept?token=" in result["delivery"]["accept_url"]
    assert "/api/invitations/accept" not in result["delivery"]["accept_url"]
    assert not any("outbox" in sql.lower() for sql, _ in session.calls)
    audit_sql, audit = next(
        (sql, params) for sql, params in session.calls
        if "INSERT INTO public.membership_audit" in sql
    )
    assert "workspace_id, actor_uc_uid, action" in audit_sql
    assert audit["actor_uc_uid"] == ctx.user_uc_uid
    assert audit["action"] == "invitation_create"
    assert audit["invited_email"] == "member@example.test"
    assert raw not in repr(audit)


@pytest.mark.asyncio
async def test_default_email_still_sends(monkeypatch):
    ctx = context("admin")
    session, mail = setup(monkeypatch, [None, SimpleNamespace(display_name="Synthetic"), None,
                                      SimpleNamespace(email="owner@example.test")])
    body = members.InvitationCreate(email="member@example.test", role="viewer")
    assert body.delivery_mode == "email"
    result = await members.create_invitation(ctx.workspace_id, body, ctx)
    mail.assert_awaited_once()
    assert result["delivery"] == {"status": "sent"}
    assert "accept_url" not in result
    body_text = mail.await_args.kwargs["body_text"]
    body_html = mail.await_args.kwargs["body_html"]
    for delivered_body in (body_text, body_html):
        assert "/invite/accept?token=" in delivered_body
        assert "/api/invitations/accept" not in delivered_body
    assert mail.await_args.kwargs["related_invitation_id"]
    assert sum("INSERT INTO public.membership_audit" in sql for sql, _ in session.calls) == 1


def test_accept_url_uses_web_form_and_encodes_token(monkeypatch):
    monkeypatch.setattr(
        members,
        "get_settings",
        lambda: SimpleNamespace(public_base_url="https://test.example/"),
    )

    url = members._accept_url("token/with spaces")

    assert url == "https://test.example/invite/accept?token=token%2Fwith%20spaces"
    assert "/api/invitations/accept" not in url


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["viewer", "bookkeeper", "accountant", "admin"])
async def test_manual_nonowner_denied_before_db(monkeypatch, role):
    ctx = context(role)
    session, mail = setup(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        await members.create_invitation(ctx.workspace_id, members.InvitationCreate(
            email="member@example.test", role="viewer", delivery_mode="manual_link"), ctx)
    assert exc.value.status_code == 403
    assert session.calls == []
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_mismatch_denied_before_db(monkeypatch):
    ctx = context()
    session, mail = setup(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        await members.create_invitation(uuid4(), members.InvitationCreate(
            email="member@example.test", role="viewer", delivery_mode="manual_link"), ctx)
    assert exc.value.status_code == 403
    assert session.calls == []
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_manual_never_reissues_capability(monkeypatch):
    ctx = context()
    session, mail = setup(monkeypatch, [SimpleNamespace(id=str(uuid4()), expires_at=datetime.now(timezone.utc))])
    result = await members.create_invitation(ctx.workspace_id, members.InvitationCreate(
        email="member@example.test", role="viewer", delivery_mode="manual_link"), ctx)
    assert result["status"] == "already_pending"
    assert "accept_url" not in repr(result)
    assert len(session.calls) == 1
    mail.assert_not_awaited()


def test_delivery_mode_rejects_typos():
    with pytest.raises(ValidationError):
        members.InvitationCreate(email="member@example.test", role="viewer", delivery_mode="manul")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["expired", "accepted", "cancelled"])
async def test_redemption_resolved_or_expired_still_denied(monkeypatch, state):
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(cancelled_at=now if state == "cancelled" else None,
                          accepted_at=now if state == "accepted" else None,
                          expires_at=now - timedelta(seconds=1) if state == "expired" else now + timedelta(days=1))
    session, mail = setup(monkeypatch, [row])
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as exc:
        await members.accept_invitation(request=request, token="synthetic-capability")
    assert exc.value.status_code == 410
    assert len(session.calls) == 1
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_pat_without_management_scope_denied(monkeypatch):
    ctx = context(pat_scopes=())
    session, mail = setup(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        await members.create_invitation(ctx.workspace_id, members.InvitationCreate(
            email="member@example.test", role="viewer", delivery_mode="manual_link"), ctx)
    assert exc.value.status_code == 403
    assert session.calls == []
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_redemption_wrong_authenticated_email_still_denied(monkeypatch):
    from app import auth
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=SimpleNamespace(
        uc_uid="different-user",
        claims={"email": "wrong@example.test", "email_verified": True},
    )))
    inv = SimpleNamespace(cancelled_at=None, accepted_at=None,
                          expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                          email="invited@example.test")
    user = SimpleNamespace(id=uuid4(), email="wrong@example.test")
    session, mail = setup(monkeypatch, [inv, user])
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer synthetic-test")]})
    with pytest.raises(HTTPException) as exc:
        await members.accept_invitation(request=request, token="synthetic-capability")
    assert exc.value.status_code == 403
    assert len(session.calls) == 2
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_redemption_unverified_matching_email_denied_without_writes(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=SimpleNamespace(
        uc_uid="invited-user",
        claims={"email": "invited@example.test", "email_verified": False},
    )))
    inv = SimpleNamespace(cancelled_at=None, accepted_at=None,
                          expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                          email="invited@example.test")
    session, mail = setup(monkeypatch, [inv])
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer synthetic-test")]})

    with pytest.raises(HTTPException) as exc:
        await members.accept_invitation(request=request, token="synthetic-capability")

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "EMAIL_NOT_VERIFIED"
    assert "provider that verifies" in exc.value.detail["message"]
    assert len(session.calls) == 1
    assert not any("UPDATE public.workspace_invitation" in sql for sql, _ in session.calls)
    assert not any("INSERT INTO public.user_account" in sql for sql, _ in session.calls)
    mail.assert_not_awaited()


@pytest.mark.asyncio
async def test_redemption_verified_matching_email_accepted(monkeypatch):
    from app import auth

    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=SimpleNamespace(
        uc_uid="invited-user",
        claims={"email": "INVITED@example.test", "email_verified": True},
    )))
    inv = SimpleNamespace(
        id=str(uuid4()), workspace_id=uuid4(), workspace_name="Synthetic",
        workspace_slug="synthetic", role="viewer", entity_scope=None,
        cancelled_at=None, accepted_at=None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        email="invited@example.test",
    )
    user = SimpleNamespace(id=uuid4(), email="invited@example.test")
    session, mail = setup(monkeypatch, [inv, user, None, None, None])
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer synthetic-test")]})

    result = await members.accept_invitation(request=request, token="synthetic-capability")

    assert result["status"] == "accepted"
    assert any("UPDATE public.workspace_invitation" in sql for sql, _ in session.calls)
    mail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 404, 410])
async def test_post_accept_reuses_get_authorization_and_errors(monkeypatch, status):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    result = {"status": "accepted", "workspace_id": "synthetic"}
    handler = AsyncMock(return_value=result)
    if status != 200:
        handler.side_effect = HTTPException(status_code=status, detail="denied")
    monkeypatch.setattr(members, "accept_invitation", handler)
    app = FastAPI()
    app.include_router(members.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/invitations/accept", json={"token": "synthetic-capability"},
                                     headers={"Authorization": "Bearer synthetic-session"})
    assert response.status_code == status
    handler.assert_awaited_once()
    request = handler.await_args.kwargs["request"]
    assert request.headers["authorization"] == "Bearer synthetic-session"
    assert request.url.query == ""
    assert handler.await_args.kwargs["token"] == "synthetic-capability"
    assert "synthetic-capability" not in response.text
    assert "get" in app.openapi()["paths"]["/api/invitations/accept"]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": "short"}, {"token": "x" * 201}])
async def test_post_accept_rejects_invalid_body_before_redemption(monkeypatch, body):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    handler = AsyncMock()
    monkeypatch.setattr(members, "accept_invitation", handler)
    app = FastAPI()
    app.include_router(members.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/invitations/accept", json=body)
    assert response.status_code == 422
    handler.assert_not_awaited()
