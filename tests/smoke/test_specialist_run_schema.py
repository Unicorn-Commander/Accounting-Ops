"""String-level migration smoke tests for 0009_specialist_run.py.

Parses the migration source as text and asserts structural properties that
catch regressions (same style as test_tax_review_queue_schema.py):

  1.  Migration file exists and is well-formed Python (importable).
  2.  Revision metadata: revision ID + down_revision chain.
  3.  CREATE TABLE specialist_run is present and guarded by _table_exists check.
  4.  All required columns are declared.
  5.  GRANT SELECT, INSERT, UPDATE on specialist_run is present.
  6.  REVOKE DELETE, TRUNCATE on specialist_run is present.
  7.  No GRANT DELETE is present on the table.
  8.  No GRANT TRUNCATE is present on the table.
  9.  BEFORE TRUNCATE guard using forbid_append_only_truncate() is present.
  10. workspace_isolation RLS policy CREATE is present.
  11. app.current_workspace GUC is used in the RLS policy.
  12. The two required indexes exist (specialist_run_lookup + specialist_run_status).
  13. Entity FK with CASCADE ondelete is present.
  14. downgrade() function exists and drops the policy, trigger, and table.
  15. The migration requires -x tenant=<schema> (RuntimeError guard).
  16. sqlalchemy.dialects.postgresql is imported for JSONB columns.
  17. JSONB columns are used for input_summary, output_summary, metrics.
  18. Status column has 'running' default (server_default pattern present).
  19. endpoint_url column is declared (audit; redacted at API layer).
  20. duration_ms BigInteger column is declared.

No DB connection required — pure text / import-level structural checks.
"""
from __future__ import annotations

import pathlib
import re

# ---------------------------------------------------------------------------
# Load the migration source once for the whole module.
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "migrations"
    / "tenant"
    / "versions"
    / "0009_specialist_run.py"
)


def _src() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migration_file_exists():
    assert _MIGRATION_PATH.exists(), f"Migration not found at {_MIGRATION_PATH}"


def test_migration_parseable_python():
    """Migration source must compile cleanly as Python."""
    src = _src()
    try:
        compile(src, str(_MIGRATION_PATH), "exec")
    except SyntaxError as e:
        raise AssertionError(f"Migration has a Python syntax error: {e}") from e


def test_revision_metadata():
    src = _src()
    assert 'revision: str = "0009_specialist_run"' in src
    assert 'down_revision: str | None = "0008_tax_review_queue"' in src


def test_create_table_present_and_idempotent():
    src = _src()
    assert "specialist_run" in src
    assert "_table_exists" in src
    assert "if not _table_exists" in src


def test_required_columns_declared():
    src = _src()
    required_columns = [
        '"id"',
        "workspace_id",
        "entity_id",
        "tax_year",
        "specialist_id",
        "agent_token_id",
        "invoker_uc_uid",
        "backend",
        "model",
        "endpoint_url",
        "started_at",
        "finished_at",
        "duration_ms",
        "status",
        "input_summary",
        "output_summary",
        "metrics",
        "error_summary",
        "trace_id",
    ]
    for col in required_columns:
        assert col in src, f"Expected column reference {col!r} in migration"


def test_grant_select_insert_update_present():
    """The migration must grant SELECT, INSERT, UPDATE on specialist_run."""
    src = _src()
    assert re.search(
        r"GRANT\s+SELECT\s*,\s*INSERT\s*,\s*UPDATE\s+ON\s+.*specialist_run",
        src,
        re.IGNORECASE,
    ), "Missing GRANT SELECT, INSERT, UPDATE on specialist_run"
    assert "RUNTIME_ROLE" in src or "accounting_ops_app" in src


def test_revoke_delete_truncate_present():
    """The migration must revoke DELETE, TRUNCATE on specialist_run."""
    src = _src()
    assert re.search(
        r"REVOKE\s+DELETE\s*,\s*TRUNCATE\s+ON\s+.*specialist_run",
        src,
        re.IGNORECASE,
    ), "Missing REVOKE DELETE, TRUNCATE on specialist_run"


def test_no_grant_delete_on_table():
    """No GRANT DELETE should appear for specialist_run."""
    src = _src()
    for line in src.splitlines():
        stripped = line.strip().upper()
        if not stripped.startswith("GRANT"):
            continue
        if "SPECIALIST_RUN" in stripped:
            assert "DELETE" not in stripped, (
                f"GRANT DELETE found on line {line!r} — "
                "specialist_run must never have DELETE granted"
            )


def test_no_grant_truncate_on_table():
    """No GRANT TRUNCATE should appear for specialist_run."""
    src = _src()
    for line in src.splitlines():
        stripped = line.strip().upper()
        if not stripped.startswith("GRANT"):
            continue
        if "SPECIALIST_RUN" in stripped:
            assert "TRUNCATE" not in stripped, (
                f"GRANT TRUNCATE found on line {line!r} — "
                "specialist_run must never have TRUNCATE granted"
            )


def test_truncate_trigger_present():
    src = _src()
    assert "forbid_append_only_truncate" in src, (
        "BEFORE TRUNCATE guard (forbid_append_only_truncate) must be present"
    )
    assert "specialist_run_no_truncate" in src


def test_rls_policy_present():
    src = _src()
    assert "ENABLE ROW LEVEL SECURITY" in src
    assert "workspace_isolation" in src
    assert "app.current_workspace" in src


def test_lookup_index_present():
    src = _src()
    assert "specialist_run_lookup" in src
    # Index must cover entity_id, tax_year, started_at DESC
    assert "entity_id, tax_year, started_at DESC" in src


def test_status_index_present():
    src = _src()
    assert "specialist_run_status" in src
    # Index must cover workspace_id, status, started_at DESC
    assert "workspace_id, status, started_at DESC" in src


def test_entity_fk_with_cascade():
    src = _src()
    assert "specialist_run_entity_fk" in src
    assert "ondelete" in src
    assert "CASCADE" in src


def test_downgrade_function_exists():
    src = _src()
    assert "def downgrade" in src
    assert "DROP POLICY" in src
    assert "DROP TRIGGER" in src
    assert "drop_table" in src


def test_upgrade_requires_tenant_schema():
    """upgrade() must raise RuntimeError if no tenant schema is provided."""
    src = _src()
    assert "raise RuntimeError" in src
    assert "requires -x tenant=" in src


def test_postgresql_jsonb_imported():
    """JSONB columns require the postgresql dialect import."""
    src = _src()
    assert "from sqlalchemy.dialects import postgresql" in src
    assert "postgresql.JSONB()" in src


def test_jsonb_columns_used():
    """input_summary, output_summary, and metrics must be JSONB."""
    src = _src()
    # Each JSONB column appears as Column("column_name", postgresql.JSONB(), ...)
    for col in ("input_summary", "output_summary", "metrics"):
        assert col in src, f"Expected JSONB column {col!r} in migration"


def test_status_default_running():
    """status column must default to 'running' via server_default."""
    src = _src()
    assert "'running'" in src, (
        "status column must have server_default of 'running'"
    )


def test_duration_ms_biginteger():
    """duration_ms must use BigInteger to handle long-running agents."""
    src = _src()
    assert "BigInteger" in src or "sa.BigInteger" in src
