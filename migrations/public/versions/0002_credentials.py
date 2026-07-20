"""public schema: tenant_credentials + credential_access_log

Revision ID: 0002_credentials
Revises: 0001_public_initial
Create Date: 2026-05-22

Encrypted credentials live in the public schema (not per-workspace) because
they're a platform-level secret that the runtime decrypts before per-tenant
queries. The workspace_id FK + AAD-binding still scopes them to a single
workspace cryptographically.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_credentials"
down_revision: str | None = "0001_public_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_credentials",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column(
            "connector",
            sa.String(length=96),
            nullable=False,
            comment="e.g. 'simplefin', 'ebay_finances', 'imap_receipts', 'stripe'",
        ),
        sa.Column(
            "label",
            sa.String(length=256),
            nullable=False,
            comment="User-visible label, e.g. 'Business Bank' or 'Acme eBay'",
        ),
        sa.Column(
            "ciphertext",
            postgresql.BYTEA(),
            nullable=False,
            comment="AES-256-GCM output: ciphertext || 16-byte auth tag",
        ),
        sa.Column(
            "nonce",
            postgresql.BYTEA(),
            nullable=False,
            comment="12-byte random nonce, per-encrypt",
        ),
        sa.Column(
            "key_version",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("1"),
            comment="For lazy rotation. Current = 1.",
        ),
        sa.Column(
            "created_by_uc_uid",
            sa.String(length=128),
            nullable=True,
            comment="uc_uid of the user who created the credential",
        ),
        sa.Column(
            "encrypted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["public.workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "connector",
            "label",
            name="uq_tenant_credentials_workspace_connector_label",
        ),
        schema="public",
    )
    op.create_index(
        "ix_tenant_credentials_workspace_connector",
        "tenant_credentials",
        ["workspace_id", "connector"],
        schema="public",
    )

    op.create_table(
        "credential_access_log",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("credential_id", sa.UUID(), nullable=False),
        sa.Column(
            "actor_uc_uid",
            sa.String(length=128),
            nullable=True,
            comment="null = system call (background worker, etc.)",
        ),
        sa.Column(
            "actor_agent_id",
            sa.String(length=256),
            nullable=True,
            comment="From RFC 8693 act claim if the caller was an agent",
        ),
        sa.Column(
            "requesting_service",
            sa.String(length=128),
            nullable=False,
            comment="e.g. 'cash_position', 'transactions', 'agent:reconciler'",
        ),
        sa.Column(
            "operation",
            sa.String(length=32),
            nullable=False,
            comment="encrypt | decrypt | rotate | delete",
        ),
        sa.Column(
            "success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["public.workspace.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        schema="public",
    )
    op.create_index(
        "ix_credential_access_log_workspace_occurred",
        "credential_access_log",
        ["workspace_id", sa.text("occurred_at DESC")],
        schema="public",
    )
    op.create_index(
        "ix_credential_access_log_credential",
        "credential_access_log",
        ["credential_id"],
        schema="public",
    )

    # Append-only enforcement: no UPDATE/DELETE on the access log.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.forbid_credential_access_log_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'credential_access_log is append-only';
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER credential_access_log_no_update "
        "BEFORE UPDATE OR DELETE ON public.credential_access_log "
        "FOR EACH ROW EXECUTE FUNCTION public.forbid_credential_access_log_mutation();"
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS public.forbid_credential_access_log_mutation() CASCADE"
    )
    op.drop_table("credential_access_log", schema="public")
    op.drop_table("tenant_credentials", schema="public")
