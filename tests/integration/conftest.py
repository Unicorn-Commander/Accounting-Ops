"""Integration fixtures: live-stack gate, HTTP client, DB pool, seeding.

Layering (deliberate):
  * Stack standup + DB migrations + workspace provisioning are done OUTSIDE
    pytest by scripts/integration_env.py (invoked by `make integration` and
    by CI). Those need alembic + the app package and are infra concerns.
  * These fixtures own the *test-data* setup that uses the real product
    surface: insert the workspace_membership, seed the chart of accounts via
    POST /coa/seed, link COA 1010 to the SimpleFIN account via POST /coa/link,
    and register the account_entity_mapping row. All idempotent.

Every fixture SKIPS (never errors) when the stack isn't reachable so the
suite stays green on a machine without docker.

Shared provisioning helpers (ENTITY_B_ID, _ensure_entity_b, _set_membership)
are defined here so they can be reused by both test_tenant_isolation.py and
test_h2_service_token_scope.py without duplication.
"""
from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio

try:
    import asyncpg
except ImportError:  # pragma: no cover - asyncpg is a core dep, present in app img
    asyncpg = None  # type: ignore[assignment]

import httpx

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_EXTERNAL_ACCOUNT_ID,
    TEST_SOURCE,
    TEST_WORKSPACE_ID,
    StackConfig,
)

# Shared-schema model (tenant/0025+0026): every workspace's rows live in the ONE
# fixed `books` schema, isolated by FORCE RLS on workspace_id — the old
# per-workspace `accounting_ops_<uuid>` schema is gone. Mirrors
# app.workspace_session.SHARED_SCHEMA; the seed/tamper helpers below still SET
# search_path to it explicitly so the OWNER pool lands in `books`.
_SCHEMA = "books"

# ---------------------------------------------------------------------------
# Shared second-entity constants (reused across test_tenant_isolation.py and
# test_h2_service_token_scope.py). Keep the slug/ID in one place.
# ---------------------------------------------------------------------------
ENTITY_B_ID = uuid.uuid5(
    uuid.NAMESPACE_DNS, f"{TEST_WORKSPACE_ID}:entity:isolation-entity-b"
)
ENTITY_B_SLUG = "isolation-entity-b"
ENTITY_B_EXTERNAL_ACCOUNT = "ACC-isolation-entity-b-0002"


async def _ensure_entity_b(pool: "asyncpg.Pool") -> None:
    """Create entity B in the workspace schema (idempotent on slug)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute(
                """
                INSERT INTO entity (
                    id, workspace_id, slug, display_name, kind,
                    country_code, functional_currency, status
                ) VALUES (
                    $1, $2, $3, 'Isolation Entity B', CAST('llc' AS entity_type),
                    'US', 'USD', 'active'
                )
                ON CONFLICT (workspace_id, slug) DO NOTHING
                """,
                str(ENTITY_B_ID),
                str(TEST_WORKSPACE_ID),
                ENTITY_B_SLUG,
            )


async def _ensure_entity_b_mapping(pool: "asyncpg.Pool") -> None:
    """Register entity B's external bank account mapping (idempotent)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute(
                """
                INSERT INTO account_entity_mapping (
                    id, workspace_id, entity_id, source, external_account_id,
                    external_org, external_account_name, auto_categorize
                ) VALUES (
                    gen_random_uuid(), $1, $2, $3, $4,
                    'Isolation B Bank', 'Isolation B Checking (0002)', true
                )
                ON CONFLICT (workspace_id, source, external_account_id) DO NOTHING
                """,
                str(TEST_WORKSPACE_ID),
                str(ENTITY_B_ID),
                TEST_SOURCE,
                ENTITY_B_EXTERNAL_ACCOUNT,
            )


