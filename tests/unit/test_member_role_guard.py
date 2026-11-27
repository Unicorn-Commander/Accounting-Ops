"""AO-ROLE-1: an admin cannot demote or re-scope an owner or another admin."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api import members
from app.api.members import MemberPatch
from app.tenancy import WorkspaceContext


def _ctx(role: str) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=uuid4(), workspace_slug="fictional", role=role,
        user_uc_uid="caller-uid", agent_id=None, entity_scope=None, hipaa_protected=False,
    )


def _install(monkeypatch, *, target_role: str, owners: int = 2):
    writes: list[str] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def begin(self):
            return self

        async def execute(self, statement, params=None):
            sql = str(statement).strip()
            if "m.role::text AS old_role" in sql:
                row = SimpleNamespace(
                    membership_id="m1", old_role=target_role,
                    old_entity_scope=None, uc_uid="target-uid",
                )
                return SimpleNamespace(first=lambda: row)
            if sql.startswith("SELECT count(*)"):
                return SimpleNamespace(first=lambda: SimpleNamespace(c=owners))
            writes.append(sql)
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(members, "SessionLocal", Session)
    audit = []

    async def fake_audit(*_a, **kw):
        audit.append(kw)

    monkeypatch.setattr(members, "_write_membership_audit", fake_audit)
    return writes


@pytest.mark.parametrize("target", ["owner", "client_owner", "admin"])
@pytest.mark.parametrize(
    "patch",
    [MemberPatch(role="viewer"), MemberPatch(entity_scope=[])],
)
async def test_admin_cannot_touch_elevated_target(monkeypatch, target, patch):
    writes = _install(monkeypatch, target_role=target, owners=2)
    with pytest.raises(HTTPException) as exc:
        await members.update_member(uuid4(), uuid4(), patch, _ctx("admin"))
    assert exc.value.status_code == 403
    assert writes == []


async def test_admin_can_still_change_non_elevated_target(monkeypatch):
    writes = _install(monkeypatch, target_role="bookkeeper")
    out = await members.update_member(uuid4(), uuid4(), MemberPatch(role="viewer"), _ctx("admin"))
    assert out["ok"] is True
    assert any(sql.startswith("UPDATE public.workspace_membership") for sql in writes)


async def test_owner_can_demote_owner_when_two_owners(monkeypatch):
    writes = _install(monkeypatch, target_role="owner", owners=2)
    out = await members.update_member(uuid4(), uuid4(), MemberPatch(role="viewer"), _ctx("owner"))
    assert out["ok"] is True
    assert any(sql.startswith("UPDATE public.workspace_membership") for sql in writes)


async def test_last_owner_guard_unchanged(monkeypatch):
    writes = _install(monkeypatch, target_role="owner", owners=1)
    with pytest.raises(HTTPException) as exc:
        await members.update_member(uuid4(), uuid4(), MemberPatch(role="viewer"), _ctx("owner"))
    assert exc.value.status_code == 400
    assert "last owner" in exc.value.detail
    assert writes == []


# --- AO-ROLE-1b: elevated invitations depend on their creator -------------


def _accept_session(monkeypatch, *, role: str, creator_is_owner: bool):
    from app import auth
    from unittest.mock import AsyncMock

    wid = uuid4()
    inv = SimpleNamespace(
        id=uuid4(), workspace_id=wid, email="robin@example.com", role=role,
        entity_scope=None, expires_at=None, accepted_at=None, cancelled_at=None,
        created_by_uc_uid="creator-uid", workspace_name="Fictional Books",
        workspace_slug="fictional",
    )
    writes: list[str] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def begin(self):
            return self

        async def execute(self, statement, params=None):
            sql = str(statement).strip()
            if "FROM public.workspace_invitation i" in sql:
                return SimpleNamespace(first=lambda: inv)
            if "SELECT id, email FROM public.user_account" in sql:
                return SimpleNamespace(
                    first=lambda: SimpleNamespace(id=uuid4(), email=inv.email)
                )
            if "u.uc_uid = :creator" in sql:
                return SimpleNamespace(first=lambda: (1,) if creator_is_owner else None)
            if sql.startswith("SELECT role::text"):
                return SimpleNamespace(first=lambda: None)
            if not sql.startswith("SELECT"):
                writes.append(sql)
            return SimpleNamespace(rowcount=1)

    caller = SimpleNamespace(uc_uid="robin-uid", claims={"email_verified": True}, agent_id=None)
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=caller))
    monkeypatch.setattr(members, "_token_email_verified", lambda _: True)
    monkeypatch.setattr(members, "SessionLocal", Session)
    monkeypatch.setattr(members, "_write_membership_audit", AsyncMock())
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer fictional")]})
    return request, writes


@pytest.mark.parametrize("role", ["owner", "client_owner", "admin"])
async def test_elevated_invite_refused_when_creator_no_longer_owner(monkeypatch, role):
    request, writes = _accept_session(monkeypatch, role=role, creator_is_owner=False)
    with pytest.raises(HTTPException) as exc:
        await members.accept_invitation(request=request, token="fictional-invitation")
    assert exc.value.status_code == 404
    assert exc.value.detail == "invitation not found or expired"
    assert not any("workspace_membership" in sql for sql in writes)


async def test_elevated_invite_accepted_when_creator_still_owner(monkeypatch):
    request, writes = _accept_session(monkeypatch, role="admin", creator_is_owner=True)
    reply = await members.accept_invitation(request=request, token="fictional-invitation")
    assert reply["status"] == "accepted"
    assert any("INSERT INTO public.workspace_membership" in sql for sql in writes)


async def test_non_elevated_invite_does_not_check_creator(monkeypatch):
    request, _ = _accept_session(monkeypatch, role="bookkeeper", creator_is_owner=False)
    reply = await members.accept_invitation(request=request, token="fictional-invitation")
    assert reply["status"] == "accepted"


async def test_removal_cancels_elevated_invites_the_person_created(monkeypatch):
    from app.security import member_removal

    seen: list[tuple[str, dict]] = []

    class Session:
        async def execute(self, statement, params=None):
            seen.append((str(statement), params))
            return SimpleNamespace(all=lambda: [SimpleNamespace(id=uuid4())])

    rows = await member_removal.cancel_elevated_invitations_created_by(
        Session(), uuid4(), "creator-uid"
    )
    assert len(rows) == 1
    sql, params = seen[-1]
    assert "created_by_uc_uid = :creator" in sql
    assert "'owner', 'client_owner', 'admin'" in sql
    assert params["creator"] == "creator-uid"


async def test_remove_person_calls_creator_cancel(monkeypatch):
    from app.security import member_removal

    cancel_calls = []

    async def fake_cancel(session, wid, uc_uid):
        cancel_calls.append(uc_uid)
        return []

    monkeypatch.setattr(member_removal, "cancel_elevated_invitations_created_by", fake_cancel)
    monkeypatch.setattr(member_removal, "_load_service_tokens", lambda: {})
    wid = uuid4()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def begin(self):
            return self

        async def execute(self, statement, params=None):
            one = SimpleNamespace(
                id=uuid4(), uc_uid="owner-uid", role="owner", entity_scope=None,
                status="active",
            )
            return SimpleNamespace(
                first=lambda: one, all=lambda: [], scalar_one=lambda: 2
            )

    monkeypatch.setattr(member_removal, "SessionLocal", Session)
    await member_removal.remove_person(wid, subject="owner-uid")
    assert cancel_calls == ["owner-uid"]


async def test_owner_demotion_cancels_their_elevated_invites(monkeypatch):
    _install(monkeypatch, target_role="owner", owners=2)
    cancelled = []

    from app.security import member_removal

    async def fake_cancel(session, wid, uc_uid):
        cancelled.append(uc_uid)
        return [SimpleNamespace(id=uuid4())]

    monkeypatch.setattr(member_removal, "cancel_elevated_invitations_created_by", fake_cancel)
    await members.update_member(uuid4(), uuid4(), MemberPatch(role="viewer"), _ctx("owner"))
    assert cancelled == ["target-uid"]

    cancelled.clear()
    await members.update_member(uuid4(), uuid4(), MemberPatch(role="owner"), _ctx("owner"))
    assert cancelled == []


# --- AO-RACE-1: accept must not race a cancel to a half-resolved row -------


def _cancel_race_session(monkeypatch, *, accept_rowcount: int):
    """An elevated invitation whose creator check still passes, then the
    cancel UPDATE commits before the accept UPDATE runs (rowcount simulates
    the row no longer matching the accept guards)."""
    from app import auth
    from unittest.mock import AsyncMock

    wid = uuid4()
    inv = SimpleNamespace(
        id=uuid4(), workspace_id=wid, email="robin@example.com", role="admin",
        entity_scope=None, expires_at=None, accepted_at=None, cancelled_at=None,
        created_by_uc_uid="creator-uid", workspace_name="Fictional Books",
        workspace_slug="fictional",
    )
    calls: list[tuple[str, dict | None]] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        def begin(self):
            return self

        async def execute(self, statement, params=None):
            sql = str(statement).strip()
            calls.append((sql, params))
            if "FROM public.workspace_invitation i" in sql:
                return SimpleNamespace(first=lambda: inv)
            if "SELECT id, email FROM public.user_account" in sql:
                return SimpleNamespace(
                    first=lambda: SimpleNamespace(id=uuid4(), email=inv.email)
                )
            if "u.uc_uid = :creator" in sql:
                # The creator is still an active owner when checked: the race
                # window is the demotion/cancel that commits after this read.
                return SimpleNamespace(first=lambda: (1,))
            if sql.startswith("SELECT role::text"):
                return SimpleNamespace(first=lambda: None)
            if "UPDATE public.workspace_invitation" in sql:
                return SimpleNamespace(rowcount=accept_rowcount)
            return SimpleNamespace(rowcount=1)

    caller = SimpleNamespace(uc_uid="robin-uid", claims={"email_verified": True}, agent_id=None)
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=caller))
    monkeypatch.setattr(members, "_token_email_verified", lambda _: True)
    monkeypatch.setattr(members, "SessionLocal", Session)
    monkeypatch.setattr(members, "_write_membership_audit", AsyncMock())
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer fictional")]})
    return request, calls


async def test_accept_after_creator_demotion_race_returns_410(monkeypatch):
    request, calls = _cancel_race_session(monkeypatch, accept_rowcount=0)
    with pytest.raises(HTTPException) as exc:
        await members.accept_invitation(request=request, token="fictional-invitation")
    assert exc.value.status_code == 410
    assert exc.value.detail == "invitation already resolved"
    # The accept UPDATE carries the guards that make the race safe.
    accept_sql = next(sql for sql, _ in calls if "UPDATE public.workspace_invitation" in sql)
    assert "cancelled_at IS NULL" in accept_sql
    assert "accepted_at IS NULL" in accept_sql
    # The creator read took the share lock for the window it covers.
    creator_sql = next(sql for sql, _ in calls if "u.uc_uid = :creator" in sql)
    assert "FOR SHARE OF m" in creator_sql


async def test_accept_when_row_still_pending_returns_200(monkeypatch):
    request, _ = _cancel_race_session(monkeypatch, accept_rowcount=1)
    reply = await members.accept_invitation(request=request, token="fictional-invitation")
    assert reply["status"] == "accepted"
