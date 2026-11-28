# Copyright (C) 2026 Magic Unicorn Unconventional Technology & Stuff Inc
# and the Accounting-Ops contributors.
# SPDX-License-Identifier: Apache-2.0
"""Run only on a migrated, disposable PostgreSQL, with owner AND runtime roles.

The normal no-stack tier skips before opening a connection. The integration
harness supplies both DSNs; connection/migration errors then FAIL, never skip.
All IdP/entitlement/graph traffic is mocked; only the scratch PostgreSQL is used.
"""

import asyncio
import hashlib
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import auth, db, tenancy
from agents.sentinel.checks import pending_invitations
from app.api import clients, members, onboarding, personal_access_tokens, provisioning, work_inbox, workspaces
from app.security import member_removal, provisioning_token, uc_api_key
from app.security.entitlements import EntitlementResult
from app.security.pat import generate_secret
from app.workspace_session import workspace_session
from scripts import provision_workspace as shared

pytestmark = pytest.mark.integration
_SECRET = "fictional-service-signing-material-" * 2


class Harness:
    def __init__(self, owner, runtime):
        self.owner, self.runtime = owner, runtime
        self.workspaces, self.users = [], []

    async def sql(self, sql, **params):
        async with self.owner.begin() as connection:
            return await connection.execute(text(sql), params)

    def body(self, wid=None):
        wid = wid or uuid4()
        self.workspaces.append(wid)
        return dict(
            org_id=str(wid),
            slug="fictional-books",
            display_name="Fictional Books",
            plan_tier="fixture",
            entitlements=["accounting_ops_access"],
            owner={"subject": "alex@example.com", "email": ""},
            timezone="UTC",
        )

    def headers(self, body=None, jti=None):
        now = int(time.time())
        bearer = jwt.encode(
            dict(
                iss="ops-center",
                aud="accounting-ops",
                iat=now,
                exp=now + 60,
                jti=jti or str(uuid4()),
            ),
            _SECRET,
            algorithm="HS256",
        )
        result = {"Authorization": f"Bearer {bearer}"}
        if body:
            result["Idempotency-Key"] = f"{body['org_id']}:accounting-ops"
        return result

    async def person(self, subject=None):
        uid = uuid4()
        subject = subject or f"alex-{uid}@example.com"
        self.users.append(uid)
        await self.sql(
            "INSERT INTO public.user_account (id, uc_uid, is_active) VALUES (:u, :s, true)",
            u=uid,
            s=subject,
        )
        return uid, subject

    async def seat(self, wid, uid, status="active", role="viewer"):
        await self.sql(
            """
            INSERT INTO public.workspace_membership (id, workspace_id, user_id, role, status)
            VALUES (gen_random_uuid(), :w, :u, CAST(:role AS workspace_role), :status)
            ON CONFLICT ON CONSTRAINT uq_workspace_membership
            DO UPDATE SET status = EXCLUDED.status, role = EXCLUDED.role
        """,
            w=wid,
            u=uid,
            role=role,
            status=status,
        )

    async def pat(self, uid, scope):
        secret, pid = generate_secret(), uuid4()
        await self.sql(
            """
            INSERT INTO public.personal_access_token
                (id, owner_user_id, name, secret_hash, secret_prefix, scopes, workspace_scope,
                 expires_at)
            VALUES (:id, :u, 'Fictional test', :hash, :prefix, ARRAY['accounting:read'], :scope,
                    now() + interval '30 days')
        """,
            id=pid,
            u=uid,
            hash=hashlib.sha256(secret.encode()).digest(),
            prefix=secret[:12],
            scope=scope,
        )
        return pid, secret

    async def create(self, body=None):
        body = body or self.body()
        response = await self.client.post(
            "/api/v1/provisioning/workspaces", json=body, headers=self.headers(body)
        )
        assert response.status_code == 200
        return body

    async def remove(self, wid, subject):
        return await self.client.delete(
            f"/api/v1/provisioning/workspaces/{wid}/members/{subject}", headers=self.headers()
        )

    async def revoked(self, pid):
        return (
            await self.sql(
                "SELECT revoked_at IS NOT NULL FROM public.personal_access_token WHERE id = :p",
                p=pid,
            )
        ).scalar_one()


