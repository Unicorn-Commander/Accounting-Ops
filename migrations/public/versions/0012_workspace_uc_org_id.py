"""public schema: workspace.uc_org_id — optional external org identifier

Revision ID: 0012_workspace_uc_org_id
Revises: 0011_workspace_setup_progress
Create Date: 2026-06-03

Adds a nullable ``uc_org_id`` text column to ``public.workspace``. It records an
optional external organization identifier a workspace belongs to, used by
identity / entitlement integrations. The provisioning path always writes this
column (NULL when unset), so it must exist for a workspace to be created.

Posture:
  * NULLABLE, no default — unset until an integration populates it; readers
    degrade safely to None.
  * Written by the provisioning / admin path (owner DSN), read-only for the
    runtime role — same boundary as the rest of the workspace row, so no extra
    runtime grant is required.

Idempotent (guarded by an information_schema check).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_workspace_uc_org_id"
down_revision: str | None = "0011_workspace_setup_progress"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(schema: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = :schema
                  AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"schema": schema, "table": table, "column": column},
        ).first()
    )


def upgrade() -> None:
    if not _column_exists("public", "workspace", "uc_org_id"):
        op.add_column(
            "workspace",
            sa.Column(
                "uc_org_id",
                sa.Text(),
                nullable=True,
                comment=(
                    "Optional external organization identifier this workspace "
                    "belongs to. NULL until set by the provisioning/admin path; "
                    "read-only for the runtime role."
                ),
            ),
            schema="public",
        )


def downgrade() -> None:
    if _column_exists("public", "workspace", "uc_org_id"):
        op.drop_column("workspace", "uc_org_id", schema="public")
