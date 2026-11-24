"""credential_access_log + membership_audit: FORCE ROW LEVEL SECURITY.

AO111-R2-F9 (FORCE parity with workspace_invitation, 0022). Both tables
already have RLS ENABLEd with a workspace_isolation policy on
app.current_workspace (0003, 0017); only FORCE was missing, so the table
owner bypassed the policy.

Why FORCE is safe here (checked before applying it):

  * Nothing reads either table as the owner across workspaces: scripts/ has
    no reference to them, and app/ only INSERTs, always after binding the
    transaction-local workspace GUC (credentials._log after
    _set_workspace_guc; members._write_membership_audit and
    member_removal.remove_person call set_config first).
  * The integration suite reads membership_audit through the owner DSN,
    which in the compose stack is the POSTGRES_USER superuser; a superuser
    bypasses RLS even under FORCE, so those reads are unchanged.

tenant_credentials is deliberately NOT forced: stripe_sync and ebay_sync
discover credentials across workspaces as the table owner (AO-RLS-1).

Downgrade restores the 0003 / 0017 state (ENABLE without FORCE).

Revision ID: 0023_force_rls_audit_logs
Revises: 0022_workspace_invitation_rls
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0023_force_rls_audit_logs"
down_revision: str | None = "0022_workspace_invitation_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("public.credential_access_log", "public.membership_audit")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
