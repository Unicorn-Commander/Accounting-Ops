"""per-workspace schema: Sentinel autonomous CPA daemon tables.

Revision ID: 0014_sentinel
Revises: 0013_tax_filing_submission
Create Date: 2026-05-30

Adds four tables that back the Sentinel agent:

  sentinel_config             — per-workspace settings (autonomy level, intervals,
                                check toggles, notification prefs, daily action limits).
                                One row per workspace; created at workspace provisioning
                                or on first sentinel run with safe defaults.
  sentinel_scheduled_action   — actions Sentinel wants to take in the future
                                (e.g. "remind on 2026-09-15 to file Q3 estimate").
                                Picked up by the next cron tick.
  sentinel_finding            — proactive findings the daemon surfaced (with parent
                                check_run_id, action_type, escalation_status,
                                acknowledgment trail).  Distinct from
                                tax_review_queue (which Sentinel ALSO writes into):
                                this surface aggregates "Sentinel did X" across all
                                check types for the activity feed.
  sentinel_check_run          — observability row per check cycle:
                                {check_type, started_at, finished_at, status,
                                 actions_taken, summary}.

Schema design notes:
  - autonomy_level enum: 'off' | 'watch_only' | 'draft_and_flag' | 'fully_autonomous'.
  - check_type enum is open-ended via TEXT column with a documented vocabulary
    (no enum DDL needed when we add new checks).
  - daily_action_rate counter is computed by counting decision_audit rows
    where actor_role='sentinel' within today's UTC date — no per-day counter
    column needed (idempotent + audit-traceable).
  - All tables RLS-gated on app.current_workspace (mirrors 0008/0009/0010).

Grants:
  sentinel_config            SELECT, INSERT, UPDATE (config is mutable)
  sentinel_scheduled_action  SELECT, INSERT, UPDATE (status transitions only)
  sentinel_finding           SELECT, INSERT, UPDATE (no DELETE — audit trail)
  sentinel_check_run         SELECT, INSERT, UPDATE (no DELETE — audit trail)

Idempotent + re-runnable (information_schema guards on every DDL change).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0014_sentinel"
down_revision: str | None = "0013_tax_filing_submission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"


def _set_search_path() -> str:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
    return schema or ""


def _table_exists(schema: str, table: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = :schema AND table_name = :table
                """
            ),
            {"schema": schema, "table": table},
        ).first()
    )


