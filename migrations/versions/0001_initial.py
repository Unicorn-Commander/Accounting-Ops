"""initial accounting ops schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sovereignty_mode = postgresql.ENUM("standard", "strict", name="sovereignty_mode")
    decision_verdict = postgresql.ENUM("Approve", "Reject", "NeedsReview", name="decision_verdict")
    approval_status = postgresql.ENUM("pending", "approved", "rejected", "canceled", name="approval_status")
    compliance_trigger = postgresql.ENUM("schedule", "event", "threshold", name="compliance_trigger")
    compliance_action = postgresql.ENUM("notify", "block", "require_approval", "tag", name="compliance_action")
    compliance_source = postgresql.ENUM(
        "user_setup", "ai_inferred", "external_context", name="compliance_source"
    )
    for enum in (
        sovereignty_mode,
        decision_verdict,
        approval_status,
        compliance_trigger,
        compliance_action,
        compliance_source,
    ):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenant",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("postgres_schema", sa.String(length=128), nullable=False),
        sa.Column("falkordb_subgraph", sa.String(length=128), nullable=False),
        sa.Column("garage_bucket", sa.String(length=128), nullable=False),
        sa.Column("hipaa_protected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sovereignty_mode", sovereignty_mode, server_default="standard", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
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
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["predecessor_event_id"], ["event_log.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
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
        sa.Column("external_refs", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
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
        sa.Column("external_refs", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["vendor_id"], ["vendor.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_counterparty_tenant_id", "counterparty", ["tenant_id"])
    op.create_index("ix_counterparty_tenant_counterparty", "counterparty", ["tenant_id", "id"])
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_receipt_tenant_id", "receipt", ["tenant_id"])
    op.create_index("ix_receipt_content_hash", "receipt", ["content_hash"], unique=True)
    op.create_check_constraint("ck_receipt_currency_upper", "receipt", "currency_code = upper(currency_code)")

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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_journal_entry_tenant_id", "journal_entry", ["tenant_id"])
    op.create_index("ix_journal_entry_tenant_posted_at", "journal_entry", ["tenant_id", sa.text("posted_at DESC")])
    op.create_index("ix_journal_entry_tenant_counterparty", "journal_entry", ["tenant_id", "counterparty_id"])
    op.create_check_constraint("ck_journal_entry_currency_upper", "journal_entry", "currency_code = upper(currency_code)")
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_compliance_rule_tenant_id", "compliance_rule", ["tenant_id"])

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
        sa.Column("status", approval_status, server_default="pending", nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("requested_by_identity", sa.String(length=256), nullable=False),
        sa.Column("decided_by_uc_uid", sa.String(length=128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approval_queue_tenant_id", "approval_queue", ["tenant_id"])
    op.create_check_constraint("ck_approval_queue_currency_upper", "approval_queue", "currency_code = upper(currency_code)")

    op.create_table(
        "connector_state",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("connector_name", sa.String(length=96), nullable=False),
        sa.Column("state", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "connector_name", name="uq_connector_state_tenant_connector"),
    )
    op.create_index("ix_connector_state_tenant_id", "connector_state", ["tenant_id"])

    op.create_table(
        "agent_session",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.String(length=256), nullable=False),
        sa.Column("user_uc_uid", sa.String(length=128), nullable=True),
        sa.Column("scope", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["agent_session_id"], ["agent_session.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["receipt.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_audit_tenant_id", "decision_audit", ["tenant_id"])

    op.create_table(
        "fx_rate",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("base_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("quote_currency", sa.CHAR(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fx_rate_tenant_id", "fx_rate", ["tenant_id"])
    op.create_check_constraint("ck_fx_rate_base_currency_upper", "fx_rate", "base_currency = upper(base_currency)")
    op.create_check_constraint("ck_fx_rate_quote_currency_upper", "fx_rate", "quote_currency = upper(quote_currency)")

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
        sa.Column("matched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["journal_entry_id"], ["journal_entry.id"]),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipt.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recon_link_tenant_id", "recon_link", ["tenant_id"])
    op.create_check_constraint("ck_recon_link_currency_upper", "recon_link", "currency_code = upper(currency_code)")


def downgrade() -> None:
    for table in (
        "recon_link",
        "fx_rate",
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
        "tenant",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS forbid_journal_entry_mutation() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS forbid_event_log_mutation() CASCADE")
    for enum_name in (
        "compliance_source",
        "compliance_action",
        "compliance_trigger",
        "approval_status",
        "decision_verdict",
        "sovereignty_mode",
    ):
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)
