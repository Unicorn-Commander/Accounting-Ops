"""public schema: workspace.setup_progress — persisted Setup Assistant checklist

Revision ID: 0011_workspace_setup_progress
Revises: 0010_personal_access_tokens
Create Date: 2026-05-31

Adds a nullable JSONB column ``setup_progress`` to the ``public.workspace`` table
so the Setup Assistant left-rail checklist (Business profile / Entity / Tax facts
/ Banking / COA / Review) can persist across sessions.

Shape (versioned, owned by GET/PATCH /api/workspaces/{wid}/setup-progress):

  {
    "version": 1,
    "skipped": false,
    "completed_at": "2026-05-31T12:34:56Z" | null,
    "steps": {
      "business_profile": {"status": "complete|current|blocked|not_started",
                            "updated_at": "..."},
      "entity":            {"status": "...", "updated_at": "..."},
      "tax_facts":         {"status": "...", "updated_at": "..."},
      "banking":           {"status": "...", "updated_at": "..."},
      "coa":               {"status": "...", "updated_at": "..."},
      "review":            {"status": "...", "updated_at": "..."}
    }
  }

Backward-compatible:
  * NULLABLE, no default — existing workspaces show "not started" client-side.
  * Single new column; inherits existing public.workspace grants (workspace is
    read by everyone in the app; setup_progress UPDATEs come from authenticated
    /api/workspaces/{wid}/setup-progress only).

Idempotent (guarded by information_schema check).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_workspace_setup_progress"
down_revision: str | None = "0010_personal_access_tokens"
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
    if not _column_exists("public", "workspace", "setup_progress"):
        op.add_column(
            "workspace",
            sa.Column(
                "setup_progress",
                postgresql.JSONB(),
                nullable=True,
                comment=(
                    "Setup Assistant checklist state (version, skipped flag, "
                    "per-step status + updated_at). NULL until the user starts "
                    "Setup; written by /api/workspaces/{wid}/setup-progress."
                ),
            ),
            schema="public",
        )

    # Runtime role needs UPDATE only on the new column. The 'accounting_ops_app'
    # role is otherwise SELECT-only on public.workspace (no INSERT/DELETE), and
    # that's intentional: the rest of the row is owned by the
    # provisioning/billing path, not the runtime app. A column-level grant
    # keeps that boundary clean while letting setup-progress flow.
    op.execute(
        "GRANT UPDATE (setup_progress) ON public.workspace TO accounting_ops_app"
    )


def downgrade() -> None:
    if _column_exists("public", "workspace", "setup_progress"):
        # Revoke before drop so the catalog stays clean.
        op.execute(
            "REVOKE UPDATE (setup_progress) ON public.workspace FROM accounting_ops_app"
        )
        op.drop_column("workspace", "setup_progress", schema="public")
