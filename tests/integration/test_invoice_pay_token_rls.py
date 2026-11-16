"""create_invoice_from_draft AS the runtime role (accounting_ops_app) under FORCE RLS.

Regression for: ``INSERT INTO public.invoice_pay_token ... ON CONFLICT DO NOTHING
RETURNING token`` needs the SELECT policy (``token = app.pay_token``) to read the
returned row back, so as the least-privilege role it failed with
"new row violates row-level security policy". Needs ACCTOPS_TEST_DSN (owner) and
ACCTOPS_TEST_APP_DSN (runtime role) on a Postgres with both migration chains at
head and a provisioned workspace with an entity; SKIPS otherwise.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ar.invoicing import create_invoice_from_draft
from app.workspace_session import workspace_session
from tests.conftest import StackConfig

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

pytestmark = pytest.mark.integration


def _sqla_url(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest_asyncio.fixture
async def owner(stack: StackConfig):
    if asyncpg is None:  # pragma: no cover
        pytest.skip("asyncpg not installed")
    try:
        pool = await asyncpg.create_pool(stack.dsn, min_size=1, max_size=2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable at ACCTOPS_TEST_DSN: {exc!r}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def runtime_sessionmaker(stack: StackConfig):
    if not stack.app_dsn:
        pytest.skip("ACCTOPS_TEST_APP_DSN unset")
    engine = create_async_engine(_sqla_url(stack.app_dsn))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def workspace(owner):
    rows = await owner.fetch(
        """
        SELECT w.id::text AS workspace_id, e.id::text AS entity_id
        FROM public.workspace w
        JOIN LATERAL (SELECT id FROM books.entity e WHERE e.workspace_id = w.id
                      ORDER BY e.created_at LIMIT 1) e ON true
        WHERE w.status = 'active' ORDER BY w.created_at LIMIT 2
        """
    )
    if not rows:
        pytest.skip("no provisioned workspace with an entity")
    return [dict(r) for r in rows]


async def _create(sm, ws: dict, ref: str) -> dict:
    async with workspace_session(sm, ws["workspace_id"]) as session:
        return await create_invoice_from_draft(
            session,
            workspace_id=ws["workspace_id"],
            entity_id=ws["entity_id"],
            actor_uc_uid="rls-test",
            external_source="rls-test",
            external_ref=ref,
            customer={"name": "Example Customer LLC", "email": "ap@example.com"},
            line_items=[{"description": "Consulting", "quantity": 2, "unit_price": "50.00"}],
        )


async def test_create_invoice_as_runtime_role_issues_pay_token(
    runtime_sessionmaker, owner, workspace
) -> None:
    ws = workspace[0]
    view = await _create(runtime_sessionmaker, ws, f"tok-{uuid.uuid4()}")
    assert view["id"]
    row = await owner.fetchrow(
        "SELECT t.token, t.workspace_id::text AS wid, i.public_token "
        "FROM public.invoice_pay_token t JOIN books.invoice i ON i.id = t.invoice_id "
        "WHERE t.invoice_id = $1::uuid",
        view["id"],
    )
    assert row is not None, "no pay-token row written"
    assert row["wid"] == ws["workspace_id"]
    assert row["public_token"] == row["token"]


async def test_runtime_role_still_cannot_enumerate_pay_tokens(
    runtime_sessionmaker, workspace
) -> None:
    """The SELECT policy is untouched: with no app.pay_token the role sees nothing."""
    await _create(runtime_sessionmaker, workspace[0], f"enum-{uuid.uuid4()}")
    async with runtime_sessionmaker() as session:
        async with session.begin():
            n = (await session.execute(text("SELECT count(*) FROM public.invoice_pay_token"))).scalar_one()
    assert n == 0
