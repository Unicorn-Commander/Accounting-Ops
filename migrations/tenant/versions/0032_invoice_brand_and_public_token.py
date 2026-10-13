"""Invoice presentation, public token, customer-ops link, and per-entity brand.

Nullable columns only. Nothing is backfilled. Not applied by this change.

Revision ID: 0032_invoice_brand
Revises: 0031_quotes_seam
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0032_invoice_brand"
down_revision: str | None = "0031_quotes_seam"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_search_path() -> str:
    bind = op.get_bind()
    schema = context.get_x_argument(as_dictionary=True).get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
        return schema
    return bind.execute(sa.text("SELECT current_schema()")).scalar_one()


def _column_exists(schema: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"schema": schema, "table": table, "column": column},
        ).first()
    )


def _add(schema: str, table: str, column: str, type_: sa.types.TypeEngine) -> None:
    if not _column_exists(schema, table, column):
        op.add_column(table, sa.Column(column, type_, nullable=True))


def upgrade() -> None:
    schema = _set_search_path()
    _add(schema, "invoice", "customer_ops_customer_id", sa.String(length=128))
    _add(schema, "invoice", "presentation", JSONB())
    _add(schema, "invoice", "public_token", sa.String(length=128))
    for name, type_ in (
        ("brand_slug", sa.String(length=128)),
        ("logo_url", sa.Text()),
        ("color_primary", sa.String(length=16)),
        ("color_accent", sa.String(length=16)),
        ("color_background", sa.String(length=16)),
        ("footer_line", sa.Text()),
        ("from_address", sa.String(length=320)),
        ("reply_to", sa.String(length=320)),
        ("invoice_address", sa.Text()),
    ):
        _add(schema, "entity", name, type_)
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f'CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_public_token '
            f'ON "{schema}".invoice (public_token) '
            "WHERE public_token IS NOT NULL"
        )
    )
    bind.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_invoice_customer_ops
            ON invoice (workspace_id, customer_ops_customer_id)
            WHERE customer_ops_customer_id IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    _set_search_path()
    op.execute("DROP INDEX IF EXISTS uq_invoice_public_token")
    op.execute("DROP INDEX IF EXISTS ix_invoice_customer_ops")
    for column in ("customer_ops_customer_id", "presentation", "public_token"):
        op.drop_column("invoice", column)
    for column in (
        "brand_slug",
        "logo_url",
        "color_primary",
        "color_accent",
        "color_background",
        "footer_line",
        "from_address",
        "reply_to",
        "invoice_address",
    ):
        op.drop_column("entity", column)
