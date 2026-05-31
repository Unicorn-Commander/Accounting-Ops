"""per-workspace schema: approval_status += 'skipped'

Revision ID: 0017_approval_status_skipped
Revises: 0016_monthly_account_balance
Create Date: 2026-05-31

Adds the 'skipped' value to the ``approval_status`` PostgreSQL ENUM so the
``/approvals`` UI can persist a "decide later" state instead of only tracking
it client-side (lost on refresh).

Why an enum value (vs a boolean column):
  * Aligns with existing pending/approved/rejected/canceled lifecycle.
  * The decide-endpoint + UI filters already key off the status column.
  * Indexes (ix_approval_queue_status) stay valid.

Idempotent: ``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` is a postgres builtin
that no-ops when the value is already present, so a second run is safe.

NOTE: ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block in
older Postgres; we explicitly commit the alembic transaction first.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0017_approval_status_skipped"
down_revision: str | None = "0016_monthly_account_balance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_search_path() -> str:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
    return schema or ""


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0017 requires -x tenant=<schema>")

    # Commit alembic's outer tx so we can run ADD VALUE (which requires its
    # own implicit transaction in older Postgres).
    bind = op.get_bind()
    bind.execute(sa.text("COMMIT"))
    try:
        bind.execute(
            sa.text("ALTER TYPE approval_status ADD VALUE IF NOT EXISTS 'skipped'")
        )
    finally:
        # Re-open a transaction so alembic's bookkeeping at the end succeeds.
        bind.execute(sa.text("BEGIN"))


def downgrade() -> None:
    # Postgres has no clean way to drop an enum value once added. The safest
    # downgrade is a no-op; data using 'skipped' would have to be migrated to
    # 'pending' first by the operator, then a manual DROP + CREATE replaces
    # the enum. We leave that to the operator.
    pass
