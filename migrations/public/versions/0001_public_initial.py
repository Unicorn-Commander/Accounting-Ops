"""public schema: tenant registry + global enums

Revision ID: 0001_public_initial
Revises:
Create Date: 2026-05-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_public_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    sovereignty_mode = postgresql.ENUM(
        "standard", "strict", name="sovereignty_mode", create_type=False
    )
    sa.Enum("standard", "strict", name="sovereignty_mode").create(op.get_bind(), checkfirst=True)

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
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'provisioning'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        sa.UniqueConstraint("postgres_schema"),
        schema="public",
    )

    # Index on slug for fast tenant lookup by name
    op.create_index("ix_tenant_slug", "tenant", ["slug"], schema="public")


def downgrade() -> None:
    op.drop_table("tenant", schema="public")
    sa.Enum(name="sovereignty_mode").drop(op.get_bind(), checkfirst=True)
