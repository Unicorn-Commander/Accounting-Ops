"""Public lookup for an unguessable invoice pay token.

Invoices live in books under workspace RLS, so a page with no login cannot
scan them. This table maps one token to one invoice. SELECT is allowed only
for the token currently in app.pay_token. INSERT is allowed only for the
workspace in app.current_workspace. RLS stays on.

Revision ID: 0018_invoice_pay_token
Revises: 0017_membership_audit
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_invoice_pay_token"
down_revision: str | None = "0017_membership_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"


def upgrade() -> None:
    op.create_table(
        "invoice_pay_token",
        sa.Column("token", sa.Text(), primary_key=True),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        schema="public",
    )
    op.execute(f"GRANT SELECT, INSERT ON public.invoice_pay_token TO {RUNTIME_ROLE}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE ON public.invoice_pay_token FROM {RUNTIME_ROLE}"
    )
    op.execute("ALTER TABLE public.invoice_pay_token ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.invoice_pay_token FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY invoice_pay_token_insert ON public.invoice_pay_token
          FOR INSERT
          WITH CHECK (
            workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
          )
        """
    )
    op.execute(
        """
        CREATE POLICY invoice_pay_token_select ON public.invoice_pay_token
          FOR SELECT
          USING (token = NULLIF(current_setting('app.pay_token', true), ''))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS invoice_pay_token_select ON public.invoice_pay_token")
    op.execute("DROP POLICY IF EXISTS invoice_pay_token_insert ON public.invoice_pay_token")
    op.execute("ALTER TABLE public.invoice_pay_token DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL ON public.invoice_pay_token FROM {RUNTIME_ROLE}")
    op.drop_table("invoice_pay_token", schema="public")
