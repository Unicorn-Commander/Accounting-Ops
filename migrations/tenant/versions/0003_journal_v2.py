"""per-workspace schema: chart_of_account + journal_entry v2 + sidecars

Revision ID: 0003_journal_v2
Revises: 0002_account_mappings
Create Date: 2026-05-22

Augments the existing journal_entry from 0001_tenant_initial to support:
  - Double-entry: debit_account + credit_account columns (canonical_form strings)
  - Hash chain: prev_hash + this_hash per-entity for tamper detection
  - Materiality gate: materiality_tier ('L' < $75, 'M' $75-1000, 'H' > $1000)
  - Idempotency: (source, source_external_id) UNIQUE so SimpleFIN dupes can't double-post
  - Category as text (drop the orphaned category_id UUID with no FK target)

Plus three sibling tables:
  - chart_of_account   : per-entity GL accounts
  - category_override  : user corrections that train future categorizations
  - posting_log        : every post attempt (success or refused), for audit

The append-only behavior (forbid_journal_entry_mutation trigger from 0001)
stays intact. Reversals are NEW entries that point back via reverses_entry_id.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003_journal_v2"
down_revision: str | None = "0002_account_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_search_path() -> None:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))


def upgrade() -> None:
    _set_search_path()

    # =====================================================================
    # chart_of_account
    # =====================================================================
    op.create_table(
        "chart_of_account",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=False),
        sa.Column(
            "account_number",
            sa.String(length=8),
            nullable=False,
            comment="4-char numeric code (1010, 6210, ...)",
        ),
        sa.Column(
            "account_name",
            sa.String(length=128),
            nullable=False,
        ),
        sa.Column(
            "canonical_form",
            sa.String(length=160),
            nullable=False,
            comment="'{number}·{name}' used in journal_entry.debit_account/credit_account",
        ),
        sa.Column(
            "account_type",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column(
            "normal_balance",
            sa.String(length=8),
            nullable=False,
        ),
        sa.Column(
            "linked_external_source",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "linked_external_account_id",
            sa.String(length=256),
            nullable=True,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "is_system",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity_id", "account_number", name="uq_coa_entity_number"),
        sa.UniqueConstraint("entity_id", "canonical_form", name="uq_coa_entity_canonical"),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "account_type IN ('asset','liability','equity','revenue','cogs',"
            "'expense','other_income','other_expense','tax')",
            name="ck_coa_account_type",
        ),
        sa.CheckConstraint(
            "normal_balance IN ('debit','credit')",
            name="ck_coa_normal_balance",
        ),
    )
    op.create_index("ix_coa_entity_active", "chart_of_account", ["entity_id", "is_active"])
    op.create_index("ix_coa_canonical", "chart_of_account", ["canonical_form"])
    op.create_index(
        "ix_coa_linked_external",
        "chart_of_account",
        ["linked_external_source", "linked_external_account_id"],
        postgresql_where=sa.text("linked_external_account_id IS NOT NULL"),
    )

    # =====================================================================
    # journal_entry — augment for double-entry + hash chain + materiality
    # =====================================================================
    # Drop the orphaned category_id (no FK target was ever created in 0001).
    op.drop_column("journal_entry", "category_id")

    op.add_column(
        "journal_entry",
        sa.Column(
            "debit_account",
            sa.String(length=160),
            nullable=True,
            comment="canonical_form of debited account",
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "credit_account",
            sa.String(length=160),
            nullable=True,
            comment="canonical_form of credited account",
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "category",
            sa.String(length=64),
            nullable=True,
            comment="Human-readable category from categorization engine",
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "materiality_tier",
            sa.String(length=8),
            nullable=True,
            comment="'L' <$75 | 'M' $75-1000 | 'H' >$1000",
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=True,
            comment="'simplefin' | 'ebay' | 'manual' | 'reversal' | 'opening_balance'",
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "source_external_id",
            sa.String(length=256),
            nullable=True,
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "description",
            sa.String(length=512),
            nullable=True,
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "payee",
            sa.String(length=256),
            nullable=True,
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "memo",
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "prev_hash",
            sa.LargeBinary(length=32),
            nullable=True,
            comment="Previous entry's this_hash for this entity. Genesis = 32 zero bytes.",
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "this_hash",
            sa.LargeBinary(length=32),
            nullable=True,
            comment="sha256(prev_hash || canonical_entry_payload)",
        ),
    )

    op.add_column(
        "journal_entry",
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'posted'"),
            comment="'posted' | 'reversed'",
        ),
    )
    op.add_column(
        "journal_entry",
        sa.Column(
            "receipt_link_id",
            sa.UUID(),
            nullable=True,
            comment="FK into receipt_transaction_link (lives in public schema) if matched receipt evidence exists",
        ),
    )

    op.create_index(
        "ix_je_workspace_effective",
        "journal_entry",
        ["workspace_id", "effective_date"],
    )
    op.create_index(
        "ix_je_status_effective",
        "journal_entry",
        ["status", "effective_date"],
    )
    op.create_index(
        "ix_je_debit_account",
        "journal_entry",
        ["entity_id", "debit_account"],
        postgresql_where=sa.text("debit_account IS NOT NULL"),
    )
    op.create_index(
        "ix_je_credit_account",
        "journal_entry",
        ["entity_id", "credit_account"],
        postgresql_where=sa.text("credit_account IS NOT NULL"),
    )
    op.create_index(
        "ix_je_source_external",
        "journal_entry",
        ["entity_id", "source", "source_external_id"],
        postgresql_where=sa.text("source_external_id IS NOT NULL"),
    )
    # Idempotency: same upstream txn cannot post twice into the same entity.
    op.create_unique_constraint(
        "uq_je_entity_source_external",
        "journal_entry",
        ["entity_id", "source", "source_external_id"],
    )

    # =====================================================================
    # category_override
    # =====================================================================
    op.create_table(
        "category_override",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column(
            "pattern",
            sa.String(length=256),
            nullable=False,
            comment="Substring matched against (payee + description + memo) lowercased",
        ),
        sa.Column(
            "category",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "debit_account",
            sa.String(length=160),
            nullable=True,
        ),
        sa.Column(
            "credit_account",
            sa.String(length=160),
            nullable=True,
        ),
        sa.Column(
            "created_by_uc_uid",
            sa.String(length=256),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "match_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "entity_id", "pattern", name="uq_cat_override_pattern"
        ),
        sa.ForeignKeyConstraint(["entity_id"], ["entity.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_cat_override_workspace", "category_override", ["workspace_id"])

    # =====================================================================
    # posting_log
    # =====================================================================
    op.create_table(
        "posting_log",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_external_id", sa.String(length=256), nullable=True),
        sa.Column(
            "outcome",
            sa.String(length=32),
            nullable=False,
            comment="posted | queued_for_confirm | blocked_materiality | "
                    "blocked_unmapped | blocked_unknown_category | "
                    "blocked_idempotent | reversed",
        ),
        sa.Column("journal_entry_id", sa.UUID(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_uc_uid", sa.String(length=256), nullable=False),
        sa.Column("actor_agent_id", sa.String(length=256), nullable=True),
        sa.Column("amount_cents", sa.BigInteger(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["journal_entry_id"], ["journal_entry.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "outcome IN ('posted','queued_for_confirm','blocked_materiality',"
            "'blocked_unmapped','blocked_unknown_category','blocked_idempotent','reversed')",
            name="ck_posting_log_outcome",
        ),
    )
    op.create_index(
        "ix_posting_log_workspace_created",
        "posting_log",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_posting_log_outcome",
        "posting_log",
        ["workspace_id", "outcome", "created_at"],
    )

    # Updated-at trigger for chart_of_account (mirrors the pattern from 0002).
    op.execute(
        """
        CREATE OR REPLACE FUNCTION coa_set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_coa_updated_at
            BEFORE UPDATE ON chart_of_account
            FOR EACH ROW
            EXECUTE FUNCTION coa_set_updated_at();
        """
    )


def downgrade() -> None:
    _set_search_path()

    op.execute("DROP TRIGGER IF EXISTS trg_coa_updated_at ON chart_of_account")
    op.execute("DROP FUNCTION IF EXISTS coa_set_updated_at()")

    op.drop_index("ix_posting_log_outcome", table_name="posting_log")
    op.drop_index("ix_posting_log_workspace_created", table_name="posting_log")
    op.drop_table("posting_log")

    op.drop_index("ix_cat_override_workspace", table_name="category_override")
    op.drop_table("category_override")

    op.drop_constraint("uq_je_entity_source_external", "journal_entry", type_="unique")
    op.drop_index("ix_je_source_external", table_name="journal_entry")
    op.drop_index("ix_je_credit_account", table_name="journal_entry")
    op.drop_index("ix_je_debit_account", table_name="journal_entry")
    op.drop_index("ix_je_status_effective", table_name="journal_entry")
    op.drop_index("ix_je_workspace_effective", table_name="journal_entry")

    for col in (
        "receipt_link_id",
        "status",
        "this_hash",
        "prev_hash",
        "memo",
        "payee",
        "description",
        "source_external_id",
        "source",
        "materiality_tier",
        "category",
        "credit_account",
        "debit_account",
    ):
        op.drop_column("journal_entry", col)

    op.add_column(
        "journal_entry",
        sa.Column("category_id", sa.UUID(), nullable=True),
    )

    op.drop_index("ix_coa_linked_external", table_name="chart_of_account")
    op.drop_index("ix_coa_canonical", table_name="chart_of_account")
    op.drop_index("ix_coa_entity_active", table_name="chart_of_account")
    op.drop_table("chart_of_account")
