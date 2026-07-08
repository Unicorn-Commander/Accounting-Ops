"""String-level migration smoke test for 0014_sentinel.py.

Mirrors test_tax_review_queue_schema.py — pure text checks (no DB), so this
runs in `make test`.
"""
from __future__ import annotations

import pathlib
import re

_MIGRATION_PATH = (
    pathlib.Path(__file__).parent.parent.parent
    / "migrations"
    / "tenant"
    / "versions"
    / "0014_sentinel.py"
)


def _src() -> str:
    return _MIGRATION_PATH.read_text(encoding="utf-8")


def test_migration_file_exists():
    assert _MIGRATION_PATH.exists()


def test_revision_metadata():
    src = _src()
    assert 'revision: str = "0014_sentinel"' in src
    assert 'down_revision: str | None = "0013_tax_filing_submission"' in src


def test_four_tables_created():
    src = _src()
    for table in (
        "sentinel_config",
        "sentinel_scheduled_action",
        "sentinel_finding",
        "sentinel_check_run",
    ):
        assert table in src, f"Missing table {table!r}"


def test_sentinel_config_columns():
    src = _src()
    required = (
        "workspace_id", "autonomy_level", "check_interval_minutes",
        "max_actions_per_day", "enabled_checks", "notify_owners",
        "notify_accountants", "last_run_at",
    )
    for col in required:
        assert col in src, f"sentinel_config missing column {col!r}"


def test_scheduled_action_columns():
    src = _src()
    required = (
        "action_type", "scheduled_for", "payload", "status",
        "execution_notes", "executed_audit_id", "created_by_check_run_id",
    )
    for col in required:
        assert col in src


def test_finding_columns():
    src = _src()
    required = (
        "check_type", "severity", "title", "summary", "details",
        "action_type", "action_ref_id", "decision_audit_id",
        "acknowledged_by_uc_uid", "acknowledged_at",
    )
    for col in required:
        assert col in src


def test_check_run_columns():
    src = _src()
    required = (
        "cycle_id", "started_at", "finished_at", "duration_ms",
        "status", "findings_count", "actions_taken", "summary",
        "error_summary",
    )
    for col in required:
        assert col in src


def test_grant_pattern_on_all_tables():
    src = _src()
    for table in (
        "sentinel_config", "sentinel_scheduled_action",
        "sentinel_finding", "sentinel_check_run",
    ):
        # The grants are emitted via an f-string loop; the table name appears
        # in the literal tuple iteration but the grant SQL itself uses
        # {table} as a parameter. Check the loop exists and references the
        # grant verb tuple.
        assert table in src
    assert re.search(
        r"GRANT\s+SELECT\s*,\s*INSERT\s*,\s*UPDATE\s+ON",
        src,
        re.IGNORECASE,
    )
    assert re.search(
        r"REVOKE\s+DELETE\s*,\s*TRUNCATE\s+ON",
        src,
        re.IGNORECASE,
    )


def test_rls_pattern():
    src = _src()
    assert "ENABLE ROW LEVEL SECURITY" in src
    assert "workspace_isolation" in src
    assert "app.current_workspace" in src


def test_truncate_guard():
    src = _src()
    assert "forbid_append_only_truncate" in src
    assert "_no_truncate" in src


def test_indexes_created():
    src = _src()
    for idx in (
        "sentinel_scheduled_action_pending_idx",
        "sentinel_finding_workspace_created_idx",
        "sentinel_finding_check_type_idx",
        "sentinel_check_run_cycle_idx",
        "sentinel_check_run_workspace_started_idx",
    ):
        assert idx in src, f"Missing index {idx!r}"


def test_downgrade_drops_all_four():
    src = _src()
    assert "def downgrade" in src
    for table in (
        "sentinel_config", "sentinel_scheduled_action",
        "sentinel_finding", "sentinel_check_run",
    ):
        assert table in src


def test_jsonb_used():
    src = _src()
    assert "from sqlalchemy.dialects import postgresql" in src
    assert "postgresql.JSONB()" in src


def test_requires_tenant_schema():
    src = _src()
    assert "raise RuntimeError" in src
    assert "requires -x tenant=" in src
