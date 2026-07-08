"""per-workspace schema: entity legal-fact columns.

Revision ID: 0011_entity_corp_facts
Revises: 0010_approval_recommendation
Create Date: 2026-05-29

Adds the columns the platform was already querying but that the original
schema never declared:

- ``incorporated_at``  DATE NULL — legal formation date; drives short-year
  detection in /api/workspaces/{wid}/tax-status.  See app/api/tax_status.py.
- ``state_of_incorporation``  VARCHAR(2) NULL — e.g. 'SC' for South
  Carolina-incorporated entities.  Used by jurisdiction inference.
- ``mailing_address``  TEXT NULL — IRS mailing address on file (CP 575 or
  most recent return).  Different from the SoS registered office.
- ``registered_agent``  TEXT NULL — name of the registered agent on file.
- ``authorized_shares``  BIGINT NULL — total shares authorized in Articles.
- ``par_value_cents``  INTEGER NULL — par value in cents (NULL = no-par).

All nullable so existing rows are unaffected.  Backfill is application-
level (the Articles + EIN extractor populates these from corporate
documents stored under the workspace's Garage bucket).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_entity_corp_facts"
down_revision: str | None = "0010_approval_recommendation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("entity", sa.Column("incorporated_at", sa.Date(), nullable=True))
    op.add_column(
        "entity", sa.Column("state_of_incorporation", sa.String(length=2), nullable=True)
    )
    op.add_column("entity", sa.Column("mailing_address", sa.Text(), nullable=True))
    op.add_column("entity", sa.Column("registered_agent", sa.Text(), nullable=True))
    op.add_column("entity", sa.Column("authorized_shares", sa.BigInteger(), nullable=True))
    op.add_column("entity", sa.Column("par_value_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("entity", "par_value_cents")
    op.drop_column("entity", "authorized_shares")
    op.drop_column("entity", "registered_agent")
    op.drop_column("entity", "mailing_address")
    op.drop_column("entity", "state_of_incorporation")
    op.drop_column("entity", "incorporated_at")
