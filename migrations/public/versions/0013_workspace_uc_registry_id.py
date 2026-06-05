"""public schema: workspace.uc_registry_id — the uc-registry tenant bridge

Revision ID: 0013_workspace_uc_registry_id
Revises: 0012_workspace_uc_org_id
Create Date: 2026-06-05

Adds a nullable, UNIQUE ``uc_registry_id`` (UUID) to ``public.workspace`` — the
Unicorn Commander **uc-registry** tenant id a workspace corresponds to.

Why: cross-app federation tokens (Brigade → Customer-Ops, etc.) carry the
uc-registry tenant id as their ``workspace_id`` claim, while AO's workspaces have
their own internal ids. ``app/platform/workspace_registry.resolve_internal_workspace_id``
maps a registry id → the internal AO workspace BEFORE membership resolution + RLS
binding, so AO's existing, proven membership authz applies unchanged — there is
NO authz bypass; a federation caller still needs the delegated user to be a
member of the resolved internal workspace.

This is the workspace-identity analog of the ``uc_org_id`` org bridge (0012), and
the AO-side ``backfill-register existing workspaces`` step of the uc-registry
convergence (handoff Phase 2).

Posture:
  * NULLABLE, no default — unknown until a workspace is registered/backfilled.
  * UNIQUE — one registry tenant ↔ at most one AO workspace.
  * SELECT-only at runtime (the resolver); set by the provisioning/admin path
    (owner DSN), NOT the runtime app — same boundary as the rest of the row.

Idempotent (information_schema-guarded).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_workspace_uc_registry_id"
down_revision: str | None = "0012_workspace_uc_org_id"
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


def _index_exists(schema: str, index: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes WHERE schemaname = :schema AND indexname = :index"
            ),
            {"schema": schema, "index": index},
        ).first()
    )


def upgrade() -> None:
    if not _column_exists("public", "workspace", "uc_registry_id"):
        op.add_column(
            "workspace",
            sa.Column(
                "uc_registry_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
                comment=(
                    "The uc-registry tenant id this workspace corresponds to. "
                    "Federation (Brigade) tokens carry this as their workspace_id "
                    "claim; resolve_internal_workspace_id maps it to this row's id "
                    "before membership + RLS. NULL until registered/backfilled; "
                    "set by the provisioning/admin path, not runtime."
                ),
            ),
            schema="public",
        )
    if not _index_exists("public", "uq_workspace_uc_registry_id"):
        op.create_index(
            "uq_workspace_uc_registry_id",
            "workspace",
            ["uc_registry_id"],
            unique=True,
            schema="public",
            postgresql_where=sa.text("uc_registry_id IS NOT NULL"),
        )


def downgrade() -> None:
    if _index_exists("public", "uq_workspace_uc_registry_id"):
        op.drop_index(
            "uq_workspace_uc_registry_id", table_name="workspace", schema="public"
        )
    if _column_exists("public", "workspace", "uc_registry_id"):
        op.drop_column("workspace", "uc_registry_id", schema="public")
