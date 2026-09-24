"""Integration proofs for the shared-schema RLS and append-only contract.

The tests use the least-privilege ``accounting_ops_app`` pool. The live-stack
fixtures skip the module when no compose stack (or runtime-role DSN) is
reachable, so this file remains safe to collect in the unit job.
"""
from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import TEST_WORKSPACE_ID

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


async def _set_books_workspace(conn: asyncpg.Connection, workspace_id: str) -> None:
    await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
    await conn.execute(
        "SELECT set_config('app.current_workspace', $1, true)", workspace_id
    )


@pytest.mark.asyncio
async def test_runtime_role_rls_is_shared_schema_default_deny(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    """The runtime role sees only its GUC workspace, and no rows without it."""
    async with app_db.acquire() as conn:
        async with conn.transaction():
            await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
            for table in _RLS_TABLES:
                rows = await conn.fetch(
                    f"SELECT DISTINCT workspace_id::text AS workspace_id FROM {table}"
                )
                assert {row["workspace_id"] for row in rows} <= {str(TEST_WORKSPACE_ID)}

            await conn.execute("SELECT set_config('app.current_workspace', '', true)")
            for table in _RLS_TABLES:
                assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 0


@pytest.mark.asyncio
async def test_runtime_role_cannot_mutate_append_only_books_tables(
    app_db: asyncpg.Pool, seeded_workspace
) -> None:
    """The migration's SELECT/INSERT-only grants reject both mutation verbs."""
    async with app_db.acquire() as conn:
        async with conn.transaction():
            await _set_books_workspace(conn, str(TEST_WORKSPACE_ID))
            for table in _APPEND_ONLY_TABLES:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(
                        f"UPDATE {table} SET workspace_id = workspace_id WHERE false"
                    )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(f"DELETE FROM {table} WHERE false")
