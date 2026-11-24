"""credential_access_log + membership_audit are FORCE RLS (AO111-R2-F9, public 0023).

Proves on a real Postgres:
  * the catalog: both tables have RLS enabled AND forced, and keep their
    workspace_isolation policy;
  * as the runtime role (accounting_ops_app) with workspace A bound, an
    audit row of workspace B is invisible and inserting one for B is refused,
    while A's own insert (the app's normal path) still works;
  * when the owner DSN is a non-superuser owner (production shape), FORCE
    binds it too: no GUC -> 0 rows. A superuser owner (the CI compose stack)
    bypasses RLS by design, so that one check only runs when it means
    something; it never skips the test.

membership_audit is append-only for the runtime role (no DELETE grant), so
rows this test adds are removed through the owner with the GUC bound.
"""
from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio

from tests.conftest import StackConfig

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

pytestmark = pytest.mark.integration

TABLES = ("public.credential_access_log", "public.membership_audit")
ACTOR = "rls-audit-fixture@example.com"


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


async def _bound(conn, wid: uuid.UUID) -> None:
    await conn.execute("SELECT set_config('app.current_workspace', $1, true)", str(wid))


@pytest_asyncio.fixture
async def two_workspaces(pools):
    owner, _ = pools
    rows = await owner.fetch(
        "SELECT id FROM public.workspace WHERE status = 'active' ORDER BY created_at LIMIT 2"
    )
    if len(rows) < 2:
        pytest.skip("needs two provisioned workspaces (integration_env provisions A and B)")
    made = [r["id"] for r in rows]
    try:
        yield made
    finally:
        for wid in made:
            async with owner.acquire() as conn:
                async with conn.transaction():
                    await _bound(conn, wid)
                    await conn.execute(
                        "DELETE FROM public.membership_audit WHERE workspace_id = $1 "
                        "AND actor_uc_uid = $2",
                        wid, ACTOR,
                    )


async def _audit_insert(conn, wid: uuid.UUID) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO public.membership_audit (workspace_id, actor_uc_uid, action) "
        "VALUES ($1, $2, 'invitation_cancel') RETURNING id",
        wid, ACTOR,
    )


async def test_catalog_enable_force_and_policy(pools) -> None:
    owner, _ = pools
    for table in TABLES:
        rel = await owner.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
            table,
        )
        assert rel["relrowsecurity"] is True, table
        assert rel["relforcerowsecurity"] is True, f"{table}: RLS not FORCEd"
        names = {
            r["polname"]
            for r in await owner.fetch(
                "SELECT polname FROM pg_policy WHERE polrelid = $1::regclass", table
            )
        }
        assert "workspace_isolation" in names, table


async def test_runtime_role_sees_and_writes_only_its_workspace(pools, two_workspaces) -> None:
    owner, runtime = pools
    a, b = two_workspaces
    async with owner.acquire() as conn:
        async with conn.transaction():
            await _bound(conn, b)
            b_row = await _audit_insert(conn, b)
    async with runtime.acquire() as conn:
        async with conn.transaction():
            await _bound(conn, a)
            a_row = await _audit_insert(conn, a)
            mine = {
                r["id"]
                for r in await conn.fetch(
                    "SELECT id FROM public.membership_audit WHERE actor_uc_uid = $1", ACTOR
                )
            }
            assert a_row in mine and b_row not in mine
            assert await conn.fetchval(
                "SELECT count(*) FROM public.membership_audit WHERE id = $1", b_row
            ) == 0
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            async with conn.transaction():
                await _bound(conn, a)
                await _audit_insert(conn, b)


async def test_owner_without_guc_is_bound_unless_superuser(pools, two_workspaces) -> None:
    owner, runtime = pools
    a, _ = two_workspaces
    async with owner.acquire() as conn:
        async with conn.transaction():
            await _bound(conn, a)
            row = await _audit_insert(conn, a)
    assert await runtime.fetchval(
        "SELECT count(*) FROM public.membership_audit WHERE id = $1", row
    ) == 0
    is_super = await owner.fetchval(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    )
    if not is_super:
        for table in TABLES:
            assert await owner.fetchval(f"SELECT count(*) FROM {table}") == 0, table
