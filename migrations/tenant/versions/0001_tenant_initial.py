"""per-tenant schema: full accounting tables (event log, journal, compliance, etc.)

Revision ID: 0001_tenant_initial
Revises:
Create Date: 2026-05-21

Runs against the schema specified by `-x tenant=<schema_name>` on the alembic
command line. The env.py sets search_path to that schema before configuring
the migration context, so every CREATE TABLE here lands in that schema.

The tenant.id column on every TenantScopedMixin table is REDUNDANT under
schema-per-tenant (each schema only has rows for one tenant), but we keep it
for two reasons:
1. Cross-tenant consolidation queries via UNION ALL stay symmetric
2. If a tenant ever needs to be merged or split, the tenant_id is preserved
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_tenant_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Per-tenant enum types. Created in the current schema (search_path set by env.py).
    bind = op.get_bind()
    for name, values in (
        ("decision_verdict", ("Approve", "Reject", "NeedsReview")),
        ("approval_status", ("pending", "approved", "rejected", "canceled")),
        ("compliance_trigger", ("schedule", "event", "threshold")),
        ("compliance_action", ("notify", "block", "require_approval", "tag")),
        ("compliance_source", ("user_setup", "ai_inferred", "external_context")),
        (
            "activity_verdict",
            ("APPROVE", "REJECT", "NEEDS_REVIEW", "BLOCKED", "THINKING", "CANCELED"),
        ),
        ("activity_materiality", ("L", "M", "H")),
    ):
        sa.Enum(*values, name=name).create(bind, checkfirst=True)

    decision_verdict = postgresql.ENUM(name="decision_verdict", create_type=False)
    approval_status = postgresql.ENUM(name="approval_status", create_type=False)
    compliance_trigger = postgresql.ENUM(name="compliance_trigger", create_type=False)
    compliance_action = postgresql.ENUM(name="compliance_action", create_type=False)
    compliance_source = postgresql.ENUM(name="compliance_source", create_type=False)
    activity_verdict = postgresql.ENUM(name="activity_verdict", create_type=False)
    activity_materiality = postgresql.ENUM(name="activity_materiality", create_type=False)

    # tenant_id is included on every row for cross-tenant UNION queries
    # (consolidation overlay agent reads multiple schemas symmetrically).

    op.create_table(
        "event_log",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=96), nullable=False),
        sa.Column("event_payload", postgresql.JSONB(), nullable=False),
        sa.Column("predecessor_event_id", sa.UUID(), nullable=True),
        sa.Column("actor_uc_uid", sa.String(length=128), nullable=True),
        sa.Column("actor_agent_id", sa.String(length=256), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("merkle_root", sa.String(length=64), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["predecessor_event_id"], ["event_log.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_hash"),
    )
    op.create_index("ix_event_log_tenant_id", "event_log", ["tenant_id"])
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_event_log_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'event_log is append-only';
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER event_log_no_update BEFORE UPDATE OR DELETE ON event_log "
        "FOR EACH ROW EXECUTE FUNCTION forbid_event_log_mutation();"
    )

    op.create_table(
        "vendor",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column(
            "external_refs",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vendor_tenant_id", "vendor", ["tenant_id"])

    op.create_table(
        "counterparty",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("vendor_id", sa.UUID(), nullable=True),
        sa.Column(
            "external_refs",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendor.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_counterparty_tenant_id", "counterparty", ["tenant_id"])

    op.create_table(
        "receipt",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("garage_bucket", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("currency_code", sa.CHAR(length=3), nullable=True),
        sa.Column("usd_equivalent_at_event_time", sa.Numeric(20, 4), nullable=True),
        sa.Column("fx_source", sa.String(length=64), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_receipt_tenant_id", "receipt", ["tenant_id"])
    op.create_index("ix_receipt_content_hash", "receipt", ["content_hash"], unique=True)
    op.create_check_constraint(
        "ck_receipt_currency_upper", "receipt", "currency_code = upper(currency_code)"
    )

    op.create_table(
        "journal_entry",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency_code", sa.CHAR(length=3), nullable=False),
        sa.Column("usd_equivalent_at_event_time", sa.Numeric(20, 4), nullable=False),
        sa.Column("fx_source", sa.String(length=64), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=False),
        sa.Column("source_document_id", sa.UUID(), nullable=True),
        sa.Column("source_document_hash", sa.String(length=64), nullable=False),
        sa.Column("counterparty_id", sa.UUID(), nullable=False),
        sa.Column("reasoning_trace", sa.Text(), nullable=False),
        sa.Column("rule_version_applied", sa.UUID(), nullable=False),
        sa.Column("model_version", sa.String(length=128), nullable=False),
        sa.Column("decision_verdict", decision_verdict, nullable=False),
        sa.Column("identity", sa.String(length=256), nullable=False),
        sa.Column("approving_human_uc_uid", sa.String(length=128), nullable=True),
        sa.Column("reverses_entry_id", sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(["counterparty_id"], ["counterparty.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["event_log.id"]),
        sa.ForeignKeyConstraint(["reverses_entry_id"], ["journal_entry.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["receipt.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_journal_entry_tenant_id", "journal_entry", ["tenant_id"])
    op.create_index(
        "ix_journal_entry_tenant_posted_at",
        "journal_entry",
        ["tenant_id", sa.text("posted_at DESC")],
    )
    op.create_index(
        "ix_journal_entry_tenant_counterparty",
        "journal_entry",
        ["tenant_id", "counterparty_id"],
    )
    op.create_check_constraint(
        "ck_journal_entry_currency_upper",
        "journal_entry",
        "currency_code = upper(currency_code)",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_journal_entry_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'journal_entry is append-only; post a reversal instead';
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER journal_entry_no_update BEFORE UPDATE OR DELETE ON journal_entry "
        "FOR EACH ROW EXECUTE FUNCTION forbid_journal_entry_mutation();"
    )

    op.create_table(
        "compliance_profile",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_compliance_profile_tenant_id", "compliance_profile", ["tenant_id"])

    op.create_table(
        "compliance_rule",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=96), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("trigger", compliance_trigger, nullable=False),
        sa.Column("action", compliance_action, nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", compliance_source, nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["profile_id"], ["compliance_profile.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_compliance_rule_tenant_id", "compliance_rule", ["tenant_id"])
    op.create_index(
        "ix_compliance_rule_effective", "compliance_rule", ["tenant_id", "effective_from"]
    )

    op.create_table(
        "approval_queue",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=True),
        sa.Column("currency_code", sa.CHAR(length=3), nullable=True),
        sa.Column("usd_equivalent_at_event_time", sa.Numeric(20, 4), nullable=True),
        sa.Column("fx_source", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            approval_status,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("requested_by_identity", sa.String(length=256), nullable=False),
        sa.Column("decided_by_uc_uid", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approval_queue_tenant_id", "approval_queue", ["tenant_id"])
    op.create_index("ix_approval_queue_status", "approval_queue", ["status"])
    op.create_check_constraint(
        "ck_approval_queue_currency_upper",
        "approval_queue",
        "currency_code = upper(currency_code)",
    )

    op.create_table(
        "connector_state",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connector_name", sa.String(length=96), nullable=False),
        sa.Column(
            "state",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "connector_name", name="uq_connector_state_tenant_connector"
        ),
    )
    op.create_index("ix_connector_state_tenant_id", "connector_state", ["tenant_id"])

    op.create_table(
        "agent_session",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.String(length=256), nullable=False),
        sa.Column("user_uc_uid", sa.String(length=128), nullable=True),
        sa.Column(
            "scope",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_session_tenant_id", "agent_session", ["tenant_id"])

    op.create_table(
        "decision_audit",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_session_id", sa.UUID(), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("response_payload", postgresql.JSONB(), nullable=True),
        sa.Column("reasoning_trace", sa.Text(), nullable=False),
        sa.Column("rule_version_applied", sa.UUID(), nullable=True),
        sa.Column("source_document_id", sa.UUID(), nullable=True),
        sa.Column("model_version", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_session_id"], ["agent_session.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["receipt.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_audit_tenant_id", "decision_audit", ["tenant_id"])
    op.create_index("ix_decision_audit_created", "decision_audit", ["created_at"])

    # Activity feed (agent ticker)
    op.create_table(
        "activity_event",
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column(
            "emitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=200), nullable=False),
        sa.Column("materiality", activity_materiality, nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=True),
        sa.Column("verdict", activity_verdict, nullable=False),
        sa.Column("decision_basis", postgresql.JSONB(), nullable=True),
        sa.Column("transparency", postgresql.JSONB(), nullable=True),
        sa.Column("parent_run_id", sa.UUID(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("resolved_by", sa.String(length=256), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_activity_event_tenant_id", "activity_event", ["tenant_id"])
    op.create_index(
        "ix_activity_event_tenant_emitted",
        "activity_event",
        ["tenant_id", sa.text("emitted_at DESC")],
    )
    op.create_index("ix_activity_event_parent_run", "activity_event", ["parent_run_id"])
    op.create_index("ix_activity_event_needs_human", "activity_event", ["needs_human"])

    op.create_table(
        "fx_rate",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("base_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("quote_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fx_rate_tenant_id", "fx_rate", ["tenant_id"])
    op.create_index("ix_fx_rate_pair_observed", "fx_rate", ["base_currency", "quote_currency", "observed_at"])
    op.create_check_constraint(
        "ck_fx_rate_base_currency_upper", "fx_rate", "base_currency = upper(base_currency)"
    )
    op.create_check_constraint(
        "ck_fx_rate_quote_currency_upper", "fx_rate", "quote_currency = upper(quote_currency)"
    )

    op.create_table(
        "recon_link",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("journal_entry_id", sa.UUID(), nullable=False),
        sa.Column("receipt_id", sa.UUID(), nullable=True),
        sa.Column("external_transaction_ref", sa.String(length=256), nullable=True),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency_code", sa.CHAR(length=3), nullable=False),
        sa.Column("usd_equivalent_at_event_time", sa.Numeric(20, 4), nullable=False),
        sa.Column("fx_source", sa.String(length=64), nullable=True),
        sa.Column(
            "matched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipt.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recon_link_tenant_id", "recon_link", ["tenant_id"])
    op.create_check_constraint(
        "ck_recon_link_currency_upper", "recon_link", "currency_code = upper(currency_code)"
    )


def downgrade() -> None:
    for table in (
        "recon_link",
        "fx_rate",
        "activity_event",
        "decision_audit",
        "agent_session",
        "connector_state",
        "approval_queue",
        "compliance_rule",
        "compliance_profile",
        "journal_entry",
        "receipt",
        "counterparty",
        "vendor",
        "event_log",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS forbid_journal_entry_mutation() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS forbid_event_log_mutation() CASCADE")
    for enum_name in (
        "activity_materiality",
        "activity_verdict",
        "compliance_source",
        "compliance_action",
        "compliance_trigger",
        "approval_status",
        "decision_verdict",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
