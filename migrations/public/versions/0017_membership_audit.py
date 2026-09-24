"""public schema: append-only workspace membership audit trail"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_membership_audit"
down_revision: str | None = "0016_drop_workspace_schema_uq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"


def upgrade() -> None:
    op.create_table(
        "membership_audit",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("actor_uc_uid", sa.Text(), nullable=True),
        sa.Column(
            "action",
            sa.Text(),
            nullable=False,
        ),
        sa.Column("target_user_id", sa.UUID(), nullable=True),
        sa.Column("invited_email", sa.Text(), nullable=True),
        sa.Column("invitation_id", sa.UUID(), nullable=True),
        sa.Column("old_role", sa.Text(), nullable=True),
        sa.Column("new_role", sa.Text(), nullable=True),
        sa.Column("old_entity_scope", postgresql.JSONB(), nullable=True),
        sa.Column("new_entity_scope", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["public.workspace.id"]),
        sa.CheckConstraint(
            "action IN ('invitation_create', 'invitation_cancel', "
            "'invitation_accept', 'role_change', 'entity_scope_change', 'removal')",
            name="ck_membership_audit_action",
        ),
        schema="public",
    )
    op.create_index(
        "ix_membership_audit_workspace_occurred",
        "membership_audit",
        ["workspace_id", "occurred_at"],
        schema="public",
    )
    op.execute(f"GRANT SELECT, INSERT ON public.membership_audit TO {RUNTIME_ROLE}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE ON public.membership_audit FROM {RUNTIME_ROLE}"
    )
    op.execute("ALTER TABLE public.membership_audit ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY workspace_isolation ON public.membership_audit
          USING (
            workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
          )
          WITH CHECK (
            workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
          )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON public.membership_audit")
    op.execute("ALTER TABLE public.membership_audit DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL ON public.membership_audit FROM {RUNTIME_ROLE}")
    op.drop_index(
        "ix_membership_audit_workspace_occurred",
        table_name="membership_audit",
        schema="public",
    )
    op.drop_table("membership_audit", schema="public")