async def _set_membership(
    pool: "asyncpg.Pool",
    uc_uid: str,
    role: str,
    entity_scope: list[str] | None,
) -> tuple[str, list[str] | None]:
    """Set (role, entity_scope) for uc_uid's membership; return the previous values."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            prev = await conn.fetchrow(
                """
                SELECT m.role::text AS role, m.entity_scope
                FROM public.workspace_membership m
                JOIN public.user_account u ON u.id = m.user_id
                WHERE u.uc_uid = $1 AND m.workspace_id = $2
                """,
                uc_uid,
                str(TEST_WORKSPACE_ID),
            )
            assert prev is not None, "membership not found — seeded_workspace must run first"
            scope_json = json.dumps(entity_scope) if entity_scope is not None else None
            await conn.execute(
                """
                UPDATE public.workspace_membership m
                SET role = CAST($1 AS public.workspace_role),
                    entity_scope = $2::jsonb
                FROM public.user_account u
                WHERE u.id = m.user_id AND u.uc_uid = $3
                  AND m.workspace_id = $4
                """,
                role,
                scope_json,
                uc_uid,
                str(TEST_WORKSPACE_ID),
            )
    prev_scope = prev["entity_scope"]
    if isinstance(prev_scope, str):
        prev_scope = json.loads(prev_scope)
    return prev["role"], prev_scope


# --------------------------------------------------------------------------
# Stack reachability gate
# --------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="session")
async def live_stack(stack: StackConfig) -> StackConfig:
    """Skip the whole integration tier unless the app answers /healthz."""
    if not stack.token:
        pytest.skip(
            "ACCTOPS_TEST_TOKEN is unset — integration stack not configured. "
            "Run `make integration` (which sets it) or export it manually."
        )
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{stack.base_url}/healthz")
            r.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any failure = stack not up = skip
        pytest.skip(f"App not reachable at {stack.base_url}/healthz: {exc!r}")
    return stack


@pytest_asyncio.fixture(scope="session")
async def db(live_stack: StackConfig) -> AsyncIterator["asyncpg.Pool"]:
    """asyncpg pool as the OWNER role. Used for setup + tamper injection.

    The owner can DISABLE TRIGGER (the tamper test relies on it) and bypasses
    RLS by default — so this pool sees all rows. It models an attacker with raw
    owner-level DB access, which is the only path by which the ledger can be
    tampered (the app path is UPDATE-proof).
    """
    if asyncpg is None:  # pragma: no cover
        pytest.skip("asyncpg not installed")
    try:
        pool = await asyncpg.create_pool(live_stack.dsn, min_size=1, max_size=4)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable at the test DSN: {exc!r}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(scope="session")
async def app_db(live_stack: StackConfig) -> AsyncIterator["asyncpg.Pool"]:
    """asyncpg pool as the LEAST-PRIVILEGE runtime role (accounting_ops_app).

    Used by the C3 proofs: this role must NOT be able to UPDATE/DELETE or
    TRUNCATE the append-only ledger, must NOT be able to DISABLE its trigger,
    and is subject to RLS (a wrong/unset app.current_workspace yields zero
    rows). SKIPS when the split-role stack isn't configured (ACCTOPS_TEST_APP_DSN
    unset), e.g. an older single-role stack — so the suite stays green there.
    """
    if asyncpg is None:  # pragma: no cover
        pytest.skip("asyncpg not installed")
    if not live_stack.app_dsn:
        pytest.skip(
            "ACCTOPS_TEST_APP_DSN unset — split runtime role not configured. "
            "Run `make integration` (it provisions accounting_ops_app)."
        )
    try:
        pool = await asyncpg.create_pool(live_stack.app_dsn, min_size=1, max_size=4)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable as the runtime role: {exc!r}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def client(live_stack: StackConfig) -> AsyncIterator[httpx.AsyncClient]:
    """Authenticated HTTP client bound to the app, scoped to the test workspace path."""
    headers = {"Authorization": f"Bearer {live_stack.token}"}
    async with httpx.AsyncClient(
        base_url=live_stack.base_url, headers=headers, timeout=30
    ) as c:
        yield c


# --------------------------------------------------------------------------
# Provisioning guard + test-data seeding
# --------------------------------------------------------------------------
async def _schema_exists(pool: "asyncpg.Pool") -> bool:
    row = await pool.fetchrow(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1", _SCHEMA
    )
    return row is not None


async def _entity_exists(pool: "asyncpg.Pool") -> bool:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT 1 FROM entity WHERE id::text = $1", str(TEST_ENTITY_ID)
            )
    return row is not None


async def _ensure_membership(pool: "asyncpg.Pool", uc_uid: str) -> None:
    """Insert (idempotently) a public.user_account + owner workspace_membership.

    resolve_workspace() auto-creates the user_account but NOT the membership;
    without an owner/admin/accountant membership the service token gets 403.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            user_id = await conn.fetchval(
                """
                INSERT INTO public.user_account (id, uc_uid, is_active, created_at)
                VALUES (gen_random_uuid(), $1, true, now())
                ON CONFLICT (uc_uid) DO UPDATE SET is_active = true
                RETURNING id
                """,
                uc_uid,
            )
            await conn.execute(
                """
                INSERT INTO public.workspace_membership (id, workspace_id, user_id, role, created_at)
                VALUES (gen_random_uuid(), $1, $2, 'owner', now())
                ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = 'owner'
                """,
                str(TEST_WORKSPACE_ID),
                user_id,
            )


