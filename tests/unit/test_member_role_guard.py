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
            return SimpleNamespace()

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
    assert len(writes) == 1


async def test_last_owner_guard_unchanged(monkeypatch):
    writes = _install(monkeypatch, target_role="owner", owners=1)
    with pytest.raises(HTTPException) as exc:
        await members.update_member(uuid4(), uuid4(), MemberPatch(role="viewer"), _ctx("owner"))
    assert exc.value.status_code == 400
    assert "last owner" in exc.value.detail
    assert writes == []
