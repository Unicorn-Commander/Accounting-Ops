"""per-workspace schema: relax journal_entry FK requirements

Revision ID: 0004_journal_relax_fks
Revises: 0003_journal_v2
Create Date: 2026-05-22

The 0001_tenant_initial schema required event_id (FK→event_log) and
counterparty_id (FK→counterparty) as NOT NULL. Those subsystems aren't
built yet, so the posting code can't satisfy them. Drop both FK
constraints and make both columns nullable so posts can succeed; the
subsystems will re-enforce when they land.

Also fixes source_document_hash being NOT NULL — it was meant for
receipt-PDF hashes but the posting path uses the chain hash for now.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0004_journal_relax_fks"
down_revision: str | None = "0003_journal_v2"
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
    op.execute("ALTER TABLE journal_entry DROP CONSTRAINT IF EXISTS journal_entry_event_id_fkey")
    op.execute("ALTER TABLE journal_entry DROP CONSTRAINT IF EXISTS journal_entry_counterparty_id_fkey")
    op.alter_column("journal_entry", "event_id", nullable=True)
    op.alter_column("journal_entry", "counterparty_id", nullable=True)
    op.alter_column("journal_entry", "source_document_hash", nullable=True)
    op.alter_column("journal_entry", "reasoning_trace", nullable=True)
    op.alter_column("journal_entry", "rule_version_applied", nullable=True)
    op.alter_column("journal_entry", "model_version", nullable=True)


def downgrade() -> None:
    _set_search_path()
    op.alter_column("journal_entry", "event_id", nullable=False)
    op.alter_column("journal_entry", "counterparty_id", nullable=False)
    op.alter_column("journal_entry", "source_document_hash", nullable=False)
    op.alter_column("journal_entry", "reasoning_trace", nullable=False)
    op.alter_column("journal_entry", "rule_version_applied", nullable=False)
    op.alter_column("journal_entry", "model_version", nullable=False)
