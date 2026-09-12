"""RLS + grant acceptance evidence for the quotes-seam tables (review M12; contract §7.6).

For BOTH ``books.invoice_schedules`` and ``books.bill`` this proves, against a
live Postgres:

  CATALOG (owner pool)
    * ``relrowsecurity`` AND ``relforcerowsecurity`` are true;
    * the four 0026-shape policies ``<tbl>_ws_{sel,ins,upd,del}`` exist;
    * ``has_table_privilege('accounting_ops_app', <tbl>, 'DELETE')`` is false,
      TRUNCATE is false, SELECT / INSERT / UPDATE are true.

  BEHAVIOUR (runtime-role pool, i.e. AS ``accounting_ops_app``)
    * scoped to workspace A the role can INSERT and read back its own rows;
    * scoped to workspace B, or with the GUC UNSET, the same SELECT returns
      ZERO rows (cross-workspace read blocked, default-deny);
    * WITH CHECK: scoped to A it cannot INSERT a row whose workspace_id is B;
    * DELETE and TRUNCATE are refused (permission denied) — retirement is
      ``active=false`` / ``status='void'``, never a row deletion.

Runs against the docker itest stack (``make integration``) AND against any
Postgres that has the tenant chain at 0031+ with two provisioned workspaces —
it needs only ``ACCTOPS_TEST_DSN`` (owner) and ``ACCTOPS_TEST_APP_DSN``
(runtime role); no HTTP app is required. SKIPS cleanly when either DSN is
unreachable or fewer than two workspaces with an entity exist.

Recorded run (2026-09-12, throwaway local Postgres 14, tenant head 0031):
see the PR #13 comment "RLS evidence".
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio

try:
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]

from tests.conftest import StackConfig

pytestmark = pytest.mark.integration

_SCHEMA = "books"
RUNTIME_ROLE = "accounting_ops_app"
TABLES = ("invoice_schedules", "bill")


def _tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


@pytest_asyncio.fixture(scope="module")
async def owner(stack: StackConfig) -> AsyncIterator["asyncpg.Pool"]:
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


@pytest_asyncio.fixture(scope="module")
async def runtime(stack: StackConfig) -> AsyncIterator["asyncpg.Pool"]:
    if asyncpg is None:  # pragma: no cover
        pytest.skip("asyncpg not installed")
    if not stack.app_dsn:
        pytest.skip("ACCTOPS_TEST_APP_DSN unset — split runtime role not configured")
    try:
        pool = await asyncpg.create_pool(stack.app_dsn, min_size=1, max_size=2)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres not reachable as the runtime role: {exc!r}")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture(scope="module")
async def two_workspaces(owner: "asyncpg.Pool") -> list[dict]:
    """Two active workspaces that each have an entity, discovered from the DB."""
    for tbl in TABLES:
        exists = await owner.fetchval(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = $1 AND table_name = $2",
            _SCHEMA, tbl,
        )
        if not exists:
            pytest.skip(f"{_SCHEMA}.{tbl} missing — tenant chain below 0031_quotes_seam")
    rows = await owner.fetch(
        f"""
        SELECT w.id::text AS workspace_id, e.id::text AS entity_id
        FROM public.workspace w
        JOIN LATERAL (
            SELECT id FROM {_SCHEMA}.entity e WHERE e.workspace_id = w.id ORDER BY e.created_at LIMIT 1
        ) e ON true
        WHERE w.status = 'active'
        ORDER BY w.created_at
        LIMIT 2
        """
    )
    if len(rows) < 2:
        pytest.skip("need two provisioned workspaces with an entity each")
    return [dict(r) for r in rows]


# ==========================================================================
# 1. Catalog: FORCE RLS + the four policies + no DELETE/TRUNCATE for the runtime role
# ==========================================================================
@pytest.mark.parametrize("table", TABLES)
async def test_catalog_force_rls_policies_and_grants(owner: "asyncpg.Pool", table: str) -> None:
    rel = await owner.fetchrow(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = $1::regclass",
        f"{_SCHEMA}.{table}",
    )
    assert rel["relrowsecurity"] is True, f"{table}: RLS not enabled"
    assert rel["relforcerowsecurity"] is True, f"{table}: RLS not FORCEd (owner would bypass)"

    policies = {
        r["polname"]
        for r in await owner.fetch(
            "SELECT polname FROM pg_policy WHERE polrelid = $1::regclass", f"{_SCHEMA}.{table}"
        )
    }
    expected = {f"{table}_ws_{sfx}" for sfx in ("sel", "ins", "upd", "del")}
    assert expected <= policies, f"{table}: missing policies {expected - policies}"

    privs = {
        p: await owner.fetchval(
            "SELECT has_table_privilege($1, $2, $3)", RUNTIME_ROLE, f"{_SCHEMA}.{table}", p
        )
        for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
    }
    assert privs["SELECT"] and privs["INSERT"] and privs["UPDATE"], privs
    assert privs["DELETE"] is False, f"{table}: runtime role must not DELETE"
    assert privs["TRUNCATE"] is False, f"{table}: runtime role must not TRUNCATE"

    triggers = {
        r["tgname"]
        for r in await owner.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = $1::regclass AND NOT tgisinternal",
            f"{_SCHEMA}.{table}",
        )
    }
    assert f"{table}_no_truncate" in triggers, f"{table}: TRUNCATE guard trigger missing"


# ==========================================================================
# 2. Behaviour under the runtime role: isolation, WITH CHECK, no DELETE/TRUNCATE
# ==========================================================================
async def _scoped(conn: "asyncpg.Connection", workspace_id: str | None) -> None:
    await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
    if workspace_id is None:
        await conn.execute("SELECT set_config('app.current_workspace', '', true)")
    else:
        await conn.execute("SELECT set_config('app.current_workspace', $1, true)", workspace_id)


async def _insert_fixture_rows(conn: "asyncpg.Connection", ws: dict, tag: str) -> tuple[str, str]:
    """As the runtime role scoped to ``ws``: a supplier counterparty, a schedule and a bill."""
    cp_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO counterparty (id, workspace_id, display_name, kind, external_refs) "
        "VALUES ($1, $2, $3, 'vendor', $4::jsonb)",
        cp_id, ws["workspace_id"], f"RLS proof supplier {tag}", json.dumps({"rls_proof": tag}),
    )
    sched_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO invoice_schedules (
            id, workspace_id, entity_id, quote_ref, ar_customer_id, line_position,
            cadence, amount_cents, currency, description, anchor_date, net_terms,
            periods_total, periods_generated, next_run_at, generated_invoice_ids, active,
            created_by_uc_uid
        ) VALUES ($1, $2, $3, $4, $5, 1, 'month', 35000, 'USD', 'RLS proof hosting', $6, 30,
                  12, 1, $7, '[]'::jsonb, true, 'rls-proof')
        """,
        sched_id, ws["workspace_id"], ws["entity_id"], f"RLS-{tag} v1", cp_id,
        dt.date(2026, 9, 12), dt.datetime(2026, 10, 12, tzinfo=dt.timezone.utc),
    )
    bill_id = str(uuid.uuid4())
    await conn.execute(
        """
        INSERT INTO bill (
            id, workspace_id, entity_id, supplier_counterparty_id, bill_number, status, currency,
            issue_date, due_date, subtotal, tax_total, total, balance_due, lines, notes,
            quote_ref, external_source, external_ref, created_by_uc_uid
        ) VALUES ($1, $2, $3, $4, $5, 'draft', 'USD', $6, $7, 350, 0, 350, 350, '[]'::jsonb,
                  'RLS proof', $8, 'customer-ops', $9, 'rls-proof')
        """,
        bill_id, ws["workspace_id"], ws["entity_id"], cp_id, f"BILL-RLS-{tag}",
        dt.date(2026, 9, 12), dt.date(2026, 10, 12), f"RLS-{tag} v1", f"RLS-{tag}:partner",
    )
    return sched_id, bill_id


