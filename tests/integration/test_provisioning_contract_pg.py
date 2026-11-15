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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import auth, db, tenancy
from app.api import members, onboarding, provisioning, workspaces
from app.security import member_removal, provisioning_token, uc_api_key
from app.security.entitlements import EntitlementResult
from app.security.pat import generate_secret
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
                (id, owner_user_id, name, secret_hash, secret_prefix, scopes, workspace_scope)
            VALUES (:id, :u, 'Fictional test', :hash, :prefix, ARRAY['accounting:read'], :scope)
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
    for module in (
        db,
        tenancy,
        onboarding,
        provisioning,
        member_removal,
        provisioning_token,
        members,
        workspaces,
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
    "role,expected",
    [
        ("owner", "owner"),
        ("admin", "admin"),
        ("manager", "viewer"),
        ("member", "viewer"),
        ("billing_admin", "viewer"),
        ("unknown", "viewer"),
    ],
)
async def test_jit_role_comes_from_answer_not_claims(pg, role, expected):
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
    await onboarding.ensure_entitled_workspace(ctx, "fixture", entitlement_client=client)
    assert (
        await pg.sql(
            "SELECT role::text FROM public.workspace_membership WHERE workspace_id = :w AND user_id = :u",
            w=wid,
            u=uid,
        )
    ).scalar_one() == expected
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
