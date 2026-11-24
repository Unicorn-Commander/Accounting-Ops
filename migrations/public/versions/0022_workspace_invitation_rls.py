"""workspace_invitation: ENABLE + FORCE RLS with per-command policies.

AO-RLS-1. public.workspace_invitation (0008) carries workspace_id but had no
row-level security at all: only the app's ``WHERE workspace_id = :wid``
filters isolated it. Now:

  * ENABLE + FORCE ROW LEVEL SECURITY (FORCE so the table owner is bound too).
  * Per-command policies keyed on the transaction-local workspace GUC, the
    0026 shape: SELECT / INSERT / UPDATE / DELETE each require
    ``workspace_id = app.current_workspace``. DELETE has no grant to the
    runtime role (0008); its policy is present for parity and is harmless.
  * Accept-by-token: the accepting caller has no workspace yet, so one more
    SELECT-only policy matches ``token_hash = app.invitation_token_hash``
    (same pattern as invoice_pay_token, 0021). A transaction sees exactly the
    row whose hash it already holds; knowing a hash requires the raw token.
    A SECURITY DEFINER lookup was not used: under FORCE the definer (the
    table owner) is itself filtered, so it would see zero rows.

The expression is inlined rather than calling public.current_workspace_id():
that helper is created by the TENANT chain (tenant 0025), which runs after
this public chain on a fresh database.

Downgrade drops the policies and restores the 0008 state (RLS off).

Revision ID: 0022_workspace_invitation_rls
Revises: 0021_invoice_pay_token_hash
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0022_workspace_invitation_rls"
down_revision: str | None = "0021_invoice_pay_token_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

T = "public.workspace_invitation"
_WS = "workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid"
_TOKEN = "token_hash = NULLIF(current_setting('app.invitation_token_hash', true), '')"
_POLICIES = (
    "workspace_invitation_ws_sel",
    "workspace_invitation_by_token",
    "workspace_invitation_ws_ins",
    "workspace_invitation_ws_upd",
    "workspace_invitation_ws_del",
)


def upgrade() -> None:
    for name in _POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {name} ON {T}")
    op.execute(f"ALTER TABLE {T} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY workspace_invitation_ws_sel ON {T} FOR SELECT USING ({_WS})")
    op.execute(f"CREATE POLICY workspace_invitation_by_token ON {T} FOR SELECT USING ({_TOKEN})")
    op.execute(f"CREATE POLICY workspace_invitation_ws_ins ON {T} FOR INSERT WITH CHECK ({_WS})")
    op.execute(
        f"CREATE POLICY workspace_invitation_ws_upd ON {T} FOR UPDATE "
        f"USING ({_WS}) WITH CHECK ({_WS})"
    )
    op.execute(f"CREATE POLICY workspace_invitation_ws_del ON {T} FOR DELETE USING ({_WS})")


def downgrade() -> None:
    for name in _POLICIES:
        op.execute(f"DROP POLICY IF EXISTS {name} ON {T}")
    op.execute(f"ALTER TABLE {T} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {T} DISABLE ROW LEVEL SECURITY")
