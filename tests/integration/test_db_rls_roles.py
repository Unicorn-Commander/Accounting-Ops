"""Integration proofs for shared-schema RLS and append-only grants.

The live-stack fixtures skip this module when no compose stack or runtime-role
DSN is reachable, so collection remains safe in the unit job.
"""
from __future__ import annotations

import datetime as dt

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_EXTERNAL_ACCOUNT_ID,
    TEST_SOURCE,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
)

pytestmark = pytest.mark.integration

_SCHEMA = "books"
_RLS_TABLES = ("journal_entry", "event_log", "posting_log")
_APPEND_ONLY_TABLES = (
    "journal_entry",
    "event_log",
    "posting_log",
    "decision_audit",
    "activity_event",
)
EFFECTIVE_DATE = dt.date(2026, 4, 1).isoformat()


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


async def _set_books_workspace(conn: asyncpg.Connection, workspace_id: str) -> None:
    await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
    await conn.execute(
        "SELECT set_config('app.current_workspace', $1, true)", workspace_id
    )


async def _post_auto_expense(client: httpx.AsyncClient, ext_id: str) -> str:
    response = await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
        json={
            "entity_id": str(TEST_ENTITY_ID),
            "source": TEST_SOURCE,
            "source_external_id": ext_id,
            "effective_date": EFFECTIVE_DATE,
            "amount_cents": -1500,
            "description": "Anthropic usage",
            "payee": "Anthropic",
            "memo": None,
            "category": "SaaS · AI",
            "category_status": "auto",
            "sign_matched": True,
            "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
            "user_confirmed": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["outcome"] == "posted", body
    return body["journal_entry_id"]


async def _count_table(
    conn: asyncpg.Connection, table: str, workspace_id: str | None
) -> int:
    async with conn.transaction():
        await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
        if workspace_id is not None:
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)", workspace_id
            )
        return await conn.fetchval(f"SELECT count(*) FROM {table}")


@pytest.mark.asyncio
async def test_app_posts_and_reads_under_runtime_role(
    client: httpx.AsyncClient, seeded_workspace
) -> None:
    entry_id = await _post_auto_expense(client, f"c3-runtime-ok-{_run_tag()}")
    response = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/{entry_id}"
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "posted"


@pytest.mark.asyncio
async def test_runtime_role_cannot_update_journal_entry(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace
) -> None:
    entry_id = await _post_auto_expense(client, f"c3-noupd-{_run_tag()}")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                await conn.execute(
                    "UPDATE journal_entry SET amount = amount + 1 WHERE id::text = $1",
                    entry_id,
                )


@pytest.mark.asyncio
async def test_runtime_role_cannot_delete_journal_entry(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace
) -> None:
    entry_id = await _post_auto_expense(client, f"c3-nodel-{_run_tag()}")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                await conn.execute("DELETE FROM journal_entry WHERE id::text = $1", entry_id)


