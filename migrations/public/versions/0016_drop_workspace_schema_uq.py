"""public schema: drop the obsolete UNIQUE(workspace.postgres_schema)

Revision ID: 0016_drop_workspace_schema_uq
Revises: 0015_membership_status
Create Date: 2026-08-25

Shared-schema consolidation (tenant/0025+0026) points EVERY workspace's rows at
the single fixed ``books`` schema, isolated by FORCE RLS on ``workspace_id``.
``scripts/provision_workspace.create_workspace_record`` therefore writes
``postgres_schema='books'`` for every new workspace, keeping the column populated
(a constant) only for back-compat with readers that still SELECT it.

But ``public/0001_public_initial`` put a ``UNIQUE (postgres_schema)`` constraint
(``workspace_postgres_schema_key``) on the table, from the per-workspace-schema
era. In the shared model that constraint is obsolete AND a live landmine: the
FIRST workspace provisioned as ``'books'`` fits, and the SECOND collides on
``workspace_postgres_schema_key`` so provisioning fails with a duplicate-key
error. (Observed on the internal ring: 4 legacy workspaces still carry their old
``accounting_ops_<uuid>`` values while one post-consolidation workspace already
occupies ``'books'`` — the next new workspace would be the colliding second.)

This drops the constraint. It is a pure DDL constraint drop: no rows are read or
written, and the ``postgres_schema`` column itself is retained. Idempotent
(``IF EXISTS``), so it is a clean no-op where the constraint was already dropped
out of band.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_drop_workspace_schema_uq"
down_revision: str | None = "0015_membership_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent: safe if the constraint was already dropped directly on a
    # running deployment ahead of this migration landing.
    op.execute(
        "ALTER TABLE public.workspace "
        "DROP CONSTRAINT IF EXISTS workspace_postgres_schema_key"
    )


def downgrade() -> None:
    # Best-effort restore of the pre-consolidation UNIQUE constraint. It can only
    # succeed while every postgres_schema value is still distinct; once more than
    # one workspace shares 'books' (the shared-schema model actually in use) the
    # ADD would fail on duplicates. Guard so the outcome is a clear explanation
    # rather than a raw duplicate-key error, and so the downgrade is a no-op when
    # the constraint is already present.
    bind = op.get_bind()
    already = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conrelid = 'public.workspace'::regclass "
            "  AND conname = 'workspace_postgres_schema_key'"
        )
    ).first()
    if already:
        return
    dupes = bind.execute(
        sa.text(
            "SELECT count(*) FROM ("
            "  SELECT postgres_schema FROM public.workspace "
            "  GROUP BY postgres_schema HAVING count(*) > 1"
            ") d"
        )
    ).scalar_one()
    if dupes:
        raise RuntimeError(
            "Cannot restore UNIQUE(workspace.postgres_schema): "
            f"{dupes} schema value(s) are shared across workspaces (the "
            "shared-schema 'books' model). Uniqueness cannot be re-established "
            "without reassigning per-workspace schema names first."
        )
    op.execute(
        "ALTER TABLE public.workspace "
        "ADD CONSTRAINT workspace_postgres_schema_key UNIQUE (postgres_schema)"
    )