async def test_runtime_role_isolation_with_check_and_no_delete(
    owner: "asyncpg.Pool", runtime: "asyncpg.Pool", two_workspaces: list[dict]
) -> None:
    ws_a, ws_b = two_workspaces
    tag = _tag()
    try:
        # --- scoped to A: insert + read back ---
        async with runtime.acquire() as conn:
            async with conn.transaction():
                await _scoped(conn, ws_a["workspace_id"])
                sched_id, bill_id = await _insert_fixture_rows(conn, ws_a, tag)
                n_s = await conn.fetchval("SELECT count(*) FROM invoice_schedules WHERE id = $1", sched_id)
                n_b = await conn.fetchval("SELECT count(*) FROM bill WHERE id = $1", bill_id)
                assert (n_s, n_b) == (1, 1), "runtime role must read its own workspace's rows"

        # --- scoped to B: zero rows; GUC unset: zero rows (default-deny) ---
        for scope in (ws_b["workspace_id"], None):
            async with runtime.acquire() as conn:
                async with conn.transaction():
                    await _scoped(conn, scope)
                    n_s = await conn.fetchval("SELECT count(*) FROM invoice_schedules WHERE id = $1", sched_id)
                    n_b = await conn.fetchval("SELECT count(*) FROM bill WHERE id = $1", bill_id)
                    assert (n_s, n_b) == (0, 0), f"cross-workspace leak with scope={scope!r}"

        # --- WITH CHECK: scoped to A, cannot insert rows that claim workspace B ---
        async with runtime.acquire() as conn:
            async with conn.transaction():
                await _scoped(conn, ws_a["workspace_id"])
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(
                        """
                        INSERT INTO invoice_schedules (
                            workspace_id, entity_id, quote_ref, ar_customer_id, line_position, cadence,
                            amount_cents, currency, description, anchor_date, net_terms, created_by_uc_uid
                        ) SELECT $1, $2, 'RLS-cross v1', ar_customer_id, 99, 'month', 1, 'USD', 'x',
                                 anchor_date, 30, 'rls-proof'
                        FROM invoice_schedules WHERE id = $3
                        """,
                        ws_b["workspace_id"], ws_b["entity_id"], sched_id,
                    )
        async with runtime.acquire() as conn:
            async with conn.transaction():
                await _scoped(conn, ws_a["workspace_id"])
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(
                        """
                        INSERT INTO bill (
                            workspace_id, entity_id, supplier_counterparty_id, currency, issue_date, due_date,
                            external_source, external_ref, created_by_uc_uid
                        ) SELECT $1, $2, supplier_counterparty_id, 'USD', issue_date, due_date,
                                 'customer-ops', $3, 'rls-proof'
                        FROM bill WHERE id = $4
                        """,
                        ws_b["workspace_id"], ws_b["entity_id"], f"RLS-cross-{tag}", bill_id,
                    )

        # --- DELETE / TRUNCATE: permission denied for the runtime role ---
        for table, row_id in (("invoice_schedules", sched_id), ("bill", bill_id)):
            async with runtime.acquire() as conn:
                async with conn.transaction():
                    await _scoped(conn, ws_a["workspace_id"])
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await conn.execute(f"DELETE FROM {table} WHERE id = $1", row_id)
            async with runtime.acquire() as conn:
                async with conn.transaction():
                    await _scoped(conn, ws_a["workspace_id"])
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await conn.execute(f"TRUNCATE {table}")

        # --- the row is still there afterwards (nothing above deleted anything) ---
        async with runtime.acquire() as conn:
            async with conn.transaction():
                await _scoped(conn, ws_a["workspace_id"])
                assert await conn.fetchval("SELECT count(*) FROM bill WHERE id = $1", bill_id) == 1
    finally:
        # Owner cleanup of the proof rows (the owner role may itself be subject
        # to FORCE RLS, so scope explicitly; a superuser owner ignores it).
        async with owner.acquire() as conn:
            async with conn.transaction():
                await _scoped(conn, ws_a["workspace_id"])
                await conn.execute("DELETE FROM bill WHERE quote_ref LIKE $1", f"RLS-{tag} v%")
                await conn.execute("DELETE FROM invoice_schedules WHERE quote_ref LIKE $1", f"RLS-{tag} v%")
                await conn.execute(
                    "DELETE FROM counterparty WHERE external_refs->>'rls_proof' = $1", tag
                )
