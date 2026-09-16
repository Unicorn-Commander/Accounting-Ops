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
        row = next(self.rows)
        return SimpleNamespace(first=lambda: row)


def setup(monkeypatch, rows):
    session = Session(rows)
    monkeypatch.setattr(members, "SessionLocal", lambda: session)
    mail = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr(members, "send_email", mail)
    monkeypatch.setattr(members, "_accept_url", lambda token: "https://test.example/accept?token=" + token)
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
    insert = session.calls[-1][1]
    assert insert["token_hash"] == members._hash_token(raw)
    assert raw not in repr(session.calls)
    assert insert["wid"] == str(ctx.workspace_id)
    assert before + timedelta(days=2) <= insert["expires_at"] <= datetime.now(timezone.utc) + timedelta(days=2)
    assert result["delivery"]["status"] == "manual"
    assert not any("outbox" in sql.lower() for sql, _ in session.calls)


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
    assert mail.await_args.kwargs["related_invitation_id"]


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
        uc_uid="different-user", claims={"email": "wrong@example.test"})))
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
