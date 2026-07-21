"""public schema: workspace_membership += status + is_default (cross-app contract)

Revision ID: 0015_membership_status
Revises: 0014_regulatory_feed_health
Create Date: 2026-07-21

Suite convergence (SUITE-IDENTITY §3; ADR-1; Spec 1 §0.3) — ADDITIVE, reversible,
behaviour-preserving. Extracted from feat/suite-identity-additive (774bdcb, where
it was 0013) and renumbered onto the current public chain; the superseded
registry-mirror pillar of that branch is deliberately NOT brought along.

The cross-app membership contract is
``(user_key, workspace_id, role, status active|suspended|revoked, is_default)``.
AO's ``public.workspace_membership`` already carries (user, workspace, role,
entity_scope); this adds the two missing columns so AO conforms to the shape CO
``user_tenant_membership`` and PO ``Membership`` also implement.

  * ``status`` TEXT NOT NULL DEFAULT 'active' CHECK in (active, suspended, revoked).
    DEFAULT 'active' ⇒ every existing membership row is active ⇒ the new
    ``AND m.status='active'`` filter added to ``_resolve_membership`` (and the two
    ``list_my_workspaces`` surfaces) is a NO-OP today and becomes load-bearing only
    when an operator suspends/revokes a specific membership.
  * ``is_default`` BOOLEAN NOT NULL DEFAULT false. A partial UNIQUE index enforces
    at most one default workspace per user.

Membership ``status`` (suspend ONE user's access to a workspace) is orthogonal to
``workspace.status`` (suspend the whole workspace). All coexist.

Idempotent (guarded by catalog checks). The runtime role's existing TABLE-LEVEL
``GRANT SELECT, INSERT, UPDATE ON public.workspace_membership`` (public/0003,
upgrade step 2) covers the new columns automatically — verified against
0003_rls_and_runtime_grants.py; no extra grant needed.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_membership_status"
down_revision: str | None = "0014_regulatory_feed_health"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMBERSHIP_STATUS_VALUES = ("active", "suspended", "revoked")


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
    if not _column_exists("public", "workspace_membership", "status"):
        op.add_column(
            "workspace_membership",
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default="active",
                comment=(
                    "Membership status (SUITE-IDENTITY §3): active|suspended|"
                    "revoked. Orthogonal to workspace.status. Default 'active' "
                    "so the _resolve_membership status filter is behaviour-"
                    "preserving until an operator suspends a user."
                ),
            ),
            schema="public",
        )
        op.create_check_constraint(
            "ck_workspace_membership_status",
            "workspace_membership",
            "status IN ('active', 'suspended', 'revoked')",
            schema="public",
        )

    if not _column_exists("public", "workspace_membership", "is_default"):
        op.add_column(
            "workspace_membership",
            sa.Column(
                "is_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
                comment="User's default workspace (SUITE-IDENTITY §3). At most one per user.",
            ),
            schema="public",
        )

    # One default workspace per user (partial unique on is_default rows).
    if not _index_exists("public", "uq_membership_one_default"):
        op.create_index(
            "uq_membership_one_default",
            "workspace_membership",
            ["user_id"],
            unique=True,
            postgresql_where=sa.text("is_default"),
            schema="public",
        )


def downgrade() -> None:
    if _index_exists("public", "uq_membership_one_default"):
        op.drop_index(
            "uq_membership_one_default",
            table_name="workspace_membership",
            schema="public",
        )
    if _column_exists("public", "workspace_membership", "is_default"):
        op.drop_column("workspace_membership", "is_default", schema="public")
    if _column_exists("public", "workspace_membership", "status"):
        op.drop_constraint(
            "ck_workspace_membership_status",
            "workspace_membership",
            schema="public",
            type_="check",
        )
        op.drop_column("workspace_membership", "status", schema="public")
