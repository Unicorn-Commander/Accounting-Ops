"""per-workspace schema: account_entity_mapping

Revision ID: 0002_account_mappings
Revises: 0001_tenant_initial
Create Date: 2026-05-22

Maps external bank/payment-processor accounts (identified by source +
external_id) to a specific entity within the workspace. Powers the
EntitySelector's filter on transactions + cash position.

Example mapping rows in a workspace:
  (simplefin, ACC-bank-0001, Example Bank, Business Checking (0001)) -> Acme Inc
  (simplefin, ACC-bank-0002, Example Bank, Checking (0002)) -> Owner Personal
  (simplefin, ACC-bank-0003, Example Bank, Signature Visa (0003)) -> Owner Personal
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0002_account_mappings"
down_revision: str | None = "0001_tenant_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))

    op.create_table(
        "account_entity_mapping",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=False,
            comment="connector name: 'simplefin', 'ebay_finances', 'plaid', 'stripe', etc.",
        ),
        sa.Column(
            "external_account_id",
            sa.String(length=256),
            nullable=False,
            comment="The connector's own account id (e.g., SimpleFIN account.id)",
        ),
        sa.Column(
            "external_org",
            sa.String(length=256),
            nullable=True,
            comment="Bank/org name from the connector (e.g., 'Bank of America')",
        ),
        sa.Column(
            "external_account_name",
            sa.String(length=512),
            nullable=True,
            comment="Display name from the connector (e.g., 'Magic Unicorn Checking (1792)')",
        ),
        sa.Column(
            "label",
            sa.String(length=256),
            nullable=True,
            comment="Optional user-overridden label",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "auto_categorize",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
            comment="If true, agents may auto-post transactions from this account "
            "to default categories (per materiality gate). If false, every "
            "transaction queues for human review regardless of materiality.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"], ["entity.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "external_account_id",
            name="uq_account_entity_mapping_source_account",
        ),
    )
    op.create_index(
        "ix_account_entity_mapping_entity_id",
        "account_entity_mapping",
        ["entity_id"],
    )
    op.create_index(
        "ix_account_entity_mapping_workspace_id",
        "account_entity_mapping",
        ["workspace_id"],
    )

    # Auto-update updated_at on every row mutation.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION touch_account_entity_mapping_updated_at()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        "CREATE TRIGGER account_entity_mapping_touch_updated_at "
        "BEFORE UPDATE ON account_entity_mapping "
        "FOR EACH ROW EXECUTE FUNCTION touch_account_entity_mapping_updated_at();"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS account_entity_mapping_touch_updated_at ON account_entity_mapping"
    )
    op.execute("DROP FUNCTION IF EXISTS touch_account_entity_mapping_updated_at() CASCADE")
    op.drop_table("account_entity_mapping")
