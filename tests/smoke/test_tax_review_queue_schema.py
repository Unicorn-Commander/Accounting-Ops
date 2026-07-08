"""String-level migration smoke test for 0008_tax_review_queue.py.

Parses the migration source as text and asserts structural properties that
catch regressions we care about (same style as test_grant_worker_runtime_script.py):

  1. The migration file exists and is well-formed Python.
  2. CREATE TABLE tax_review_queue is present (idempotent guard).
  3. All required columns are declared.
  4. GRANT SELECT, INSERT, UPDATE on tax_review_queue is present.
  5. REVOKE DELETE, TRUNCATE on tax_review_queue is present.
  6. The BEFORE TRUNCATE trigger using forbid_append_only_truncate() is present.
  7. The workspace_isolation RLS policy CREATE is present.
  8. The revision chain is correct (down_revision = 0007_closed_period).
  9. The two required indexes are created (entity_year_status + workspace_status_created).
  10. No DELETE or TRUNCATE grant is present on the table.
  11. The downgrade function exists and references the same objects.

No DB connection is required — these are import-level structural checks.
"""
from __future__ import annotations

import pathlib
import re

# ---------------------------------------------------------------------------
# Load the migration once for the whole module.
# ---------------------------------------------------------------------------
_MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "migrations"
    / "tenant"
    / "versions"
    / "0008_tax_review_queue.py"
)


def _src() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_migration_file_exists():
    assert _MIGRATION_PATH.exists(), f"Migration not found at {_MIGRATION_PATH}"


def test_revision_metadata():
    src = _src()
    assert 'revision: str = "0008_tax_review_queue"' in src
    assert 'down_revision: str | None = "0007_closed_period"' in src


def test_create_table_present():
    src = _src()
    assert "tax_review_queue" in src
    # The create_table call must be guarded by an idempotency check
    assert "_table_exists" in src
    assert "if not _table_exists" in src


def test_required_columns_declared():
    src = _src()
    required_columns = [
        '"id"',   # not checking schema, just the column name patterns
        "workspace_id",
        "entity_id",
        "tax_year",
        "item_type",
        "form_reference",
        "content",
        "confidence",
        "citations",
        "status",
        "submitter_kind",
        "submitter_id",
        "reviewer_uc_uid",
        "resolution_notes",
        "created_at",
        "resolved_at",
    ]
    # Collect all column name strings mentioned in the migration.
    for col in required_columns:
        # Column names appear as string literals in Column() calls.
        assert col in src, f"Expected column reference {col!r} in migration"


def test_foreign_key_entity_with_cascade():
    src = _src()
    assert "tax_review_queue_entity_fk" in src
    assert "ondelete" in src
    assert "CASCADE" in src


def test_grant_select_insert_update_present():
    """The migration must grant SELECT, INSERT, UPDATE on tax_review_queue.

    The migration uses an f-string with {schema} and {RUNTIME_ROLE}, so the
    raw source contains the column name pattern alongside the variable references.
    We check for both the grant verb combo and the table name in the same
    statement-like context (same line or adjacent).
    """
    src = _src()
    # The f-string in the migration is:
    #   f'GRANT SELECT, INSERT, UPDATE ON "{schema}".tax_review_queue TO {RUNTIME_ROLE}'
    # In the raw source the table name is literal even though schema/role are vars.
    assert re.search(
        r"GRANT\s+SELECT\s*,\s*INSERT\s*,\s*UPDATE\s+ON\s+.*tax_review_queue",
        src,
        re.IGNORECASE,
    ), "Missing GRANT SELECT, INSERT, UPDATE on tax_review_queue"
    # Confirm the runtime role constant is present as a target
    assert "RUNTIME_ROLE" in src or "accounting_ops_app" in src, (
        "Migration must reference RUNTIME_ROLE or accounting_ops_app as the grant target"
    )


def test_revoke_delete_truncate_present():
    """The migration must revoke DELETE, TRUNCATE on tax_review_queue.

    Same f-string pattern: table name is literal, schema/role are variables.
    """
    src = _src()
    assert re.search(
        r"REVOKE\s+DELETE\s*,\s*TRUNCATE\s+ON\s+.*tax_review_queue",
        src,
        re.IGNORECASE,
    ), "Missing REVOKE DELETE, TRUNCATE on tax_review_queue"


def test_no_grant_delete_on_table():
    src = _src()
    for line in src.splitlines():
        stripped = line.strip().upper()
        if not stripped.startswith("GRANT"):
            continue
        if "TAX_REVIEW_QUEUE" in stripped or "TAX_REVIEW" in stripped:
            assert "DELETE" not in stripped, (
                f"GRANT DELETE found on line {line!r} — "
                "tax_review_queue must never have DELETE granted"
            )


def test_no_grant_truncate_on_table():
    src = _src()
    for line in src.splitlines():
        stripped = line.strip().upper()
        if not stripped.startswith("GRANT"):
            continue
        if "TAX_REVIEW_QUEUE" in stripped or "TAX_REVIEW" in stripped:
            assert "TRUNCATE" not in stripped, (
                f"GRANT TRUNCATE found on line {line!r} — "
                "tax_review_queue must never have TRUNCATE granted"
            )


def test_truncate_trigger_present():
    src = _src()
    assert "forbid_append_only_truncate" in src, (
        "BEFORE TRUNCATE guard (forbid_append_only_truncate) must be present"
    )
    assert "tax_review_queue_no_truncate" in src


def test_rls_policy_present():
    src = _src()
    assert "ENABLE ROW LEVEL SECURITY" in src
    assert "workspace_isolation" in src
    # Policy must filter on the workspace_id GUC
    assert "app.current_workspace" in src


def test_entity_year_status_index_present():
    src = _src()
    assert "tax_review_queue_entity_year_status_idx" in src
    assert "entity_id, tax_year, status" in src


def test_workspace_status_created_index_present():
    src = _src()
    assert "tax_review_queue_workspace_status_created_idx" in src
    assert "workspace_id, status, created_at DESC" in src


def test_downgrade_function_exists():
    src = _src()
    assert "def downgrade" in src
    # Downgrade must drop the policy, trigger, and table.
    assert "DROP POLICY" in src
    assert "DROP TRIGGER" in src
    assert "drop_table" in src


def test_upgrade_requires_tenant_schema():
    """The upgrade function must raise RuntimeError when no tenant schema is set.

    This prevents the migration from silently running against the public schema.
    """
    src = _src()
    assert "raise RuntimeError" in src
    assert "requires -x tenant=" in src


def test_postgresql_jsonb_imported():
    """The migration must import sqlalchemy.dialects.postgresql for JSONB columns."""
    src = _src()
    assert "from sqlalchemy.dialects import postgresql" in src
    assert "postgresql.JSONB()" in src
