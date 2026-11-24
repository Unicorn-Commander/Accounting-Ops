"""public.workspace_invitation under ENABLE + FORCE RLS (AO-RLS-1, public 0022).

Before 0022 the table had workspace_id but no row-level security: only the
app's ``WHERE workspace_id = :wid`` filters isolated it. These tests run the
real members.py code AS the runtime role (accounting_ops_app) and prove:

  * the catalog: RLS enabled AND forced, the five per-command policies;
  * a cross-workspace SELECT / UPDATE as accounting_ops_app returns 0 rows,
    and a cross-workspace INSERT is refused;
  * no workspace GUC -> no rows at all (default deny);
  * create -> list -> cancel through members.py still work in their own
    workspace, and cancel cannot touch another workspace's invitation;
  * accept-by-token (no workspace context yet) still resolves, both the
    unauthenticated "needs_auth" path and the authenticated accept;
  * when the owner DSN is a non-superuser owner (the production shape),
    FORCE binds it too: no GUC -> 0 rows. A superuser owner (the CI compose
    stack) bypasses RLS by design, so that one check only runs when it means
    something; it never skips the test.

Needs ACCTOPS_TEST_DSN (owner) and ACCTOPS_TEST_APP_DSN (runtime role) on a
Postgres with the public chain at head and two provisioned workspaces (the
integration stack's A and B). Adds fictional invitations and removes them.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app import auth
from app.api import members
from app.tenancy import WorkspaceContext
from tests.conftest import StackConfig

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

pytestmark = pytest.mark.integration

T = "public.workspace_invitation"


def _sqla_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest_asyncio.fixture
async def pools(stack: StackConfig):
    if asyncpg is None:  # pragma: no cover
        pytest.skip("asyncpg not installed")
    if not os.getenv("ACCTOPS_TEST_DSN") or not stack.app_dsn:
        pytest.skip("requires ACCTOPS_TEST_DSN (owner) and ACCTOPS_TEST_APP_DSN (runtime)")
    owner = await asyncpg.create_pool(stack.dsn, min_size=1, max_size=2)
    runtime = await asyncpg.create_pool(stack.app_dsn, min_size=1, max_size=2)
    try:
        assert await runtime.fetchval("SELECT current_user") == "accounting_ops_app"
        yield owner, runtime
    finally:
        await runtime.close()
        await owner.close()


async def _owner_insert_invitation(owner, wid: uuid.UUID, email: str, token: str) -> uuid.UUID:
    iid = uuid.uuid4()
    async with owner.acquire() as conn:
        async with conn.transaction():
            # Bind the GUC: under FORCE a non-superuser owner is filtered too.
            await conn.execute("SELECT set_config('app.current_workspace', $1, true)", str(wid))
            await conn.execute(
                f"""
                INSERT INTO {T} (id, workspace_id, email, role, token_hash, expires_at,
                                 created_by_uc_uid)
                VALUES ($1, $2, $3, CAST('viewer' AS workspace_role), $4,
                        now() + interval '1 day', 'owner@example.com')
                """,
                iid, wid, email, hashlib.sha256(token.encode()).hexdigest(),
            )
    return iid


@pytest_asyncio.fixture
async def two_workspaces(pools):
    owner, _ = pools
    rows = await owner.fetch(
        "SELECT id FROM public.workspace WHERE status = 'active' ORDER BY created_at LIMIT 2"
    )
    if len(rows) < 2:
        pytest.skip("needs two provisioned workspaces (integration_env provisions A and B)")
    made = [r["id"] for r in rows]
    tokens = {wid: f"fictional-invite-token-{uuid.uuid4().hex}" for wid in made}
    invites = {
        wid: await _owner_insert_invitation(owner, wid, f"guest-{uuid.uuid4().hex[:6]}@example.com",
                                            tokens[wid])
        for wid in made
    }
    try:
        yield made, invites, tokens
    finally:
        # Remove what this test added: its invitations (by fictional creator /
        # email) and any membership its accept created. membership_audit is
        # append-only and keeps its rows, as in the other provisioning tests.
        for wid in made:
            async with owner.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.current_workspace', $1, true)", str(wid)
                    )
                    await conn.execute(
                        f"DELETE FROM {T} WHERE workspace_id = $1 AND (id = $2 "
                        "OR email IN ('new-guest@example.com', 'robin@example.com'))",
                        wid, invites[wid],
                    )
        await owner.execute(
            "DELETE FROM public.workspace_membership m USING public.user_account u "
            "WHERE u.id = m.user_id AND u.uc_uid LIKE 'rls-invite-%' "
            "AND m.workspace_id = ANY($1::uuid[])",
            made,
        )


@pytest.fixture
def runtime_session(monkeypatch, stack: StackConfig):
    engine = create_async_engine(_sqla_url(stack.app_dsn))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(members, "SessionLocal", maker)
    yield maker


def _ctx(wid: uuid.UUID, role: str = "owner") -> WorkspaceContext:
    return WorkspaceContext(wid, "fixture", role, "owner@example.com", None, None, False)


async def test_catalog_enable_force_and_policies(pools) -> None:
    owner, _ = pools
    rel = await owner.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass", T
    )
    assert rel["relrowsecurity"] is True
    assert rel["relforcerowsecurity"] is True, "RLS not FORCEd (owner would bypass)"
    names = {
        r["polname"]
        for r in await owner.fetch("SELECT polname FROM pg_policy WHERE polrelid = $1::regclass", T)
    }
    assert names == {
        "workspace_invitation_ws_sel",
        "workspace_invitation_by_token",
        "workspace_invitation_ws_ins",
        "workspace_invitation_ws_upd",
        "workspace_invitation_ws_del",
    }


async def test_runtime_role_cross_workspace_select_update_insert(pools, two_workspaces) -> None:
    _, runtime = pools
    (a, b), invites, _ = two_workspaces
    async with runtime.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_workspace', $1, true)", str(a))
            seen = {r["id"] for r in await conn.fetch(f"SELECT id FROM {T}")}
            assert seen == {invites[a]}
            # Even naming B's row explicitly yields nothing.
            assert await conn.fetchval(f"SELECT count(*) FROM {T} WHERE id = $1", invites[b]) == 0
            assert await conn.fetchval(
                f"SELECT count(*) FROM {T} WHERE workspace_id = $1", b
            ) == 0
            status = await conn.execute(
                f"UPDATE {T} SET cancelled_at = now() WHERE id = $1", invites[b]
            )
            assert status == "UPDATE 0"
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.current_workspace', $1, true)", str(a))
                await conn.execute(
                    f"""
                    INSERT INTO {T} (workspace_id, email, role, token_hash, expires_at,
                                     created_by_uc_uid)
                    VALUES ($1, 'x@example.com', CAST('viewer' AS workspace_role), 'h',
                            now() + interval '1 day', 'x')
                    """,
                    b,
                )
    owner, _ = pools
    async with owner.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_workspace', $1, true)", str(b))
            assert await conn.fetchval(f"SELECT cancelled_at FROM {T} WHERE id = $1", invites[b]) is None


async def test_no_workspace_guc_sees_nothing(pools, two_workspaces) -> None:
    owner, runtime = pools
    (a, b), _, _ = two_workspaces
    assert await runtime.fetchval(
        f"SELECT count(*) FROM {T} WHERE workspace_id = ANY($1::uuid[])", [a, b]
    ) == 0
    is_super = await owner.fetchval(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if not is_super:
        # Production shape: the non-superuser table owner is bound by FORCE.
        assert await owner.fetchval(
            f"SELECT count(*) FROM {T} WHERE workspace_id = ANY($1::uuid[])", [a, b]
        ) == 0


async def test_members_create_list_cancel_stay_in_their_workspace(
    pools, two_workspaces, runtime_session
) -> None:
    (a, b), invites, _ = two_workspaces
    created = await members.create_invitation(
        a,
        members.InvitationCreate(email="new-guest@example.com", role="viewer",
                                 delivery_mode="manual_link"),
        workspace=_ctx(a),
    )
    assert created["status"] == "pending"
    listed = {row["id"] for row in await members.list_invitations(a, False, workspace=_ctx(a))}
    assert listed == {str(invites[a]), created["id"]}
    # Cancelling B's invitation from A's context is a 404, and B is untouched.
    with pytest.raises(HTTPException) as exc:
        await members.cancel_invitation(a, invites[b], workspace=_ctx(a))
    assert exc.value.status_code == 404
    assert {row["id"] for row in await members.list_invitations(b, False, workspace=_ctx(b))} == {
        str(invites[b])
    }
    assert (await members.cancel_invitation(a, uuid.UUID(created["id"]), workspace=_ctx(a)))["ok"]


async def test_accept_by_token_still_works(pools, two_workspaces, runtime_session, monkeypatch) -> None:
    owner, _ = pools
    (a, _b), _, _ = two_workspaces
    created = await members.create_invitation(
        a,
        members.InvitationCreate(email="robin@example.com", role="bookkeeper",
                                 delivery_mode="manual_link"),
        workspace=_ctx(a),
    )
    token = parse_qs(urlsplit(created["delivery"]["accept_url"]).query)["token"][0]

    # 1. No bearer: the token alone resolves the invitation (needs_auth).
    anon = Request({"type": "http", "headers": []})
    reply = await members.accept_invitation(request=anon, token=token)
    assert reply["status"] == "needs_auth"
    assert reply["role"] == "bookkeeper"

    # 2. A wrong token resolves nothing (constant 404).
    with pytest.raises(HTTPException) as exc:
        await members.accept_invitation(request=anon, token="fictional-wrong-token-0000")
    assert exc.value.status_code == 404

    # 3. Authenticated accept marks it accepted and seats the member.
    subject = f"rls-invite-{uuid.uuid4().hex[:8]}"
    caller = auth.AuthContext(subject, None, (), {"email": "robin@example.com", "email_verified": True})
    monkeypatch.setattr(auth, "authenticate_token", AsyncMock(return_value=caller))
    monkeypatch.setattr(members, "_token_email_verified", lambda claims: True)
    bearer = Request({"type": "http", "headers": [(b"authorization", b"Bearer fictional")]})
    accepted = await members.accept_invitation(request=bearer, token=token)
    assert accepted["status"] == "accepted"
    async with owner.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_workspace', $1, true)", str(a))
            row = await conn.fetchrow(
                f"SELECT accepted_at, accepted_by_user_id FROM {T} WHERE id = $1",
                uuid.UUID(created["id"]),
            )
    assert row["accepted_at"] is not None and row["accepted_by_user_id"] is not None
    role = await owner.fetchval(
        "SELECT m.role::text FROM public.workspace_membership m "
        "JOIN public.user_account u ON u.id = m.user_id "
        "WHERE m.workspace_id = $1 AND u.uc_uid = $2",
        a, subject,
    )
    assert role == "bookkeeper"
