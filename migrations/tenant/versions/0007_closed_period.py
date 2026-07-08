"""per-workspace schema: closed_period table for period close/lock

Revision ID: 0007_closed_period
Revises: 0006_approval_queue_payload
Create Date: 2026-05-28

Adds the ``closed_period`` table that records which accounting periods have
been officially closed for a given entity.  Once a period is closed, the
posting path (``post_journal_entry``) refuses any entry whose effective_date
falls on or before the closed period_end date for that entity.

Schema (entity-scoped):
  entity_id      UUID        NOT NULL  references entity(id)
  period_end     DATE        NOT NULL
  closed_at      TIMESTAMPTZ NOT NULL  DEFAULT now()
  closed_by_uc_uid  TEXT     NOT NULL
  reason         TEXT
  workspace_id   UUID        NOT NULL  (RLS key)
  PRIMARY KEY (entity_id, period_end)

Grants / RLS / TRUNCATE guard mirror 0005 / 0006:
  - SELECT, INSERT, UPDATE on closed_period -> accounting_ops_app
    (no DELETE — period closes should be an ops action, not app-reversible)
  - BEFORE TRUNCATE guard (same pattern as journal_entry / event_log)
  - RLS policy workspace_isolation on workspace_id GUC

Idempotent + re-runnable (information_schema guards on every DDL change).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0007_closed_period"
down_revision: str | None = "0006_approval_queue_payload"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"


def _set_search_path() -> str:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
    return schema or ""


def _table_exists(schema: str, table: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = :schema AND table_name = :table
                """
            ),
            {"schema": schema, "table": table},
        ).first()
    )


def _column_exists(schema: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = :schema
                  AND table_name   = :table
                  AND column_name  = :column
                """
            ),
            {"schema": schema, "table": table, "column": column},
        ).first()
    )


def _trigger_exists(schema: str, table: str, trigger: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.triggers
                WHERE trigger_schema = :schema
                  AND event_object_table = :table
                  AND trigger_name = :trigger
                """
            ),
            {"schema": schema, "table": table, "trigger": trigger},
        ).first()
    )


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0007 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. Create closed_period table (idempotent)
    # ------------------------------------------------------------------
    if not _table_exists(schema, "closed_period"):
        op.create_table(
            "closed_period",
            sa.Column("entity_id",        sa.UUID(),              nullable=False),
            sa.Column("period_end",        sa.Date(),              nullable=False),
            sa.Column(
                "closed_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("closed_by_uc_uid", sa.Text(),              nullable=False),
            sa.Column("reason",           sa.Text(),              nullable=True),
            # workspace_id is required for RLS (mirrors every other tenant table).
            sa.Column("workspace_id",     sa.UUID(),              nullable=False),
            sa.PrimaryKeyConstraint("entity_id", "period_end"),
            # FK to entity is per-schema — use a named constraint so it can be
            # dropped cleanly in downgrade.
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"],
                name="closed_period_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "Records closed (locked) accounting periods per entity. "
                "post_journal_entry refuses entries with effective_date <= "
                "period_end for the same entity. Opened by POST "
                "/close-period; no reopen endpoint (ops action only)."
            ),
        )

    # ------------------------------------------------------------------
    # 2. Least-privilege grants for the runtime role
    #    SELECT, INSERT, UPDATE (no DELETE — same intent as RW_NO_DELETE)
    # ------------------------------------------------------------------
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".closed_period TO {RUNTIME_ROLE}'
    )
    # Belt-and-suspenders: strip DELETE/TRUNCATE if they ever leaked in.
    op.execute(
        f'REVOKE DELETE, TRUNCATE ON "{schema}".closed_period FROM {RUNTIME_ROLE}'
    )

    # ------------------------------------------------------------------
    # 3. BEFORE TRUNCATE guard
    #    forbid_append_only_truncate() was created in 0005; re-use it.
    # ------------------------------------------------------------------
    # Drop-if-exists then create (idempotent).
    op.execute("DROP TRIGGER IF EXISTS closed_period_no_truncate ON closed_period")
    op.execute(
        "CREATE TRIGGER closed_period_no_truncate "
        "BEFORE TRUNCATE ON closed_period "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    # ------------------------------------------------------------------
    # 4. Row-Level Security on workspace_id GUC
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE closed_period ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON closed_period")
    op.execute(
        """
        CREATE POLICY workspace_isolation ON closed_period
          USING (
            workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
          )
          WITH CHECK (
            workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
          )
        """
    )


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0007 requires -x tenant=<schema>")

    op.execute("DROP POLICY IF EXISTS workspace_isolation ON closed_period")
    op.execute("ALTER TABLE closed_period DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TRIGGER IF EXISTS closed_period_no_truncate ON closed_period")

    if _table_exists(schema, "closed_period"):
        op.drop_table("closed_period")