async def _ensure_account_mapping(pool: "asyncpg.Pool") -> None:
    """Register the SimpleFIN account -> entity mapping (idempotent).

    The journal-posting endpoint reads this to decide entity_mapped=True.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute(
                """
                INSERT INTO account_entity_mapping (
                    id, workspace_id, entity_id, source, external_account_id,
                    external_org, external_account_name, auto_categorize
                ) VALUES (
                    gen_random_uuid(), $1, $2, $3, $4,
                    'Integration Test Bank', 'Integration Checking (0001)', true
                )
                ON CONFLICT (workspace_id, source, external_account_id) DO NOTHING
                """,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
                TEST_SOURCE,
                TEST_EXTERNAL_ACCOUNT_ID,
            )


@pytest_asyncio.fixture(scope="session")
async def seeded_workspace(
    live_stack: StackConfig, db: "asyncpg.Pool"
) -> StackConfig:
    """Bring the test workspace to a fully-seeded, post-ready state.

    Preconditions (done by scripts/integration_env.py before pytest):
      - public schema migrated
      - workspace provisioned: schema created, registry row inserted, tenant
        migrations applied, the test entity created.

    This fixture (using the real API + DB):
      1. inserts the owner membership for the token's uc_uid
      2. POST /coa/seed       -> default chart of accounts for the entity
      3. POST /coa/link        -> link COA 1010 to the SimpleFIN account
      4. inserts account_entity_mapping for that SimpleFIN account
    """
    if not await _schema_exists(db):
        pytest.skip(
            f"Workspace schema {_SCHEMA} is missing. Run `make integration` "
            "(or scripts/integration_env.py provision) to migrate + provision first."
        )
    if not await _entity_exists(db):
        pytest.skip(
            f"Test entity {TEST_ENTITY_ID} not provisioned in {_SCHEMA}. "
            "Run the provisioning step (scripts/integration_env.py provision)."
        )

    await _ensure_membership(db, live_stack.uc_uid)

    headers = {"Authorization": f"Bearer {live_stack.token}"}
    async with httpx.AsyncClient(
        base_url=live_stack.base_url, headers=headers, timeout=30
    ) as c:
        # Seed the chart of accounts (idempotent server-side).
        r = await c.post(f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/seed")
        assert r.status_code == 200, f"coa/seed failed: {r.status_code} {r.text}"

        # Link COA 1010 (Cash · Primary Checking) to the SimpleFIN account so
        # the posting path can resolve the cash side of each entry.
        r = await c.post(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/link",
            json={
                "entity_id": str(TEST_ENTITY_ID),
                "account_number": "1010",
                "source": TEST_SOURCE,
                "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
            },
        )
        assert r.status_code == 200, f"coa/link failed: {r.status_code} {r.text}"

    await _ensure_account_mapping(db)
    return live_stack


# --------------------------------------------------------------------------
# Helpers reused across tests
# --------------------------------------------------------------------------
async def fetch_chain_rows(pool: "asyncpg.Pool", entity_id: uuid.UUID) -> list:
    """Return journal_entry rows for one entity, oldest-first (chain order)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            return await conn.fetch(
                """
                SELECT id::text AS id, amount, debit_account, credit_account,
                       payee, description, effective_date, posted_at,
                       prev_hash, this_hash, status
                FROM journal_entry
                WHERE entity_id::text = $1 AND this_hash IS NOT NULL
                ORDER BY posted_at ASC, id ASC
                """,
                str(entity_id),
            )
