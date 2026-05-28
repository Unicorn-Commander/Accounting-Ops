"""public schema: least-privilege runtime grants for the receipt-worker + reconciler

Revision ID: 0004_worker_runtime_grants
Revises: 0003_rls_and_runtime_grants
Create Date: 2026-05-27

ARCHITECTURE NOTE (2026-05-28)
-------------------------------
This revision is intentionally a NO-OP. It exists solely to preserve the
Alembic revision chain (downstream revisions depend on this revision id).

The GRANT/REVOKE logic that was originally here has been moved to
``scripts/grant_worker_runtime.sql`` and MUST be run as a separate step in
the deploy runbook (DEPLOY.md, Step 4) AFTER the sibling repos' migrations
have created the worker tables.

Why the grants were removed from this Alembic migration
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The worker tables (worker_runs, worker_extractions, worker_dedup_index,
worker_reconcile_runs, receipt_transaction_link) are created by SIBLING
repos' own Alembic chains (receipt-worker 0001_initial,
reconciler 0001_reconciler), not by this main-repo's migrations.

Alembic provides no mechanism to model cross-repo migration ordering.
On a fresh stack this migration runs as part of the main-repo alembic
``upgrade head`` BEFORE the sibling tables exist, so every GRANT is silently
skipped (via the ``to_regclass`` / ``IF EXISTS`` guard). Because Alembic
stamps the revision immediately and never re-runs an already-stamped
migration, those grants are permanently lost — ``accounting_ops_app`` ends
up with zero privileges on all worker tables.

The fix: ``scripts/grant_worker_runtime.sql`` is idempotent, lives outside
Alembic, and is executed explicitly after sibling migrations have run. See
DEPLOY.md Step 4 for the exact command.
"""
from collections.abc import Sequence

from alembic import op  # noqa: F401  (imported for chain integrity)

revision: str = "0004_worker_runtime_grants"
down_revision: str | None = "0003_rls_and_runtime_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Intentional no-op. See module docstring.
    # Grant logic lives in scripts/grant_worker_runtime.sql.
    pass


def downgrade() -> None:
    # Intentional no-op. See module docstring.
    # To revoke grants, run scripts/grant_worker_runtime.sql manually and
    # remove the GRANT statements, or connect as the owner role and issue
    # REVOKE SELECT, INSERT, UPDATE ON <table> FROM accounting_ops_app;
    pass
