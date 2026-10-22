"""public schema: workspace += onboarded_by_user_id (console-onboarded client workspaces)

``POST /api/me/clients`` (Team → Clients → "Add client") provisions a workspace
for a client of the caller's practice. The practitioner who does that is seated
as the workspace's ``owner`` so the practice can run the books end to end —
connect the bank (admin floor), invite the client-owner and their own team
(owner floor), close periods. That also means "holds a ``cpa`` seat" no longer
identifies a practitioner, so the registry records who onboarded each
workspace and the persona router counts those.

Nullable, no backfill: existing workspaces were provisioned by operators or by
self-serve first-workspace onboarding, never through the console.

Revision ID: 0019_workspace_onboarded_by
Revises: 0018_invoice_pay_token
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_workspace_onboarded_by"
down_revision: str | None = "0018_invoice_pay_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMN = "onboarded_by_user_id"
FK_NAME = "fk_workspace_onboarded_by_user"
INDEX_NAME = "ix_workspace_onboarded_by_user_id"


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


def _constraint_exists(schema: str, table: str, name: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_schema = :schema AND table_name = :table
                  AND constraint_name = :name
                """
            ),
            {"schema": schema, "table": table, "name": name},
        ).first()
    )


def _index_exists(schema: str, name: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes WHERE schemaname = :schema AND indexname = :name"
            ),
            {"schema": schema, "name": name},
        ).first()
    )


def upgrade() -> None:
    if not _column_exists("public", "workspace", COLUMN):
        op.add_column(
            "workspace",
            sa.Column(
                COLUMN,
                sa.UUID(),
                nullable=True,
                comment=(
                    "user_account.id of the practitioner who provisioned this "
                    "workspace through POST /api/me/clients (the CPA console). "
                    "NULL for operator- or self-provisioned workspaces."
                ),
            ),
            schema="public",
        )
    if not _constraint_exists("public", "workspace", FK_NAME):
        op.create_foreign_key(
            FK_NAME,
            "workspace",
            "user_account",
            [COLUMN],
            ["id"],
            source_schema="public",
            referent_schema="public",
            ondelete="SET NULL",
        )
    if not _index_exists("public", INDEX_NAME):
        op.create_index(
            INDEX_NAME,
            "workspace",
            [COLUMN],
            schema="public",
            postgresql_where=sa.text(f"{COLUMN} IS NOT NULL"),
        )


def downgrade() -> None:
    if _index_exists("public", INDEX_NAME):
        op.drop_index(INDEX_NAME, table_name="workspace", schema="public")
    if _constraint_exists("public", "workspace", FK_NAME):
        op.drop_constraint(FK_NAME, "workspace", schema="public", type_="foreignkey")
    if _column_exists("public", "workspace", COLUMN):
        op.drop_column("workspace", COLUMN, schema="public")