@pytest.fixture
async def pg(monkeypatch, stack):
    if not os.getenv("ACCTOPS_TEST_DSN") or not os.getenv("ACCTOPS_TEST_APP_DSN"):
        pytest.skip("requires explicitly configured disposable PostgreSQL owner/runtime DSNs")
    owner = create_async_engine(stack.dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    runtime = create_async_engine(
        stack.app_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    maker = async_sessionmaker(runtime, expire_on_commit=False)
    h = Harness(owner, runtime)
    monkeypatch.setenv("PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("PROVISIONING_JWT_SECRET", _SECRET)
    monkeypatch.delenv("AO_ENTITLED_PROVISIONING", raising=False)
    monkeypatch.delenv("PROVISIONING_MEMBER_ROLE", raising=False)
    # Each sign-in in these tests asks the control plane again, as if the last
    # answer had aged out; the tests about remembering an answer set their own.
    provisioning.forget_answers()
    monkeypatch.setattr(provisioning, "ANSWER_FRESH_SECONDS", 0.0)
    monkeypatch.setattr(provisioning, "RETRY_AFTER_FAILURE_SECONDS", 0.0)
    for module in (
        db,
        tenancy,
        onboarding,
        provisioning,
        member_removal,
        provisioning_token,
        members,
        workspaces,
        clients,
        personal_access_tokens,
        work_inbox,
    ):
        monkeypatch.setattr(module, "SessionLocal", maker)
    monkeypatch.setattr(shared, "_admin_engine", lambda: owner)
    monkeypatch.setattr(shared, "ensure_workspace_subgraph", None)
    monkeypatch.setattr(auth, "_service_tokens_cache", {})
    monkeypatch.setattr(auth, "_touch_pat_last_used", AsyncMock())
    monkeypatch.setattr(auth, "_try_authenticate_local", lambda _: None)
    from app.security import brigade_jwt

    monkeypatch.setattr(brigade_jwt, "verify_brigade_jwt", AsyncMock(return_value=None))
    # SQL really runs as the restricted runtime role, never the owner by accident.
    async with runtime.connect() as connection:
        assert (
            await connection.execute(text("SELECT current_user"))
        ).scalar_one() == "accounting_ops_app"
        await connection.execute(text("SELECT control_plane_state FROM public.workspace LIMIT 1"))
    app = FastAPI()
    app.include_router(provisioning.router)
    app.include_router(workspaces.router)
    app.include_router(members.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://accounting.example.com"
    ) as client:
        h.client = client
        try:
            yield h
        finally:
            # Only fictional rows made by this fixture, in dependency order.
            for wid in h.workspaces:
                await h.sql("DELETE FROM public.membership_audit WHERE workspace_id = :w", w=wid)
                await h.sql("DELETE FROM public.workspace WHERE id = :w", w=wid)
            for uid in h.users:
                await h.sql("DELETE FROM public.user_account WHERE id = :u", u=uid)
    await runtime.dispose()
    await owner.dispose()


async def test_post_replay_does_not_modify_existing_registry(pg, monkeypatch):
    body = await pg.create()
    before = (
        await pg.sql(
            "SELECT * FROM public.workspace WHERE id = CAST(:id AS uuid)", id=body["org_id"]
        )
    ).first()
    assert (
        await pg.sql(
            "SELECT count(*) FROM public.workspace_membership WHERE workspace_id = :w", w=before.id
        )
    ).scalar_one() == 0
    provision = AsyncMock(side_effect=AssertionError("replay called shared provisioner"))
    monkeypatch.setattr(shared, "provision_workspace", provision)
    changed = {
        **body,
        "display_name": "Changed",
        "slug": "changed",
        "plan_tier": "different",
        "owner": {"subject": "someone-else@example.com"},
    }
    response = await pg.client.post(
        "/api/v1/provisioning/workspaces", json=changed, headers=pg.headers(changed)
    )
    assert response.json() == {"workspace_id": body["org_id"], "status": "active"}
    after = (await pg.sql("SELECT * FROM public.workspace WHERE id = :id", id=before.id)).first()
    assert tuple(after) == tuple(before)
    provision.assert_not_called()


async def test_same_slug_is_never_identity(pg):
    a, b = await pg.create(), await pg.create()
    assert a["slug"] == b["slug"] and a["org_id"] != b["org_id"]
    rows = (
        await pg.sql("SELECT slug FROM public.workspace WHERE id = ANY(:ids)", ids=pg.workspaces)
    ).all()
    assert len({row.slug for row in rows}) == 2


async def test_generated_slug_collision_gets_another_slug(pg):
    body = await pg.create()
    next_body = pg.body()
    from app.api.onboarding import _org_workspace_slug, _workspace_id_for_org

    occupied = _org_workspace_slug(_workspace_id_for_org(next_body["org_id"]))
    await pg.sql(
        "UPDATE public.workspace SET slug = :s WHERE id = CAST(:w AS uuid)",
        s=occupied,
        w=body["org_id"],
    )
    await pg.create(next_body)
    row = (
        await pg.sql(
            "SELECT slug FROM public.workspace WHERE id = CAST(:w AS uuid)", w=next_body["org_id"]
        )
    ).first()
    assert row.slug != occupied


@pytest.mark.parametrize("first", ["post", "signin"])
async def test_post_and_signin_converge(pg, first):
    body = pg.body()
    uid, subject = await pg.person()
    answer = SimpleNamespace(
        fetch=AsyncMock(
            return_value=EntitlementResult(
                "entitled", org_id=body["org_id"], org_role="admin", org_name="Fictional Books"
            )
        )
    )
    ctx = auth.AuthContext(
        subject, None, (), {"iss": "https://identity.example.com"}, may_autoprovision=True
    )
    if first == "post":
        await pg.create(body)
    result = await onboarding.ensure_entitled_workspace(
        ctx, "fictional-bearer", entitlement_client=answer
    )
    if first == "signin":
        await pg.create(body)
    assert str(result.workspace_id) == body["org_id"]
    row = (
        await pg.sql(
            "SELECT role::text, status FROM public.workspace_membership "
            "WHERE workspace_id = :w AND user_id = :u",
            w=result.workspace_id,
            u=uid,
        )
    ).first()
    assert tuple(row) == ("admin", "active")
    assert answer.fetch.await_count == 1


async def test_state_transitions_and_erasure_stays_closed(pg):
    body = await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    await pg.seat(wid, uid)
    _, pat = await pg.pat(uid, [wid])
    url = f"/api/v1/provisioning/workspaces/{wid}"
    user_url = f"/api/workspaces/{wid}"
    for state in ("suspended", "archived", "active", "archived", "suspended", "active"):
        reply = await pg.client.patch(url, json={"state": state}, headers=pg.headers())
        assert reply.json() == {"workspace_id": str(wid), "status": state}
        read = await pg.client.get(url, headers=pg.headers())
        assert read.json() == reply.json()
        # Contract item 3's explicit storage-only fallback: reads must remain usable.
        assert (
            await pg.client.get(user_url, headers={"Authorization": f"Bearer {pat}"})
        ).status_code == 200
    # Reproduce the erasure transition without invoking or changing erasure code.
    await pg.sql("UPDATE public.workspace SET status = 'suspended' WHERE id = :w", w=wid)
    await pg.create(body)
    await pg.client.patch(url, json={"state": "active"}, headers=pg.headers())
    assert (
        await pg.client.get(user_url, headers={"Authorization": f"Bearer {pat}"})
    ).status_code == 403
    assert (await pg.client.get(url, headers=pg.headers())).json()["status"] == "suspended"
    assert (await pg.remove(wid, subject)).status_code == 204


@pytest.mark.parametrize("kind", ["pat", "static", "uc_key", "browser", "machine", "agent"])
async def test_person_credentials_work_before_and_fail_after(pg, monkeypatch, kind):
    await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    await pg.seat(wid, uid)
    if kind == "pat":
        _, bearer = await pg.pat(uid, [wid])
    elif kind == "static":
        bearer = "fictional-static-token-" * 3
        monkeypatch.setattr(
            auth,
            "_service_tokens_cache",
            {bearer: dict(uc_uid=subject, agent_id="fixture", workspace_ids=frozenset({wid}))},
        )
    elif kind == "uc_key":
        bearer = "uc_fictional_key_for_example_com"
        monkeypatch.setattr(
            uc_api_key,
            "resolve_uc_api_key",
            AsyncMock(
                return_value=uc_api_key.UcKeyIdentity(
                    subject, ("mcp:accounting-ops",), "fixture", None, None, {}
                )
            ),
        )
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
        key["kid"] = "fixture"
        monkeypatch.setattr(auth, "_fetch_jwks", AsyncMock(return_value={"keys": [key]}))
        monkeypatch.setattr(
            auth,
            "get_settings",
            lambda: SimpleNamespace(
                keycloak_realm_url="https://identity.example.com",
                accepted_audience="accounting-ops",
                keycloak_expected_audiences=(),
                trusted_m2m_callers=(),
            ),
        )
        now = int(time.time())
        payload = dict(
            sub=subject,
            iss="https://identity.example.com",
            aud="accounting-ops",
            iat=now,
            exp=now + 300,
            groups=["fictional-books/owner"],
        )
        if kind == "browser":
            payload["sid"] = "fixture-session"
        if kind == "machine":
            payload.update(
                azp="fixture-client",
                clientId="fixture-client",
                preferred_username="service-account-fixture-client",
            )
        if kind == "agent":
            payload["act"] = {"sub": "fixture-agent"}
        bearer = jwt.encode(payload, private, algorithm="RS256", headers={"kid": "fixture"})
    headers = {"Authorization": f"Bearer {bearer}"}
    url = f"/api/workspaces/{wid}"
    assert (await pg.client.get(url, headers=headers)).status_code == 200
    removed = await pg.remove(wid, subject)
    assert removed.status_code == 204
    assert removed.headers["x-static-service-token-count"] == ("1" if kind == "static" else "0")
    assert (await pg.client.get(url, headers=headers)).status_code == (
        401 if kind == "pat" else 403
    )


async def test_scoped_and_shared_tokens_follow_membership_rule(pg):
    await pg.create()
    await pg.create()
    a, b = pg.workspaces
    uid, subject = await pg.person()
    await pg.seat(a, uid)
    await pg.seat(b, uid)
    a_id, _ = await pg.pat(uid, [a])
    b_id, b_key = await pg.pat(uid, [b])
    shared_id, shared_key = await pg.pat(uid, [a, b])
    global_id, global_key = await pg.pat(uid, None)
    assert (await pg.remove(a, subject)).headers["x-revoked-pat-count"] == "1"
    assert await pg.revoked(a_id)
    for pid in (b_id, shared_id, global_id):
        assert not await pg.revoked(pid)
    for bearer in (shared_key, global_key):
        headers = {"Authorization": f"Bearer {bearer}"}
        assert (await pg.client.get(f"/api/workspaces/{a}", headers=headers)).status_code == 403
        assert (await pg.client.get(f"/api/workspaces/{b}", headers=headers)).status_code == 200
    assert (
        await pg.client.get(f"/api/workspaces/{b}", headers={"Authorization": f"Bearer {b_key}"})
    ).status_code == 200
    assert (await pg.remove(b, subject)).headers["x-revoked-pat-count"] == "3"
    assert await pg.revoked(shared_id) and await pg.revoked(global_id)


async def test_missing_membership_still_revokes_and_unknowns_are_safe(pg):
    await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    pid, _ = await pg.pat(uid, [wid])
    assert (await pg.remove(wid, "unknown@example.com")).status_code == 204
    assert not await pg.revoked(pid)
    assert (await pg.remove(uuid4(), subject)).status_code == 404
    reply = await pg.remove(wid, subject)
    assert reply.status_code == 204 and reply.headers["x-revoked-pat-count"] == "1"
    before = (
        await pg.sql(
            "SELECT max(occurred_at) FROM public.membership_audit WHERE workspace_id = :w", w=wid
        )
    ).scalar_one()
    assert (await pg.remove(wid, subject)).headers["x-revoked-pat-count"] == "0"
    after = (
        await pg.sql(
            "SELECT max(occurred_at) FROM public.membership_audit WHERE workspace_id = :w", w=wid
        )
    ).scalar_one()
    assert after > before
    assert (
        await pg.sql(
            "SELECT sum(revoked_pat_count) FROM public.membership_audit WHERE workspace_id = :w",
            w=wid,
        )
    ).scalar_one() == 1


async def test_own_delete_nonmember_does_not_count_or_revoke(pg, monkeypatch):
    await pg.create()
    wid = pg.workspaces[-1]
    uid, _ = await pg.person()
    pid, _ = await pg.pat(uid, None)
    actor = tenancy.WorkspaceContext(
        wid, "fixture", "owner", "owner@example.com", None, None, False
    )
    counted = AsyncMock(side_effect=AssertionError("nonmember credentials counted"))
    monkeypatch.setattr(member_removal, "_load_service_tokens", counted)
    with pytest.raises(HTTPException) as exc:
        await members.remove_member(wid, uid, actor)
    assert exc.value.status_code == 404 and not await pg.revoked(pid)
    counted.assert_not_called()


async def test_own_delete_uses_same_policy_and_preserves_self_and_last_owner(pg):
    await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    await pg.seat(wid, uid, role="owner")
    pid, _ = await pg.pat(uid, [wid])
    actor = tenancy.WorkspaceContext(wid, "fixture", "owner", subject, None, None, False)
    with pytest.raises(HTTPException) as exc:
        await members.remove_member(wid, uid, actor)
    assert exc.value.status_code == 400 and not await pg.revoked(pid)
    actor = tenancy.WorkspaceContext(
        wid, "fixture", "owner", "other@example.com", None, None, False
    )
    with pytest.raises(HTTPException) as exc:
        await members.remove_member(wid, uid, actor)
    assert exc.value.status_code == 400 and not await pg.revoked(pid)
    await pg.seat(wid, uid, role="viewer")
    result = await members.remove_member(wid, uid, actor)
    assert result["revoked_pat_count"] == 1 and await pg.revoked(pid)


@pytest.mark.parametrize(
    "role,member_role,expected",
    [
        ("owner", None, "owner"),
        ("admin", None, "admin"),
        # Not an administrator: no seat in the books from the answer alone...
        ("manager", None, None),
        ("member", None, None),
        ("billing_admin", None, None),
        ("unknown", None, None),
        # ...unless the deployment names the role an ordinary member starts with.
        ("member", "viewer", "viewer"),
        ("unknown", "viewer", "viewer"),
    ],
)
async def test_jit_role_comes_from_answer_not_claims(pg, monkeypatch, role, member_role, expected):
    if member_role:
        monkeypatch.setenv("PROVISIONING_MEMBER_ROLE", member_role)
    body = await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    ctx = auth.AuthContext(
        subject,
        None,
        (),
        {"groups": ["fictional-books/owner"], "role": "owner"},
        may_autoprovision=True,
    )
    client = SimpleNamespace(
        fetch=AsyncMock(
            return_value=EntitlementResult("entitled", org_id=body["org_id"], org_role=role)
        )
    )
    result = await onboarding.ensure_entitled_workspace(ctx, "fixture", entitlement_client=client)
    assert (
        await pg.sql(
            "SELECT role::text FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).scalar_one_or_none() == expected
    if expected is None:
        # The group claim that names the workspace decided nothing either.
        assert result.state == "not_entitled"
        return
    # A later answer demotes in place, it never removes/re-adds or restores PATs.
    old_id = (
        await pg.sql(
            "SELECT id FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).scalar_one()
    client.fetch.return_value = EntitlementResult(
        "entitled", org_id=body["org_id"], org_role="member"
    )
    await onboarding.ensure_entitled_workspace(ctx, "fixture", entitlement_client=client)
    row = (
        await pg.sql(
            "SELECT id, role::text FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).first()
    assert tuple(row) == (old_id, "viewer")


async def test_paused_stays_paused_and_revoked_needs_positive_answer(pg):
    body = await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    ctx = auth.AuthContext(
        subject, None, (), {"groups": ["fictional-books/owner"]}, may_autoprovision=True
    )
    client = SimpleNamespace(
        fetch=AsyncMock(
            return_value=EntitlementResult("entitled", org_id=body["org_id"], org_role="owner")
        )
    )
    await pg.seat(wid, uid, status="suspended")
    await onboarding.ensure_entitled_workspace(ctx, "fixture", entitlement_client=client)
    await onboarding._upsert_workspace_membership(uid, wid, "owner", preserve_existing_role=False)
    row = (
        await pg.sql(
            "SELECT status, role::text FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).first()
    assert tuple(row) == ("suspended", "viewer")
    pid, _ = await pg.pat(uid, [wid])
    await pg.remove(wid, subject)
    for verdict in (
        EntitlementResult("not_entitled"),
        EntitlementResult("unavailable"),
        EntitlementResult("entitled", org_id=body["org_id"], org_role=""),
    ):
        client.fetch.return_value = verdict
        await onboarding.ensure_entitled_workspace(ctx, "fixture", entitlement_client=client)
        assert (
            await pg.sql(
                "SELECT status FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
                w=wid,
                u=uid,
            )
        ).scalar_one() == "revoked"
    client.fetch.return_value = EntitlementResult(
        "entitled", org_id=body["org_id"], org_role="owner"
    )
    await onboarding.ensure_entitled_workspace(ctx, "fixture", entitlement_client=client)
    assert (
        await pg.sql(
            "SELECT status FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).scalar_one() == "active"
    assert await pg.revoked(pid)


async def test_jti_replay_and_full_store(pg, monkeypatch):
    body = await pg.create()
    url = f"/api/v1/provisioning/workspaces/{body['org_id']}"
    headers = pg.headers()
    assert (await pg.client.get(url, headers=headers)).status_code == 200
    assert (await pg.client.get(url, headers=headers)).status_code == 401
    # Preserve other tests' replay rows: capacity zero exercises the real full
    # branch regardless of the count, without truncating shared scratch data.
    monkeypatch.setattr(provisioning_token, "REPLAY_CAPACITY", 0)
    assert (await pg.client.get(url, headers=pg.headers())).status_code == 503


async def test_two_removals_serialize_on_person_postgresql(pg, monkeypatch):
    await pg.create()
    await pg.create()
    a, b = pg.workspaces
    uid, subject = await pg.person()
    await pg.seat(a, uid)
    await pg.seat(b, uid)
    pid, _ = await pg.pat(uid, None)
    first_acquired, second_attempted, second_acquired = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    release = asyncio.Event()
    attempts = 0

    class ObservedSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            nonlocal attempts
            sql = str(statement)
            is_person_lock = sql.startswith("SELECT id, uc_uid FROM public.user_account")
            index = None
            if is_person_lock:
                attempts += 1
                index = attempts
                if index == 2:
                    second_attempted.set()
            result = await super().execute(statement, *args, **kwargs)
            if index == 1:
                first_acquired.set()
                await release.wait()
            elif index == 2:
                second_acquired.set()
            return result

    monkeypatch.setattr(
        member_removal, "SessionLocal", async_sessionmaker(pg.runtime, class_=ObservedSession)
    )
    first = asyncio.create_task(member_removal.remove_person(a, subject=subject))
    await asyncio.wait_for(first_acquired.wait(), 5)
    second = asyncio.create_task(member_removal.remove_person(b, subject=subject))
    try:
        await asyncio.wait_for(second_attempted.wait(), 5)
        # Removing FOR UPDATE makes this assertion fail on PostgreSQL: the
        # second transaction passes the user lock while the first is held.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_acquired.wait(), 0.2)
    finally:
        release.set()
        receipts = await asyncio.wait_for(asyncio.gather(first, second), 10)
    assert sum(r["revoked_pat_count"] for r in receipts) == 1
    assert await pg.revoked(pid)


async def test_inflight_entitlement_cannot_undo_later_removal(pg):
    body = await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    await pg.seat(wid, uid)
    answer_started, release = asyncio.Event(), asyncio.Event()

    async def fetch(_):
        answer_started.set()
        await release.wait()
        return EntitlementResult("entitled", org_id=body["org_id"], org_role="owner")

    ctx = auth.AuthContext(subject, None, (), {}, may_autoprovision=True)
    signin = asyncio.create_task(
        onboarding.ensure_entitled_workspace(
            ctx, "fixture", entitlement_client=SimpleNamespace(fetch=fetch)
        )
    )
    await asyncio.wait_for(answer_started.wait(), 5)
    removal = asyncio.create_task(member_removal.remove_person(wid, subject=subject))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(removal), 0.2)
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(signin, removal), 10)
    assert (
        await pg.sql(
            "SELECT status FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).scalar_one() == "revoked"


@pytest.mark.parametrize("state", ["active", "suspended", "archived"])
async def test_member_removal_in_every_control_plane_state(pg, state):
    await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    await pg.seat(wid, uid)
    pid, _ = await pg.pat(uid, [wid])
    reply = await pg.client.patch(
        f"/api/v1/provisioning/workspaces/{wid}", json={"state": state}, headers=pg.headers()
    )
    assert reply.status_code == 200
    assert (await pg.remove(wid, subject)).status_code == 204
    assert await pg.revoked(pid)


async def test_legacy_adoption_requires_exact_admin_subject(pg):
    body = await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    await pg.seat(wid, uid, role="admin")
    await pg.sql("UPDATE public.workspace SET uc_org_id = NULL WHERE id = :w", w=wid)
    wrong = {**body, "owner": {"subject": "stranger@example.com", "email": subject}}
    reply = await pg.client.post(
        "/api/v1/provisioning/workspaces", json=wrong, headers=pg.headers(wrong)
    )
    assert reply.status_code == 409
    assert (
        await pg.sql("SELECT uc_org_id FROM public.workspace WHERE id = :w", w=wid)
    ).scalar_one() is None
    correct = {**body, "owner": {"subject": subject}}
    assert (
        await pg.client.post(
            "/api/v1/provisioning/workspaces", json=correct, headers=pg.headers(correct)
        )
    ).status_code == 200
    assert (
        await pg.sql("SELECT uc_org_id FROM public.workspace WHERE id = :w", w=wid)
    ).scalar_one() == str(wid)


async def test_runtime_cannot_change_erasure_status(pg):
    await pg.create()
    wid = pg.workspaces[-1]
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        async with pg.runtime.begin() as connection:
            await connection.execute(
                text("UPDATE public.workspace SET status = 'active' WHERE id = :w"), {"w": wid}
            )


async def test_concurrent_creation_calls_shared_provisioner_once(pg, monkeypatch):
    body = pg.body()
    provision = AsyncMock(wraps=shared.provision_workspace)
    monkeypatch.setattr(shared, "provision_workspace", provision)
    results = await asyncio.gather(
        *[
            pg.client.post("/api/v1/provisioning/workspaces", json=body, headers=pg.headers(body))
            for _ in range(2)
        ]
    )
    assert [reply.status_code for reply in results] == [200, 200]
    assert results[0].json() == results[1].json()
    assert provision.await_count == 1


def _signed_in(subject):
    return auth.AuthContext(
        subject, None, (), {"iss": "https://identity.example.com"}, may_autoprovision=True
    )


def _answering(org_id, role):
    return EntitlementResult("entitled", org_id=org_id, org_role=role)


async def _membership(pg, wid, uid):
    row = (
        await pg.sql(
            "SELECT role::text, status FROM public.workspace_membership "
            "WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).first()
    return tuple(row) if row else None


async def test_a_role_set_here_survives_the_next_answer_and_administrators_follow_it(pg):
    body = await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    client = SimpleNamespace(fetch=AsyncMock(return_value=_answering(body["org_id"], "member")))

    async def sign_in():
        return await onboarding.ensure_entitled_workspace(
            _signed_in(subject), "fixture", entitlement_client=client
        )

    # An owner here made the person an accountant: the control plane's "member" leaves that alone.
    await pg.seat(wid, uid, role="accountant")
    assert (await sign_in()).state == "existing"
    assert await _membership(pg, wid, uid) == ("accountant", "active")
    # Who administers follows the answer, up and down.
    client.fetch.return_value = _answering(body["org_id"], "admin")
    await sign_in()
    assert await _membership(pg, wid, uid) == ("admin", "active")
    client.fetch.return_value = _answering(body["org_id"], "member")
    await sign_in()
    assert await _membership(pg, wid, uid) == ("viewer", "active")
    # And the owner's next choice sticks again.
    await pg.seat(wid, uid, role="bookkeeper")
    await sign_in()
    assert await _membership(pg, wid, uid) == ("bookkeeper", "active")


@pytest.mark.parametrize("member_role", [None, "viewer", "accountant"])
async def test_an_ordinary_member_gets_a_seat_only_when_the_setting_names_a_role(
    pg, monkeypatch, member_role
):
    if member_role:
        monkeypatch.setenv("PROVISIONING_MEMBER_ROLE", member_role)
    body = pg.body()  # the control plane has not created this workspace yet
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    client = SimpleNamespace(fetch=AsyncMock(return_value=_answering(body["org_id"], "member")))

    async def sign_in():
        return await onboarding.ensure_entitled_workspace(
            _signed_in(subject), "fixture", entitlement_client=client
        )

    result = await sign_in()
    made = (
        await pg.sql("SELECT count(*) FROM public.workspace WHERE id = :w", w=wid)
    ).scalar_one()
    if member_role:
        assert result.state == "provisioned" and made == 1
        assert await _membership(pg, wid, uid) == (member_role, "active")
    else:
        # In the organization, no seat in its books: no membership and no workspace made for them.
        assert result.state == "not_entitled" and made == 0
        assert await _membership(pg, wid, uid) is None
    # Removed by the control plane, then still on the organization's list at the next sign-in.
    await pg.create(body)
    await pg.seat(wid, uid, role="cpa")
    assert (await pg.remove(wid, subject)).status_code == 204
    await sign_in()
    # Coming back is starting again: the role the deployment names, not the one held before.
    assert await _membership(pg, wid, uid) == (
        (member_role, "active") if member_role else ("cpa", "revoked")
    )


async def test_a_remembered_answer_saves_the_call_and_never_brings_anyone_back(pg, monkeypatch):
    monkeypatch.setattr(provisioning, "ANSWER_FRESH_SECONDS", 60.0)
    body = await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    client = SimpleNamespace(fetch=AsyncMock(return_value=_answering(body["org_id"], "owner")))

    async def sign_in():
        return await onboarding.ensure_entitled_workspace(
            _signed_in(subject), "fixture", entitlement_client=client
        )

    for _ in range(3):
        result = await sign_in()
    assert client.fetch.await_count == 1
    assert result.state == "existing" and result.workspace_id == wid
    assert await _membership(pg, wid, uid) == ("owner", "active")
    # A removal while that answer is still remembered: what is remembered changes nothing.
    # The next sign-in asks again, under the person's lock, and the answer is now "no".
    pid, _ = await pg.pat(uid, [wid])
    assert (await pg.remove(wid, subject)).status_code == 204
    client.fetch.return_value = EntitlementResult("not_entitled")
    assert (await sign_in()).state == "not_entitled"
    assert client.fetch.await_count == 2
    assert await _membership(pg, wid, uid) == ("owner", "revoked")
    # That "no" is remembered too.
    await sign_in()
    assert client.fetch.await_count == 2
    assert await _membership(pg, wid, uid) == ("owner", "revoked")
    # Only a fresh "yes" brings the person back, and never the key that was revoked.
    provisioning.forget_answers()
    client.fetch.return_value = _answering(body["org_id"], "owner")
    await sign_in()
    assert client.fetch.await_count == 3
    assert await _membership(pg, wid, uid) == ("owner", "active")
    assert await pg.revoked(pid)


async def test_the_control_plane_being_down_locks_out_nobody_who_is_already_in(pg, monkeypatch):
    monkeypatch.setattr(provisioning, "RETRY_AFTER_FAILURE_SECONDS", 60.0)
    await pg.create()
    wid = pg.workspaces[-1]
    uid, subject = await pg.person()
    await pg.seat(wid, uid, role="accountant")
    _, newcomer = await pg.person()
    down = SimpleNamespace(fetch=AsyncMock(side_effect=RuntimeError("fictional outage")))

    async def sign_in(who):
        return await onboarding.ensure_entitled_workspace(
            _signed_in(who), "fixture", entitlement_client=down
        )

    result = await sign_in(subject)
    assert result.state == "existing" and result.workspace_id == wid
    assert (await sign_in(newcomer)).state == "unavailable"
    # A lookup that failed is not repeated straight away, and the answers stay the same.
    assert (await sign_in(subject)).state == "existing"
    assert (await sign_in(newcomer)).state == "unavailable"
    assert down.fetch.await_count == 2
    assert await _membership(pg, wid, uid) == ("accountant", "active")
    # "No verdict" in an answer is the same as no answer.
    provisioning.forget_answers()
    silent = SimpleNamespace(fetch=AsyncMock(return_value=EntitlementResult("unavailable")))
    result = await onboarding.ensure_entitled_workspace(
        _signed_in(subject), "fixture", entitlement_client=silent
    )
    assert result.state == "existing" and result.workspace_id == wid


async def test_a_removed_member_is_not_listed_and_does_not_count_as_an_owner(pg):
    await pg.create()
    wid = pg.workspaces[-1]
    owner_uid, owner_subject = await pg.person()
    await pg.seat(wid, owner_uid, role="owner")
    gone_uid, _ = await pg.person()
    await pg.seat(wid, gone_uid, role="owner")
    paused_uid, _ = await pg.person()
    await pg.seat(wid, paused_uid, status="suspended", role="bookkeeper")
    actor = tenancy.WorkspaceContext(wid, "fixture", "owner", owner_subject, None, None, False)
    await members.remove_member(wid, gone_uid, actor)
    listed = await members.list_members(wid, actor)
    assert {(row["user_id"], row["status"]) for row in listed} == {
        (str(owner_uid), "active"),
        (str(paused_uid), "suspended"),
    }
    # The removed owner's row is kept, revoked. It is not an owner: the one who is left cannot be demoted.
    assert await _membership(pg, wid, gone_uid) == ("owner", "revoked")
    other = tenancy.WorkspaceContext(
        wid, "fixture", "owner", "another-owner@example.com", None, None, False
    )
    with pytest.raises(HTTPException) as exc:
        await members.update_member(wid, owner_uid, members.MemberPatch(role="viewer"), other)
    assert exc.value.status_code == 400 and "last owner" in exc.value.detail
    assert await _membership(pg, wid, owner_uid) == ("owner", "active")


async def test_an_accepted_invitation_starts_a_removed_membership_again(pg, monkeypatch):
    from starlette.requests import Request

    await pg.create()
    wid = pg.workspaces[-1]
    owner_uid, owner_subject = await pg.person()
    await pg.seat(wid, owner_uid, role="owner")
    uid, subject = await pg.person()
    await pg.seat(wid, uid, role="accountant")
    actor = tenancy.WorkspaceContext(wid, "fixture", "owner", owner_subject, None, None, False)
    await members.remove_member(wid, uid, actor)
    assert await _membership(pg, wid, uid) == ("accountant", "revoked")
    # The owner invites the same person again, as a bookkeeper this time (a LOWER role than
    # the removed row had: a removed membership is not a membership to be "promoted" from).
    token, invitation = "fictional-invitation-token-" + uuid4().hex, uuid4()
    await pg.sql(
        """
        INSERT INTO public.workspace_invitation
            (id, workspace_id, email, role, token_hash, expires_at, created_by_uc_uid)
        VALUES (:id, :w, 'robin@example.com', CAST('bookkeeper' AS workspace_role), :hash,
                now() + interval '1 day', :by)
    """,
        id=invitation,
        w=wid,
        hash=hashlib.sha256(token.encode()).hexdigest(),
        by=owner_subject,
    )
    caller = auth.AuthContext(
        subject, None, (), {"email": "robin@example.com", "email_verified": True}
    )
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=caller))
    monkeypatch.setattr(members, "_token_email_verified", lambda claims: True)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer fictional")]})
    reply = await members.accept_invitation(request=request, token=token)
    assert reply["status"] == "accepted"
    assert await _membership(pg, wid, uid) == ("bookkeeper", "active")
    listed = await members.list_members(wid, actor)
    assert (str(uid), "active", "bookkeeper") in {
        (row["user_id"], row["status"], row["role"]) for row in listed
    }


async def test_an_organization_without_an_owner_row_still_gets_its_workspace(pg):
    # What Ops-Center sends for such an organization: an empty subject. Also a null plan tier
    # and a name longer than this app keeps.
    long_name = "Fictional " * 25
    body = {
        **pg.body(),
        "owner": {"subject": "", "email": ""},
        "plan_tier": None,
        "display_name": long_name,
    }
    wid = pg.workspaces[-1]
    reply = await pg.client.post(
        "/api/v1/provisioning/workspaces", json=body, headers=pg.headers(body)
    )
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"workspace_id": body["org_id"], "status": "active"}
    assert (
        await pg.sql("SELECT display_name FROM public.workspace WHERE id = :w", w=wid)
    ).scalar_one() == long_name.strip()[:120]
    assert (
        await pg.sql(
            "SELECT count(*) FROM public.workspace_membership WHERE workspace_id = :w", w=wid
        )
    ).scalar_one() == 0
    # An empty subject proves nothing: a workspace from before the control plane is not taken over with it.
    await pg.sql("UPDATE public.workspace SET uc_org_id = NULL WHERE id = :w", w=wid)
    reply = await pg.client.post(
        "/api/v1/provisioning/workspaces", json=body, headers=pg.headers(body)
    )
    assert reply.status_code == 409


@pytest.fixture
async def pg_flag_off(pg, monkeypatch):
    # Setup uses the existing disposable harness; all behavior below runs with
    # the flag absent, through sessions bound to accounting_ops_app.
    await pg.create()
    await pg.create()
    monkeypatch.delenv("PROVISIONING_ENABLED", raising=False)
    async with pg.runtime.connect() as connection:
        privileges = (
            await connection.execute(text("""
                SELECT has_table_privilege(current_user, 'public.workspace_membership', 'DELETE'),
                       has_table_privilege(current_user, 'public.workspace_membership', 'UPDATE')
            """))
        ).one()
        assert tuple(privileges) == (False, True)
    return pg


async def _removal_audits(pg, wid):
    return (
        await pg.sql(
            "SELECT target_user_id, actor_uc_uid, old_role, revoked_pat_count, "
            "static_service_token_count, old_entity_scope FROM public.membership_audit "
            "WHERE workspace_id = :w AND action = 'removal' ORDER BY occurred_at",
            w=wid,
        )
    ).all()


async def test_flag_off_removal_revokes_row_and_scoped_pat_with_receipt(pg_flag_off, monkeypatch):
    pg = pg_flag_off
    wid, other = pg.workspaces
    owner, owner_subject = await pg.person()
    uid, subject = await pg.person()
    await pg.seat(wid, owner, role="owner")
    await pg.seat(wid, uid, role="accountant")
    scope = [str(uuid4())]
    await pg.sql(
        "UPDATE public.workspace_membership SET entity_scope = CAST(:scope AS jsonb) "
        "WHERE workspace_id = :w AND user_id = :u",
        scope=json.dumps(scope), w=wid, u=uid,
    )
    await pg.seat(other, uid)
    scoped, _ = await pg.pat(uid, [wid])
    elsewhere, _ = await pg.pat(uid, [other])
    unscoped, _ = await pg.pat(uid, None)
    monkeypatch.setattr(member_removal, "_load_service_tokens", lambda: {
        "fictional-service": {"uc_uid": subject},
        "fictional-other-service": {"uc_uid": "other@example.com"},
    })
    actor = tenancy.WorkspaceContext(wid, "fictional", "owner", owner_subject, None, None, False)

    # On main this reaches DELETE, which accounting_ops_app is not granted.
    reply = await members.remove_member(wid, uid, actor)
    assert reply == {
        "ok": True, "removed_user_id": str(uid),
        "revoked_pat_count": 1, "static_service_token_count": 1,
    }
    assert await _membership(pg, wid, uid) == ("accountant", "revoked")
    assert await pg.revoked(scoped)
    assert not await pg.revoked(elsewhere)
    assert not await pg.revoked(unscoped)
    assert [tuple(row) for row in await _removal_audits(pg, wid)] == [
        (uid, owner_subject, "accountant", 1, 1, scope)
    ]
    assert str(uid) not in {row["user_id"] for row in await members.list_members(wid, actor)}

    # A second local removal cannot count credentials or append another receipt.
    def forbidden():
        pytest.fail("already removed member's credentials counted")

    monkeypatch.setattr(member_removal, "_load_service_tokens", forbidden)
    with pytest.raises(HTTPException) as exc:
        await members.remove_member(wid, uid, actor)
    assert exc.value.status_code == 404
    assert len(await _removal_audits(pg, wid)) == 1
    assert not await pg.revoked(elsewhere)
    assert not await pg.revoked(unscoped)


@pytest.mark.parametrize("target", ["self", "last_owner", "nonmember", "revoked", "unknown"])
async def test_flag_off_removal_rejections_change_and_count_nothing(pg_flag_off, monkeypatch, target):
    pg = pg_flag_off
    wid = pg.workspaces[0]
    uid, subject = await pg.person()
    pid, _ = await pg.pat(uid, [wid])
    if target in {"self", "last_owner"}:
        await pg.seat(wid, uid, role="owner")
        if target == "last_owner":
            former_owner, _ = await pg.person()
            await pg.seat(wid, former_owner, role="owner", status="revoked")
    elif target == "revoked":
        await pg.seat(wid, uid, status="revoked")
    before = await _membership(pg, wid, uid)
    actor = tenancy.WorkspaceContext(
        wid, "fictional", "owner",
        subject if target == "self" else "another-owner@example.com", None, None, False,
    )

    def forbidden():
        pytest.fail("rejected removal counted credentials")

    monkeypatch.setattr(member_removal, "_load_service_tokens", forbidden)
    with pytest.raises(HTTPException) as exc:
        await members.remove_member(wid, uuid4() if target == "unknown" else uid, actor)
    assert exc.value.status_code == (400 if target in {"self", "last_owner"} else 404)
    assert not await pg.revoked(pid)
    assert await _membership(pg, wid, uid) == before
    assert await _removal_audits(pg, wid) == []


@pytest.mark.parametrize("initial_status", ["active", "revoked"])
async def test_flag_off_invitation_restarts_revoked_row_with_invited_role_and_scope(
    pg_flag_off, monkeypatch, initial_status
):
    from starlette.requests import Request

    pg = pg_flag_off
    wid = pg.workspaces[0]
    uid, subject = await pg.person()
    # Seed the tombstone independently so this tests the invitation gate even
    # before the removal route is fixed.
    await pg.seat(wid, uid, role="accountant", status=initial_status)
    owner, owner_subject = await pg.person()
    await pg.seat(wid, owner, role="owner")
    actor = tenancy.WorkspaceContext(wid, "fictional", "owner", owner_subject, None, None, False)
    if initial_status == "active":
        await members.remove_member(wid, uid, actor)
    assert str(uid) not in {row["user_id"] for row in await members.list_members(wid, actor)}
    scope = [str(uuid4())]
    token, invitation = "fictional-invitation-" + uuid4().hex, uuid4()
    await pg.sql(
        """
        INSERT INTO public.workspace_invitation
            (id, workspace_id, email, role, entity_scope, token_hash, expires_at, created_by_uc_uid)
        VALUES (:id, :w, 'robin@example.com', CAST('bookkeeper' AS workspace_role),
                CAST(:scope AS jsonb), :hash, now() + interval '1 day', 'owner@example.com')
        """,
        id=invitation, w=wid, scope=json.dumps(scope),
        hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    caller = auth.AuthContext(
        subject, None, (), {"email": "robin@example.com", "email_verified": True}
    )
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=caller))
    monkeypatch.setattr(members, "_token_email_verified", lambda _: True)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer fictional")]})
    reply = await members.accept_invitation(request=request, token=token)
    assert reply["status"] == "accepted"
    assert await _membership(pg, wid, uid) == ("bookkeeper", "active")
    assert (
        await pg.sql(
            "SELECT entity_scope FROM public.workspace_membership "
            "WHERE workspace_id = :w AND user_id = :u", w=wid, u=uid,
        )
    ).scalar_one() == scope
    assert (str(uid), "active", "bookkeeper") in {
        (row["user_id"], row["status"], row["role"])
        for row in await members.list_members(wid, actor)
    }


@pytest.mark.parametrize("status", ["revoked", "suspended"])
@pytest.mark.parametrize("activate", [True, False])
@pytest.mark.parametrize("enabled", [False, True])
async def test_onboarding_upsert_preserves_nonactive_rows(
    pg_flag_off, monkeypatch, status, activate, enabled
):
    pg = pg_flag_off
    monkeypatch.setenv("PROVISIONING_ENABLED", str(enabled).lower())
    wid = pg.workspaces[0]
    uid, _ = await pg.person()
    await pg.seat(wid, uid, role="accountant", status=status)
    await onboarding._upsert_workspace_membership(
        uid, wid, "viewer", preserve_existing_role=False, activate_membership=activate,
    )
    assert await _membership(pg, wid, uid) == ("accountant", status)


@pytest.fixture
async def inactive_memberships(pg_flag_off):
    """One caller, both retained statuses, and a separate active seat as control."""
    pg = pg_flag_off
    revoked, suspended = pg.workspaces
    uid, subject = await pg.person()
    await pg.seat(revoked, uid, status="revoked", role="owner")
    await pg.seat(suspended, uid, status="suspended", role="accountant")
    # pg.create() is flag-gated; register the active control via the shared provisioner.
    active = uuid4()
    pg.workspaces.append(active)
    await shared.provision_workspace(
        None, active, "Fictional Active Books", slug=f"fixture-{active}", entities=None,
    )
    await pg.seat(active, uid, role="viewer")
    return SimpleNamespace(
        pg=pg, uid=uid, subject=subject, auth=_signed_in(subject),
        revoked=revoked, suspended=suspended, active=active,
    )


@pytest.mark.parametrize("reader", ["list_my_clients", "list_my_clients_triage"])
async def test_client_lists_exclude_inactive_memberships(inactive_memberships, reader):
    seats = inactive_memberships
    result = await getattr(clients, reader)(auth=seats.auth)
    assert [(w["id"], w["role"]) for w in result["workspaces"]] == [
        (str(seats.active), "viewer")
    ]
    assert result["persona"] == "client"


async def test_client_activity_excludes_inactive_memberships(inactive_memberships, monkeypatch):
    seats = inactive_memberships
    # Observe real workspace sessions: an empty audit table must not mask an unauthorized read.
    from contextlib import asynccontextmanager

    opened = []

    @asynccontextmanager
    async def observed(maker, wid):
        opened.append(str(wid))
        async with workspace_session(maker, wid) as session:
            yield session

    monkeypatch.setattr(clients, "workspace_session", observed)
    assert await clients.cpa_recent_activity(days=30, auth=seats.auth) == {"items": [], "days": 30}
    assert opened == [str(seats.active)]


async def test_client_roles_exclude_inactive_memberships(inactive_memberships):
    seats = inactive_memberships
    uid, _, roles, _ = await clients._caller_account(seats.auth)
    assert uid == seats.uid
    assert roles == ["viewer"]


async def test_pat_scope_ceiling_excludes_inactive_memberships(inactive_memberships):
    seats = inactive_memberships
    assert await personal_access_tokens._owner_max_scopes(seats.uid) == {"accounting:read"}
    with pytest.raises(HTTPException) as exc:
        await personal_access_tokens.mint_pat(
            personal_access_tokens.CreatePATRequest(
                name="Fictional write key", scopes=["accounting:write"], ttl_days=30,
            ),
            auth=seats.auth,
        )
    assert exc.value.status_code == 403


async def test_pat_workspace_scope_excludes_inactive_memberships(inactive_memberships):
    seats = inactive_memberships
    assert await personal_access_tokens._validate_workspace_scope(
        seats.uid, [str(seats.active)]
    ) == (seats.active,)
    for wid in (seats.revoked, seats.suspended):
        with pytest.raises(HTTPException) as exc:
            await personal_access_tokens.mint_pat(
                personal_access_tokens.CreatePATRequest(
                    name="Fictional removed workspace", scopes=["accounting:read"],
                    workspace_scope=[str(wid)], ttl_days=30,
                ),
                auth=seats.auth,
            )
        assert exc.value.status_code == 403
    assert (await seats.pg.sql(
        "SELECT count(*) FROM public.personal_access_token WHERE owner_user_id = :u", u=seats.uid,
    )).scalar_one() == 0


async def test_pat_workspace_list_excludes_inactive_memberships(inactive_memberships):
    seats = inactive_memberships
    result = await personal_access_tokens.pat_meta(auth=seats.auth)
    assert [(w["id"], w["role"]) for w in result["workspaces"]] == [
        (str(seats.active), "viewer")
    ]


async def test_first_workspace_excludes_inactive_memberships(inactive_memberships):
    seats = inactive_memberships
    assert (await onboarding._first_active_workspace(seats.uid))["id"] == str(seats.active)
    await seats.pg.sql(
        "DELETE FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
        w=seats.active, u=seats.uid,
    )
    assert await onboarding._first_active_workspace(seats.uid) is None


async def test_update_member_rejects_inactive_targets(inactive_memberships):
    seats = inactive_memberships
    for wid, role, status in (
        (seats.revoked, "owner", "revoked"),
        (seats.suspended, "accountant", "suspended"),
    ):
        actor = tenancy.WorkspaceContext(wid, "fictional", "owner", "owner@example.com", None, None, False)
        with pytest.raises(HTTPException) as exc:
            await members.update_member(wid, seats.uid, members.MemberPatch(role="bookkeeper"), actor)
        assert exc.value.status_code == 404
        assert "No such member" in exc.value.detail
        assert await _membership(seats.pg, wid, seats.uid) == (role, status)
        assert (await seats.pg.sql(
            "SELECT count(*) FROM public.membership_audit WHERE workspace_id = :w", w=wid,
        )).scalar_one() == 0


async def test_pending_invitations_counts_only_active_members(pg_flag_off, monkeypatch):
    pg = pg_flag_off
    wid = pg.workspaces[0]
    for status in ("active", "revoked", "suspended"):
        uid, _ = await pg.person()
        await pg.seat(wid, uid, status=status, role="accountant")
    action = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(pending_invitations, "execute_action", action)
    async with async_sessionmaker(pg.runtime)() as session:
        ctx = SimpleNamespace(workspace_id=wid, session=session)
        result = await pending_invitations.run(ctx)
    assert result["findings"] == 1
    assert action.await_args.kwargs["details"] == {"admin_like_count": 1}


@pytest.mark.parametrize("status", ["revoked", "suspended"])
async def test_flag_off_entitled_signin_preserves_nonactive_membership(pg_flag_off, monkeypatch, status):
    pg = pg_flag_off
    monkeypatch.setenv("AO_ENTITLED_PROVISIONING", "1")
    wid = pg.workspaces[0]
    uid, subject = await pg.person()
    await pg.seat(wid, uid, role="accountant", status=status)
    before = (await pg.sql(
        "SELECT * FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
        w=wid, u=uid,
    )).one()
    client = SimpleNamespace(fetch=AsyncMock(return_value=_answering(str(wid), "owner")))
    await onboarding.ensure_entitled_workspace(_signed_in(subject), "fictional", entitlement_client=client)
    client.fetch.assert_awaited_once_with("fictional")
    after = (await pg.sql(
        "SELECT * FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
        w=wid, u=uid,
    )).one()
    assert tuple(after) == tuple(before)
    assert await onboarding._first_active_workspace(uid) is None
    with pytest.raises(HTTPException) as exc:
        await tenancy._resolve_membership(subject, wid)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "org_role,setting,seated",
    [
        ("owner", None, "owner"),
        ("admin", None, "admin"),
        # An ordinary member of the organization is not let into its books by this path...
        ("member", None, None),
        # ...unless the deployment names the role they start with.
        ("member", "viewer", "viewer"),
    ],
)
async def test_flag_off_entitled_first_join_follows_the_one_seating_rule(
    pg_flag_off, monkeypatch, org_role, setting, seated
):
    pg = pg_flag_off
    monkeypatch.setenv("AO_ENTITLED_PROVISIONING", "1")
    if setting is not None:
        monkeypatch.setenv("PROVISIONING_MEMBER_ROLE", setting)
    wid = pg.workspaces[0]
    uid, subject = await pg.person()
    client = SimpleNamespace(fetch=AsyncMock(return_value=_answering(str(wid), org_role)))
    result = await onboarding.ensure_entitled_workspace(
        _signed_in(subject), "fictional", entitlement_client=client,
    )
    if seated is None:
        assert result.state == "not_entitled" and result.workspace_id is None
        assert await _membership(pg, wid, uid) is None
        with pytest.raises(HTTPException) as exc:
            await tenancy._resolve_membership(subject, wid)
        assert exc.value.status_code == 403
        return
    assert result.workspace_id == wid
    assert await _membership(pg, wid, uid) == (seated, "active")
    assert (await tenancy._resolve_membership(subject, wid))[0] == seated


async def test_work_inbox_excludes_inactive_books_and_totals(inactive_memberships):
    seats = inactive_memberships
    pg = seats.pg
    owner_maker = async_sessionmaker(pg.owner)
    seeded = []
    try:
        for wid in (seats.active, seats.revoked, seats.suspended):
            entity, approval, tax_review = uuid4(), uuid4(), uuid4()
            async with workspace_session(owner_maker, wid) as session:
                await session.execute(text("""
                    INSERT INTO entity (id, workspace_id, slug, display_name, kind)
                    VALUES (:id, :w, 'fictional', 'Fictional Entity', 'c_corp')
                """), {"id": entity, "w": wid})
                await session.execute(text("""
                    INSERT INTO approval_queue (
                        id, workspace_id, entity_id, subject_type, subject_id,
                        amount, currency_code, status, reasoning, requested_by_identity, payload
                    ) VALUES (
                        :id, :w, :e, 'bank_transaction_import', :subject,
                        123.45, 'USD', 'pending', 'Fictional pending review', 'alex@example.com',
                        CAST(:payload AS jsonb)
                    )
                """), {
                    "id": approval, "w": wid, "e": entity, "subject": uuid4(),
                    "payload": json.dumps({
                        "payee": "Fictional Vendor", "description": "Fictional expense",
                        "amount_cents": 12345,
                    }),
                })
                await session.execute(text("""
                    INSERT INTO tax_review_queue (
                        id, workspace_id, entity_id, tax_year, item_type, content,
                        confidence, status, submitter_kind, submitter_id
                    ) VALUES (
                        :id, :w, :e, 2025, 'memo', CAST(:content AS jsonb),
                        'low', 'pending', 'human', 'alex@example.com'
                    )
                """), {
                    "id": tax_review, "w": wid, "e": entity,
                    "content": json.dumps({"text": "Fictional confidential tax review"}),
                })
            seeded.append((wid, entity, approval, tax_review))
            await pg.seat(wid, seats.uid, role="accountant")

        # Positive control catches missing tables, RLS failures, or swallowed query errors.
        before = await work_inbox.work_inbox(max_total=100, max_per_workspace=100, auth=seats.auth)
        assert {item["id"] for item in before["items"]} == {
            str(item_id) for _, _, approval, tax in seeded for item_id in (approval, tax)
        }
        assert before["totals"]["total"] == 6
        actor = tenancy.WorkspaceContext(
            seats.revoked, "fictional", "owner", "owner@example.com", None, None, False,
        )
        await members.remove_member(seats.revoked, seats.uid, actor)
        await pg.seat(seats.suspended, seats.uid, role="accountant", status="suspended")
        after = await work_inbox.work_inbox(max_total=100, max_per_workspace=100, auth=seats.auth)
        assert {item["workspace_id"] for item in after["items"]} == {str(seats.active)}
        assert after["totals"] == {
            "total": 2,
            "by_item_type": {"approval": 1, "tax_review": 1},
            "by_workspace": [{
                "workspace_id": str(seats.active), "display_name": "Fictional Active Books", "count": 2,
            }],
        }
        assert not after["truncated"] and after["dropped"] == 0
    finally:
        # Delete only this test's fictional fixtures as the disposable DB owner.
        for wid, entity, approval, tax_review in seeded:
            async with workspace_session(owner_maker, wid) as session:
                await session.execute(text("DELETE FROM tax_review_queue WHERE id = :id"), {"id": tax_review})
                await session.execute(text("DELETE FROM approval_queue WHERE id = :id"), {"id": approval})
                await session.execute(text("DELETE FROM entity WHERE id = :id"), {"id": entity})


async def test_a_paused_owner_does_not_count_when_the_last_active_owner_is_removed(pg_flag_off):
    pg = pg_flag_off
    wid = pg.workspaces[0]
    owner_uid, _ = await pg.person()
    await pg.seat(wid, owner_uid, role="owner")
    paused_uid, _ = await pg.person()
    await pg.seat(wid, paused_uid, status="suspended", role="owner")
    # An administrator who is not the target: the refusal below is the owner count, not "yourself".
    actor = tenancy.WorkspaceContext(
        wid, "fictional", "owner", "another-owner@example.com", None, None, False
    )
    with pytest.raises(HTTPException) as exc:
        await members.remove_member(wid, owner_uid, actor)
    assert exc.value.status_code == 400 and "last owner" in exc.value.detail
    assert await _membership(pg, wid, owner_uid) == ("owner", "active")
    # With a second ACTIVE owner the same removal goes through.
    second_uid, _ = await pg.person()
    await pg.seat(wid, second_uid, role="owner")
    await members.remove_member(wid, owner_uid, actor)
    assert await _membership(pg, wid, owner_uid) == ("owner", "revoked")


async def test_a_removal_cancels_the_invitation_that_person_had_not_accepted(pg_flag_off, monkeypatch):
    from starlette.requests import Request

    pg = pg_flag_off
    wid, other_wid = pg.workspaces[0], pg.workspaces[1]
    uid, subject = await pg.person()
    await pg.sql("UPDATE public.user_account SET email = 'Robin@Example.com' WHERE id = :u", u=uid)
    await pg.seat(wid, uid, role="accountant")
    owner, owner_subject = await pg.person()
    await pg.seat(wid, owner, role="owner")
    # Three invitations nobody accepted: the removed person's for this workspace, the same
    # person's for another workspace, and somebody else's for this one. Only the first goes.
    token = "fictional-invitation-" + uuid4().hex
    mine, elsewhere, somebody = uuid4(), uuid4(), uuid4()
    for invitation, workspace_id, email, secret in (
        (mine, wid, "robin@example.com", token),
        (elsewhere, other_wid, "robin@example.com", "fictional-invitation-" + uuid4().hex),
        (somebody, wid, "sam@example.com", "fictional-invitation-" + uuid4().hex),
    ):
        await pg.sql(
            """
            INSERT INTO public.workspace_invitation
                (id, workspace_id, email, role, token_hash, expires_at, created_by_uc_uid)
            VALUES (:id, :w, :email, CAST('admin' AS workspace_role), :hash,
                    now() + interval '1 day', 'owner@example.com')
            """,
            id=invitation, w=workspace_id, email=email,
            hash=hashlib.sha256(secret.encode()).hexdigest(),
        )

    async def cancelled(invitation):
        return (await pg.sql(
            "SELECT cancelled_at IS NOT NULL FROM public.workspace_invitation WHERE id = :id",
            id=invitation,
        )).scalar_one()

    actor = tenancy.WorkspaceContext(wid, "fictional", "owner", owner_subject, None, None, False)
    await members.remove_member(wid, uid, actor)
    assert [await cancelled(mine), await cancelled(elsewhere), await cancelled(somebody)] == [
        True, False, False,
    ]
    audits = (await pg.sql(
        "SELECT invitation_id, target_user_id, actor_uc_uid FROM public.membership_audit "
        "WHERE workspace_id = :w AND action = 'invitation_cancel'", w=wid,
    )).all()
    assert [(str(row[0]), str(row[1]), row[2]) for row in audits] == [
        (str(mine), str(uid), owner_subject)
    ]
    # The old link does not bring the removed person back.
    caller = auth.AuthContext(
        subject, None, (), {"email": "robin@example.com", "email_verified": True}
    )
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=caller))
    monkeypatch.setattr(members, "_token_email_verified", lambda _: True)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer fictional")]})
    with pytest.raises(HTTPException) as exc:
        await members.accept_invitation(request=request, token=token)
    assert exc.value.status_code == 410
    assert await _membership(pg, wid, uid) == ("accountant", "revoked")
    # A second removal (the control plane's, say) finds nothing left to cancel and adds no receipt for it.
    await member_removal.remove_person(wid, subject=subject)
    assert len((await pg.sql(
        "SELECT 1 FROM public.membership_audit WHERE workspace_id = :w AND action = 'invitation_cancel'",
        w=wid,
    )).all()) == 1


# --- AO-RACE-TEST: two real connections racing the two last-owner/accept paths ---


class PausedAcceptSession(AsyncSession):
    """Holds the accept's transaction open right after its creator FOR SHARE
    read (signalled via `paused`, gated by `release`) so the test can commit
    a cancel inside the AO-RACE-1 window before the accept's guarded UPDATE
    runs. Only the FIRST FOR SHARE read pauses; later ones return at once."""

    def __init__(self, *args, paused: asyncio.Event, release: asyncio.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self._paused = paused
        self._release = release
        self._held = False

    async def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        result = await super().execute(statement, *args, **kwargs)
        if "FOR SHARE" in sql and not self._held:
            self._held = True
            self._paused.set()
            await asyncio.wait_for(self._release.wait(), 30)
        return result


async def test_accept_and_cancel_race_never_leaves_a_half_resolved_row(pg, monkeypatch):
    # AO-RACE-TEST-A: two real connections race accept vs cancel on the SAME
    # pending elevated invitation. The overlap is forced: the accept pauses
    # right after its creator FOR SHARE read (and STAYS there, holding its
    # transaction, until released), the cancel runs to COMMIT inside that
    # window, and only then is the accept released to hit its guarded UPDATE.
    # The guarded UPDATE must see the committed cancel: deterministically the
    # cancel wins and the accept 410s — the row can never end both cancelled
    # and accepted.
    from starlette.requests import Request

    await pg.create()
    wid = pg.workspaces[-1]
    owner_uid, owner_subject = await pg.person()
    await pg.seat(wid, owner_uid, role="owner")
    uid, subject = await pg.person()
    await pg.sql("UPDATE public.user_account SET email = 'robin@example.com' WHERE id = :u", u=uid)

    token = "fictional-invitation-" + uuid4().hex
    invitation = uuid4()
    await pg.sql(
        """
        INSERT INTO public.workspace_invitation
            (id, workspace_id, email, role, token_hash, expires_at, created_by_uc_uid)
        VALUES (:id, :w, 'robin@example.com', CAST('admin' AS workspace_role), :hash,
                now() + interval '1 day', :by)
    """,
        id=invitation,
        w=wid,
        hash=hashlib.sha256(token.encode()).hexdigest(),
        by=owner_subject,
    )
    caller = auth.AuthContext(
        subject, None, (), {"email": "robin@example.com", "email_verified": True}
    )
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=caller))
    monkeypatch.setattr(members, "_token_email_verified", lambda _: True)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer fictional")]})
    actor = tenancy.WorkspaceContext(
        wid, "fixture", "owner", owner_subject, None, None, False
    )

    accept_paused = asyncio.Event()
    accept_release = asyncio.Event()
    accept_maker = async_sessionmaker(
        pg.runtime, expire_on_commit=False, class_=PausedAcceptSession,
        paused=accept_paused, release=accept_release,
    )
    cancel_maker = async_sessionmaker(pg.runtime, expire_on_commit=False)
    # accept_invitation opens its session via the SessionLocal imported into
    # app.api.members (from app.db), so binding members.SessionLocal to the
    # pausing accept_maker puts the accept on its own connection. The cancel
    # runs on a separate connection via the db-level maker.
    monkeypatch.setattr(members, "SessionLocal", accept_maker)
    monkeypatch.setattr(db, "SessionLocal", cancel_maker)

    accept_task = asyncio.create_task(members.accept_invitation(request=request, token=token))
    await asyncio.wait_for(accept_paused.wait(), 30)
    # The accept is now HELD after its FOR SHARE read: run the cancel to full
    # COMPLETION (its UPDATE + commit) while the accept stays paused.
    cancel_result = await asyncio.wait_for(
        members.cancel_invitation(wid, invitation, actor), 30
    )
    accept_release.set()
    try:
        accept_result = await asyncio.wait_for(accept_task, 30)
        accept_error = None
    except BaseException as exc:  # noqa: BLE001 - a lost race raises the 410
        accept_result, accept_error = None, exc

    assert isinstance(cancel_result, dict) and cancel_result.get("ok") is True, (
        f"the cancel must succeed inside the forced window, got {cancel_result!r}"
    )
    assert isinstance(accept_error, HTTPException) and accept_error.status_code == 410, (
        "the accept lost to the committed cancel with 410 "
        f"(the guarded UPDATE's 0-row path), got result={accept_result!r} error={accept_error!r}"
    )
    row = (
        await pg.sql(
            "SELECT accepted_at, cancelled_at FROM public.workspace_invitation WHERE id = :id",
            id=invitation,
        )
    ).first()
    assert row.cancelled_at is not None and row.accepted_at is None, (
        "the invitation must end cancelled and never accepted, got "
        f"accepted_at={row.accepted_at!r} cancelled_at={row.cancelled_at!r}"
    )
    seats = (
        await pg.sql(
            "SELECT count(*) FROM public.workspace_membership "
            "WHERE workspace_id = :w AND user_id = :u AND status = 'active'",
            w=wid,
            u=uid,
        )
    ).scalar_one()
    assert seats == 0, "the losing accept must have left the person no active membership"


async def test_demote_and_remove_owner_race_does_not_deadlock(pg, monkeypatch):
    # AO-RACE-TEST (b): two connections race demote(T) vs remove(T) on the
    # same owner T (2 active owners). Neither may deadlock on the row locks;
    # both complete or one gets the existing 400, and at least one active
    # owner always remains.
    await pg.create()
    wid = pg.workspaces[-1]
    keeper_uid, _ = await pg.person()
    await pg.seat(wid, keeper_uid, role="owner")
    t_uid, t_subject = await pg.person()
    await pg.seat(wid, t_uid, role="owner")
    actor = tenancy.WorkspaceContext(
        wid, "fixture", "owner", "another-owner@example.com", None, None, False
    )

    demote_started = asyncio.Event()

    class DemoteSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            sql = str(statement)
            if sql.startswith("SELECT pg_advisory_xact_lock"):
                demote_started.set()
            return await super().execute(statement, *args, **kwargs)

    demote_maker = async_sessionmaker(pg.runtime, expire_on_commit=False, class_=DemoteSession)
    remove_maker = async_sessionmaker(pg.runtime, expire_on_commit=False)
    for module in (db, tenancy, members):
        monkeypatch.setattr(module, "SessionLocal", demote_maker)
    monkeypatch.setattr(member_removal, "SessionLocal", remove_maker)

    demote = asyncio.create_task(
        members.update_member(wid, t_uid, members.MemberPatch(role="viewer"), actor)
    )
    # Let the demote take its advisory lock so the race is real: the removal
    # starts only after the demote is holding the lock, so the removal's own
    # lock (the same key) blocks on it until the demote's transaction commits.
    await asyncio.wait_for(demote_started.wait(), 10)
    remove = asyncio.create_task(members.remove_member(wid, t_uid, actor))
    # Both must finish — no deadlock, no 500 — within the statement timeout.
    results = await asyncio.wait_for(
        asyncio.gather(demote, remove, return_exceptions=True), 30
    )
    deadlocks = [
        r for r in results
        if isinstance(r, OperationalError) and "deadlock" in str(r).lower()
    ]
    assert not deadlocks, f"the race deadlocked on the row locks: {deadlocks!r}"
    for r in results:
        if isinstance(r, HTTPException):
            assert r.status_code == 400, f"unexpected 4xx/5xx from the race: {r!r}"
    owners = (
        await pg.sql(
            """
            SELECT count(*) FROM public.workspace_membership
            WHERE workspace_id = :wid AND role IN ('owner', 'client_owner')
              AND status = 'active'
            """,
            wid=wid,
        )
    ).scalar_one()
    assert owners >= 1, "the race left the workspace with no active owner"