@pytest.mark.asyncio
async def test_runtime_role_cannot_truncate_journal_entry(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                await conn.execute("TRUNCATE journal_entry")


@pytest.mark.asyncio
async def test_owner_truncate_journal_entry_is_blocked_by_trigger(
    db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with db.acquire() as conn:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                await conn.execute("TRUNCATE journal_entry CASCADE")
    assert "truncate is forbidden" in str(excinfo.value).lower(), excinfo.value


@pytest.mark.asyncio
async def test_owner_truncate_event_log_is_blocked_by_trigger(
    db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with db.acquire() as conn:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                await conn.execute("TRUNCATE event_log")
    assert "truncate is forbidden" in str(excinfo.value).lower(), excinfo.value


@pytest.mark.asyncio
async def test_runtime_role_cannot_disable_append_only_trigger(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                await conn.execute("ALTER TABLE journal_entry DISABLE TRIGGER USER")


@pytest.mark.asyncio
async def test_runtime_role_cannot_set_session_replication_role(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("SET session_replication_role = 'replica'")


@pytest.mark.asyncio
async def test_rls_correct_workspace_sees_rows(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace
) -> None:
    await _post_auto_expense(client, f"c3-rls-baseline-{_run_tag()}")
    async with app_db.acquire() as conn:
        for table in _RLS_TABLES:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                rows = await conn.fetch(
                    f"SELECT DISTINCT workspace_id::text AS workspace_id FROM {table}"
                )
            assert {row["workspace_id"] for row in rows} <= {str(TEST_WORKSPACE_ID)}
        assert await _count_table(conn, "journal_entry", str(TEST_WORKSPACE_ID)) > 0


@pytest.mark.asyncio
async def test_rls_unset_workspace_guc_sees_nothing(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace
) -> None:
    await _post_auto_expense(client, f"c3-rls-unset-{_run_tag()}")
    async with app_db.acquire() as conn:
        for table in _RLS_TABLES:
            assert await _count_table(conn, table, None) == 0


@pytest.mark.asyncio
async def test_rls_wrong_workspace_guc_sees_nothing(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace
) -> None:
    await _post_auto_expense(client, f"c3-rls-wrong-{_run_tag()}")
    async with app_db.acquire() as conn:
        for table in _RLS_TABLES:
            assert await _count_table(conn, table, str(TEST_WORKSPACE_B_ID)) == 0


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_read_with_wrong_workspace_guc(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace
) -> None:
    """Shared-schema replacement for the old wrong-search_path proof."""
    await _post_auto_expense(client, f"c3-rls-cross-tenant-{_run_tag()}")
    async with app_db.acquire() as conn:
        a_rows = await _count_table(conn, "journal_entry", str(TEST_WORKSPACE_ID))
        b_rows = await _count_table(conn, "journal_entry", str(TEST_WORKSPACE_B_ID))
    assert a_rows > 0, "workspace A must have rows for a non-vacuous isolation proof"
    assert b_rows == 0, "GUC=B must not reveal workspace A rows in shared books"


@pytest.mark.asyncio
async def test_rls_with_check_blocks_cross_workspace_insert(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
                await conn.execute(
                    """
                    INSERT INTO chart_of_account (
                        workspace_id, entity_id, account_number, account_name,
                        canonical_form, account_type, normal_balance
                    ) VALUES (
                        $1, $2, '9999', 'RLS smuggle attempt',
                        '9999·RLS smuggle attempt', 'expense', 'debit'
                    )
                    """,
                    str(TEST_WORKSPACE_B_ID),
                    str(TEST_ENTITY_ID),
                )
    msg = str(excinfo.value).lower()
    assert "row-level security" in msg or "row level security" in msg or "policy" in msg


@pytest.mark.asyncio
async def test_runtime_role_cannot_truncate_credential_access_log(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("TRUNCATE public.credential_access_log")


@pytest.mark.asyncio
async def test_runtime_role_cannot_update_or_delete_credential_access_log(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("UPDATE public.credential_access_log SET success = false")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("DELETE FROM public.credential_access_log")


@pytest.mark.asyncio
async def test_runtime_role_cannot_mutate_append_only_books_tables(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    async with app_db.acquire() as conn:
        async with conn.transaction():
            await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
            for table in _APPEND_ONLY_TABLES:
                # Each statement gets its own SAVEPOINT (asyncpg opens one
                # automatically for a `conn.transaction()` nested inside an
                # already-open transaction). Postgres aborts the WHOLE
                # transaction on any error, so without this, the first caught
                # InsufficientPrivilegeError would poison every statement
                # after it: the next `conn.execute` raises
                # InFailedSQLTransactionError instead — a different exception
                # that `pytest.raises(InsufficientPrivilegeError)` below does
                # NOT match, so it propagates and fails the test rather than
                # proving the DELETE was blocked. The SAVEPOINT rolls back
                # only the failed statement; `_set_books_workspace`'s SET
                # LOCAL applies for the whole outer transaction and survives
                # a ROLLBACK TO SAVEPOINT that happened after it was set.
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(
                            f"UPDATE {table} SET workspace_id = workspace_id WHERE false"
                        )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(f"DELETE FROM {table} WHERE false")
