"""public schema: workspace.uc_org_id — the UC platform org a workspace belongs to

Revision ID: 0012_workspace_uc_org_id
Revises: 0011_workspace_setup_progress
Create Date: 2026-06-03

Adds a nullable ``uc_org_id`` text column to ``public.workspace`` — the Unicorn
Commander **org** (`org_<uuid>`, the Lago billing customer + Keycloak org) that a
workspace belongs to. This is the durable workspace→org bridge used by
``app/platform/org_resolver.py`` for per-org inference metering + entitlement.

Why a column (not only a token claim):
  The long-term mechanism is a Keycloak ``org_id`` claim in the uchub token (the
  resolver reads it FIRST, so every UC app gets the org for free). But today no
  such claim is emitted, so this column is the bridge: the resolver falls back to
  it, and an operator backfills it per workspace (just the dogfood ring to start).
  When the claim lands, the column is simply the fallback — zero rework.

Posture:
  * NULLABLE, no default — unknown until backfilled; the resolver returns None
    safely when it's unset (callers degrade to the platform key / today's path).
  * The runtime role only SELECTs it (the resolver). It is set by the
    provisioning/billing/admin path (owner DSN), NOT the runtime app — same
    boundary as the rest of the workspace row — so no UPDATE grant is added.

Idempotent (guarded by information_schema check).
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
                    "The UC platform org (org_<uuid>; Lago customer external_id + "
                    "Keycloak org) this workspace bills/meters under. NULL until "
                    "backfilled; resolver falls back to it when the token carries "
                    "no org claim. Set by the provisioning/admin path, not runtime."
                ),
            ),
            schema="public",
        )


def downgrade() -> None:
    if _column_exists("public", "workspace", "uc_org_id"):
        op.drop_column("workspace", "uc_org_id", schema="public")
