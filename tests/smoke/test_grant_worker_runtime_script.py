"""String-level smoke tests for scripts/grant_worker_runtime.sql.

These tests do NOT connect to a live database. They parse the SQL file as
text and assert structural properties that catch the regressions we care about:

  - All worker tables have GRANT SELECT/INSERT/UPDATE (or SELECT/INSERT for
    append-only tables).
  - No table has GRANT DELETE or GRANT TRUNCATE.
  - All worker tables have REVOKE DELETE/TRUNCATE (belt-and-suspenders).
  - Every grant/revoke block is guarded by an IF EXISTS check (idempotency).

The list of worker tables is the source of truth for this test — if a table
is added to the script but not this list (or vice versa) the test fails, which
is the intended regression surface.
"""
from __future__ import annotations

import pathlib
import re

# ---------------------------------------------------------------------------
# Load the script once for the whole module.
# ---------------------------------------------------------------------------
_SCRIPT_PATH = (
    pathlib.Path(__file__).parent.parent.parent / "scripts" / "grant_worker_runtime.sql"
)


def _sql() -> str:
    return _SCRIPT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Worker tables and their expected grant sets.
# Matches GRANTS dict in the original 0004 migration + the SQL script.
# ---------------------------------------------------------------------------

# Tables that need SELECT, INSERT, UPDATE.
_SELECT_INSERT_UPDATE_TABLES = [
    "worker_runs",
    "worker_extractions",
    "worker_reconcile_runs",
]

# Tables that need only SELECT, INSERT (no UPDATE — neither service mutates them).
_SELECT_INSERT_TABLES = [
    "worker_dedup_index",
    "receipt_transaction_link",
]

_ALL_WORKER_TABLES = _SELECT_INSERT_UPDATE_TABLES + _SELECT_INSERT_TABLES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_block(sql: str, table: str) -> str:
    """Return the DO $$ ... END $$ block that mentions table (case-insensitive)."""
    # Find DO blocks that reference the table name.
    pattern = re.compile(
        r"DO\s+\$\$.*?END\s+\$\$\s*;",
        re.DOTALL | re.IGNORECASE,
    )
    blocks = [m.group() for m in pattern.finditer(sql) if table.lower() in m.group().lower()]
    assert blocks, f"No DO $$ block found for table {table!r} in grant_worker_runtime.sql"
    assert len(blocks) == 1, (
        f"Expected exactly 1 DO block for {table!r}, found {len(blocks)}"
    )
    return blocks[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_script_file_exists():
    assert _SCRIPT_PATH.exists(), f"Script not found at {_SCRIPT_PATH}"


def test_all_worker_tables_have_a_do_block():
    sql = _sql()
    for table in _ALL_WORKER_TABLES:
        _table_block(sql, table)  # raises AssertionError if absent


def test_select_insert_update_grants_present():
    sql = _sql()
    for table in _SELECT_INSERT_UPDATE_TABLES:
        block = _table_block(sql, table)
        assert re.search(
            rf"GRANT\s+SELECT\s*,\s*INSERT\s*,\s*UPDATE\s+ON\s+public\.{table}\s+TO\s+accounting_ops_app",
            block,
            re.IGNORECASE,
        ), f"Missing GRANT SELECT, INSERT, UPDATE on {table}"


def test_select_insert_only_grants_present():
    sql = _sql()
    for table in _SELECT_INSERT_TABLES:
        block = _table_block(sql, table)
        assert re.search(
            rf"GRANT\s+SELECT\s*,\s*INSERT\s+ON\s+public\.{table}\s+TO\s+accounting_ops_app",
            block,
            re.IGNORECASE,
        ), f"Missing GRANT SELECT, INSERT on {table}"


def test_no_grant_delete_on_any_worker_table():
    sql = _sql()
    for table in _ALL_WORKER_TABLES:
        block = _table_block(sql, table)
        # Match only GRANT lines (single-line: no DOTALL).  The REVOKE lines
        # in the same DO block mention DELETE/TRUNCATE intentionally — but a
        # REVOKE is not a GRANT, so we restrict to lines that start with GRANT.
        for line in block.splitlines():
            stripped = line.strip().upper()
            if not stripped.startswith("GRANT"):
                continue
            assert "DELETE" not in stripped, (
                f"GRANT DELETE found on line {line!r} in block for {table!r} — "
                "worker tables must never have DELETE granted"
            )


def test_no_grant_truncate_on_any_worker_table():
    sql = _sql()
    for table in _ALL_WORKER_TABLES:
        block = _table_block(sql, table)
        for line in block.splitlines():
            stripped = line.strip().upper()
            if not stripped.startswith("GRANT"):
                continue
            assert "TRUNCATE" not in stripped, (
                f"GRANT TRUNCATE found on line {line!r} in block for {table!r} — "
                "worker tables must never have TRUNCATE granted"
            )


def test_revoke_delete_truncate_present_on_all_worker_tables():
    sql = _sql()
    for table in _ALL_WORKER_TABLES:
        block = _table_block(sql, table)
        assert re.search(
            rf"REVOKE\s+DELETE\s*,\s*TRUNCATE\s+ON\s+public\.{table}\s+FROM\s+accounting_ops_app",
            block,
            re.IGNORECASE,
        ), f"Missing REVOKE DELETE, TRUNCATE on {table}"


def test_if_exists_guard_present_on_all_worker_tables():
    """Every DO block must check to_regclass(...) IS NOT NULL before granting.

    This ensures the script is safe to run before sibling migrations have
    created their tables (it skips rather than errors).
    """
    sql = _sql()
    for table in _ALL_WORKER_TABLES:
        block = _table_block(sql, table)
        assert re.search(
            r"to_regclass\s*\(.*IS\s+NOT\s+NULL",
            block,
            re.IGNORECASE | re.DOTALL,
        ), f"Missing IF EXISTS / to_regclass guard in block for {table!r}"


def test_no_other_tables_granted():
    """Only the known worker tables appear in GRANT statements.

    Catches accidental widening of the grant surface (e.g. a copy-paste
    that grants on a non-worker table).
    """
    sql = _sql()
    grant_table_re = re.compile(
        r"GRANT\s+\S.*?\s+ON\s+public\.(\w+)\s+TO",
        re.IGNORECASE,
    )
    granted_tables = {m.group(1).lower() for m in grant_table_re.finditer(sql)}
    expected = {t.lower() for t in _ALL_WORKER_TABLES}
    unexpected = granted_tables - expected
    assert not unexpected, (
        f"GRANT found on unexpected table(s): {unexpected!r}. "
        "Update _ALL_WORKER_TABLES in this test if the script was intentionally extended."
    )


def test_script_mentions_architecture_rationale():
    """The header comment must explain why this is outside Alembic.

    Prevents the comment from being stripped during a future edit,
    which would make the next person wonder why grants are in a random SQL file.
    """
    sql = _sql()
    assert "Alembic" in sql, "Script header must mention Alembic to explain the architecture choice"
    assert "sibling" in sql.lower(), "Script header must mention sibling repo ordering rationale"
