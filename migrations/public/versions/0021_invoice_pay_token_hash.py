"""invoice_pay_token: store a hash, add expiry and revocation.

AO-TOK-2. 0018 stored the raw bearer token as the primary key, with no
expiry and no way to revoke it (the runtime role could not UPDATE or DELETE).
Now:

  * ``token_hash`` (hex SHA-256 of the token) is the primary key; the raw
    token is never stored. Existing rows are hashed in place, so a link that
    is already out keeps working until it expires.
  * ``expires_at`` NOT NULL, default now() + 60 days (existing rows:
    created_at + 60 days).
  * ``revoked_at`` NULL. The runtime role gets UPDATE on that one column only,
    under a workspace-scoped UPDATE policy, so it can revoke on paid / void.
    DELETE and TRUNCATE stay revoked.
  * SELECT: the public lookup matches ``token_hash = app.pay_token_hash``; a
    workspace session may also see its OWN rows (hashes only, needed for the
    revoke UPDATE and for INSERT ... RETURNING).

Production had 0 rows when this was written (measured 2026-11-23).

Downgrade restores the 0018 shape. A hash cannot be turned back into a token,
so every link minted after this upgrade stops resolving after a downgrade.

Revision ID: 0021_invoice_pay_token_hash
Revises: 0020_provisioning_state
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0021_invoice_pay_token_hash"
down_revision: str | None = "0020_provisioning_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
T = "public.invoice_pay_token"
_WS = "NULLIF(current_setting('app.current_workspace', true), '')::uuid"


def upgrade() -> None:
    # The owner is subject to FORCE RLS; lift it for the in-place backfill.
    op.execute(f"ALTER TABLE {T} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS invoice_pay_token_select ON {T}")
    op.execute(f"ALTER TABLE {T} ADD COLUMN token_hash text")
    op.execute(
        f"UPDATE {T} SET token_hash = encode(sha256(convert_to(token, 'UTF8')), 'hex')"
    )
    op.execute(f"ALTER TABLE {T} ALTER COLUMN token_hash SET NOT NULL")
    op.execute(f"ALTER TABLE {T} DROP CONSTRAINT invoice_pay_token_pkey")
    op.execute(f"ALTER TABLE {T} DROP COLUMN token")
    op.execute(f"ALTER TABLE {T} ADD CONSTRAINT invoice_pay_token_pkey PRIMARY KEY (token_hash)")
    op.execute(
        f"ALTER TABLE {T} ADD COLUMN expires_at timestamptz NOT NULL "
        "DEFAULT (now() + interval '60 days')"
    )
    op.execute(f"UPDATE {T} SET expires_at = created_at + interval '60 days'")
    op.execute(f"ALTER TABLE {T} ADD COLUMN revoked_at timestamptz")
    op.execute(
        "CREATE INDEX ix_invoice_pay_token_invoice "
        f"ON {T} (workspace_id, invoice_id)"
    )
    op.execute(f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY invoice_pay_token_select ON {T}
          FOR SELECT
          USING (token_hash = NULLIF(current_setting('app.pay_token_hash', true), ''))
        """
    )
    op.execute(
        f"""
        CREATE POLICY invoice_pay_token_select_workspace ON {T}
          FOR SELECT
          USING (workspace_id = {_WS})
        """
    )
    op.execute(
        f"""
        CREATE POLICY invoice_pay_token_revoke ON {T}
          FOR UPDATE
          USING (workspace_id = {_WS})
          WITH CHECK (workspace_id = {_WS})
        """
    )
    op.execute(f"GRANT UPDATE (revoked_at) ON {T} TO {RUNTIME_ROLE}")
    op.execute(f"REVOKE DELETE, TRUNCATE ON {T} FROM {RUNTIME_ROLE}")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {T} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"REVOKE UPDATE ON {T} FROM {RUNTIME_ROLE}")
    op.execute(f"DROP POLICY IF EXISTS invoice_pay_token_revoke ON {T}")
    op.execute(f"DROP POLICY IF EXISTS invoice_pay_token_select_workspace ON {T}")
    op.execute(f"DROP POLICY IF EXISTS invoice_pay_token_select ON {T}")
    op.execute("DROP INDEX IF EXISTS public.ix_invoice_pay_token_invoice")
    op.execute(f"ALTER TABLE {T} DROP COLUMN revoked_at")
    op.execute(f"ALTER TABLE {T} DROP COLUMN expires_at")
    # The hash stands in for the token: unique, but it no longer resolves.
    op.execute(f"ALTER TABLE {T} ADD COLUMN token text")
    op.execute(f"UPDATE {T} SET token = token_hash")
    op.execute(f"ALTER TABLE {T} ALTER COLUMN token SET NOT NULL")
    op.execute(f"ALTER TABLE {T} DROP CONSTRAINT invoice_pay_token_pkey")
    op.execute(f"ALTER TABLE {T} DROP COLUMN token_hash")
    op.execute(f"ALTER TABLE {T} ADD CONSTRAINT invoice_pay_token_pkey PRIMARY KEY (token)")
    op.execute(f"ALTER TABLE {T} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY invoice_pay_token_select ON {T}
          FOR SELECT
          USING (token = NULLIF(current_setting('app.pay_token', true), ''))
        """
    )