def _index_exists(schema: str, index: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM pg_indexes
                WHERE schemaname = :schema AND indexname = :index
                """
            ),
            {"schema": schema, "index": index},
        ).first()
    )


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0014 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. sentinel_config: per-workspace settings (one row per workspace).
    # ------------------------------------------------------------------
    if not _table_exists(schema, "sentinel_config"):
        op.create_table(
            "sentinel_config",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column(
                "autonomy_level",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'draft_and_flag'"),
                comment=(
                    "'off' | 'watch_only' | 'draft_and_flag' | 'fully_autonomous'. "
                    "Default 'draft_and_flag' is the recommended safe starting "
                    "point: Sentinel can draft memos, flag items, and send "
                    "informational emails, but cannot bulk-decide approvals or "
                    "initiate connector syncs."
                ),
            ),
            sa.Column(
                "check_interval_minutes",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("360"),
                comment="Minutes between Sentinel cron cycles. Default 360 = 6h.",
            ),
            sa.Column(
                "max_actions_per_day",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("50"),
                comment="Hard cap on actions per workspace per UTC day.",
            ),
            sa.Column(
                "enabled_checks",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[\"all\"]'::jsonb"),
                comment=(
                    "List of check_type slugs to run, or ['all']. "
                    "Vocabulary: filing_deadlines, approval_aging, recon_drift, "
                    "uncategorized_txns, pending_invitations, regulatory_updates, "
                    "tax_credit_scan, sec_174_compliance, sec_195_election, "
                    "officer_comp, nol_carryforward, qsbs_holding, cash_runway, "
                    "yoy_diff, connector_health, coa_coverage, "
                    "corporate_formalities."
                ),
            ),
            sa.Column(
                "notify_owners",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
                comment="Email workspace owners on daily summary + escalations.",
            ),
            sa.Column(
                "notify_accountants",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
                comment="Email workspace accountants (CPAs) on daily summary.",
            ),
            sa.Column(
                "last_run_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                comment="Timestamp of the last successful cron cycle.",
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workspace_id", name="sentinel_config_workspace_uq"),
            comment=(
                "Sentinel autonomous CPA daemon configuration. One row per "
                "workspace; created on first sentinel run with defaults if absent."
            ),
        )

    # ------------------------------------------------------------------
    # 2. sentinel_scheduled_action: future actions queued for later ticks.
    # ------------------------------------------------------------------
    if not _table_exists(schema, "sentinel_scheduled_action"):
        op.create_table(
            "sentinel_scheduled_action",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=True),
            sa.Column(
                "action_type",
                sa.Text(),
                nullable=False,
                comment=(
                    "'draft_memo' | 'flag_for_cpa_review' | 'send_invitation' | "
                    "'send_email' | 'schedule_reminder' | 'create_open_item' | "
                    "'run_specialist' | 'create_project_ops_task' (stubbed)"
                ),
            ),
            sa.Column(
                "scheduled_for",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                comment="When to execute (typically next sentinel tick at or after this).",
            ),
            sa.Column(
                "payload",
                postgresql.JSONB(),
                nullable=False,
                comment="Action-type-specific args (memo text, recipient, etc.).",
            ),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'pending'"),
                comment="'pending' | 'executed' | 'cancelled' | 'failed'",
            ),
            sa.Column(
                "execution_notes",
                sa.Text(),
                nullable=True,
                comment="Free-text notes after execution (success message or error).",
            ),
            sa.Column(
                "created_by_check_run_id",
                sa.UUID(),
                nullable=True,
                comment="FK to sentinel_check_run.id that scheduled this action.",
            ),
            sa.Column(
                "executed_audit_id",
                sa.UUID(),
                nullable=True,
                comment="FK to decision_audit.id of the row written on execution.",
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "executed_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="sentinel_scheduled_action_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "Future actions Sentinel wants to take. Polled by every cron "
                "tick: rows where scheduled_for <= now() and status='pending' "
                "are executed and marked 'executed'."
            ),
        )

    # ------------------------------------------------------------------
    # 3. sentinel_finding: proactive findings surfaced by Sentinel.
    # ------------------------------------------------------------------
    if not _table_exists(schema, "sentinel_finding"):
        op.create_table(
            "sentinel_finding",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=True),
            sa.Column(
                "check_type",
                sa.Text(),
                nullable=False,
                comment=(
                    "Check that produced this finding (see sentinel_config."
                    "enabled_checks vocabulary)."
                ),
            ),
            sa.Column(
                "severity",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'info'"),
                comment="'info' | 'low' | 'medium' | 'high' | 'critical'",
            ),
            sa.Column(
                "title",
                sa.Text(),
                nullable=False,
                comment="Short headline (<= 200 chars).",
            ),
            sa.Column(
                "summary",
                sa.Text(),
                nullable=False,
                comment="One-paragraph description.",
            ),
            sa.Column(
                "details",
                postgresql.JSONB(),
                nullable=True,
                comment=(
                    "Structured details. E.g. for filing_deadlines: "
                    "{form: '1120', due: '2026-04-15', days_until: 14}."
                ),
            ),
            sa.Column(
                "action_type",
                sa.Text(),
                nullable=True,
                comment="Action Sentinel took ('draft_memo', 'flag', 'email', etc.).",
            ),
            sa.Column(
                "action_ref_id",
                sa.UUID(),
                nullable=True,
                comment="ID of the artifact (memo, scheduled action, etc.).",
            ),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'open'"),
                comment="'open' | 'acknowledged' | 'resolved' | 'dismissed'",
            ),
            sa.Column(
                "check_run_id",
                sa.UUID(),
                nullable=True,
                comment="FK to sentinel_check_run.id that produced this finding.",
            ),
            sa.Column(
                "decision_audit_id",
                sa.UUID(),
                nullable=True,
                comment=(
                    "Optional: parent decision_audit.id (when an action was "
                    "audit-trailed). Provides linkage to the show-your-work record."
                ),
            ),
            sa.Column(
                "acknowledged_by_uc_uid",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "acknowledged_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="sentinel_finding_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "Proactive findings the Sentinel daemon surfaced. The activity-"
                "feed surface for 'what Sentinel did this week'. Distinct from "
                "tax_review_queue (Sentinel writes into both: tax_review_queue "
                "for CPA-grade items; sentinel_finding for the catch-all feed)."
            ),
        )

    # ------------------------------------------------------------------
    # 4. sentinel_check_run: observability row per cron cycle.
    # ------------------------------------------------------------------
    if not _table_exists(schema, "sentinel_check_run"):
        op.create_table(
            "sentinel_check_run",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column(
                "entity_id",
                sa.UUID(),
                nullable=True,
                comment="NULL for workspace-wide checks.",
            ),
            sa.Column(
                "check_type",
                sa.Text(),
                nullable=False,
                comment="See sentinel_config.enabled_checks vocabulary.",
            ),
            sa.Column(
                "cycle_id",
                sa.UUID(),
                nullable=False,
                comment=(
                    "Shared id for all check_runs in one cron cycle (so we can "
                    "group 'Sentinel did all 17 checks at 2026-05-30 06:00 UTC')."
                ),
            ),
            sa.Column(
                "started_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "finished_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "duration_ms",
                sa.BigInteger(),
                nullable=True,
            ),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'running'"),
                comment="'running' | 'completed' | 'failed' | 'skipped'",
            ),
            sa.Column(
                "findings_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "actions_taken",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "summary",
                sa.Text(),
                nullable=True,
                comment="One-line human-readable result.",
            ),
            sa.Column(
                "error_summary",
                sa.Text(),
                nullable=True,
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="sentinel_check_run_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "Observability record per Sentinel check execution. "
                "Append-only; no DELETE."
            ),
        )

    # ------------------------------------------------------------------
    # 5. Indexes
    # ------------------------------------------------------------------
    if not _index_exists(schema, "sentinel_scheduled_action_pending_idx"):
        op.execute(
            f'CREATE INDEX sentinel_scheduled_action_pending_idx '
            f'ON "{schema}".sentinel_scheduled_action (workspace_id, status, scheduled_for)'
        )
    if not _index_exists(schema, "sentinel_finding_workspace_created_idx"):
        op.execute(
            f'CREATE INDEX sentinel_finding_workspace_created_idx '
            f'ON "{schema}".sentinel_finding (workspace_id, status, created_at DESC)'
        )
    if not _index_exists(schema, "sentinel_finding_check_type_idx"):
        op.execute(
            f'CREATE INDEX sentinel_finding_check_type_idx '
            f'ON "{schema}".sentinel_finding (workspace_id, check_type, created_at DESC)'
        )
    if not _index_exists(schema, "sentinel_check_run_cycle_idx"):
        op.execute(
            f'CREATE INDEX sentinel_check_run_cycle_idx '
            f'ON "{schema}".sentinel_check_run (workspace_id, cycle_id, started_at DESC)'
        )
    if not _index_exists(schema, "sentinel_check_run_workspace_started_idx"):
        op.execute(
            f'CREATE INDEX sentinel_check_run_workspace_started_idx '
            f'ON "{schema}".sentinel_check_run (workspace_id, started_at DESC)'
        )

    # ------------------------------------------------------------------
    # 6. Least-privilege grants for the runtime role
    #    All four tables: SELECT, INSERT, UPDATE. No DELETE.
    # ------------------------------------------------------------------
    for table in (
        "sentinel_config",
        "sentinel_scheduled_action",
        "sentinel_finding",
        "sentinel_check_run",
    ):
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{table} TO {RUNTIME_ROLE}'
        )
        op.execute(
            f'REVOKE DELETE, TRUNCATE ON "{schema}".{table} FROM {RUNTIME_ROLE}'
        )

    # ------------------------------------------------------------------
    # 7. BEFORE TRUNCATE guards (re-uses forbid_append_only_truncate from 0005)
    # ------------------------------------------------------------------
    for table in (
        "sentinel_config",
        "sentinel_scheduled_action",
        "sentinel_finding",
        "sentinel_check_run",
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate "
            f"BEFORE TRUNCATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
        )

    # ------------------------------------------------------------------
    # 8. Row-Level Security on workspace_id GUC
    # ------------------------------------------------------------------
    for table in (
        "sentinel_config",
        "sentinel_scheduled_action",
        "sentinel_finding",
        "sentinel_check_run",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY workspace_isolation ON {table}
              USING (
                workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
              )
              WITH CHECK (
                workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
              )
            """
        )


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0014 requires -x tenant=<schema>")

    for table in (
        "sentinel_check_run",
        "sentinel_finding",
        "sentinel_scheduled_action",
        "sentinel_config",
    ):
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")

    for idx in (
        "sentinel_check_run_workspace_started_idx",
        "sentinel_check_run_cycle_idx",
        "sentinel_finding_check_type_idx",
        "sentinel_finding_workspace_created_idx",
        "sentinel_scheduled_action_pending_idx",
    ):
        op.execute(f'DROP INDEX IF EXISTS "{schema}".{idx}')

    for table in (
        "sentinel_check_run",
        "sentinel_finding",
        "sentinel_scheduled_action",
        "sentinel_config",
    ):
        if _table_exists(schema, table):
            op.drop_table(table)
