"""per-workspace schema: signature_request

Revision ID: 0024_signature_request
Revises: 0023_professional_substitute
Create Date: 2026-06-25

The e-signature layer. One tenant table:

  signature_request  A request for a client's e-signature on a finished
                     work_product (a filled W-4P / 1040 / engagement letter, …).
                     REAL, not dormant: the taxpayer signs their OWN document,
                     so there is no UPL / IRS-transmission concern (unlike the
                     dormant e-FILE / EFTPS rail). Creation is gated behind a
                     per-workspace feature flag in the API layer. Entity-scoped.

                     ``provider`` is 'docuseal' (a self-hostable OSS e-signature
                     server) or 'manual' (download → wet-sign → re-upload). The
                     completed/signed PDF lands in ``signed_garage_key`` (object
                     store) or ``signed_path`` (local filesystem).

Grants / RLS / TRUNCATE posture (mirrors 0023 / 0022 / 0005)
============================================================
  - SELECT, INSERT, UPDATE -> accounting_ops_app (NO DELETE: status transitions
    are UPDATEs; signature requests are append-only at the row level).
  - DELETE/TRUNCATE explicitly REVOKEd + a BEFORE TRUNCATE guard trigger
    (forbid_append_only_truncate(), created in 0005).
  - RLS policy ``workspace_isolation`` keyed on the ``app.current_workspace`` GUC
    (default-deny when the GUC is unset).

Idempotent + re-runnable: every DDL is guarded by an information_schema check, so
this applies cleanly to a NEW workspace schema at provision time AND to EXISTING
schemas via upgrade_all_workspaces.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0024_signature_request"
down_revision: str | None = "0023_professional_substitute"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
SIGNATURE_REQUEST = "signature_request"


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


def _index_exists(schema: str, index: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM pg_indexes
                WHERE schemaname = :schema AND indexname = :index
                """
            ),
            {"schema": schema, "index": index},
        ).first()
    )


def _harden(schema: str, table: str) -> None:
    """Apply the least-privilege grants + TRUNCATE guard + RLS to ``table``."""
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{table} TO {RUNTIME_ROLE}'
    )
    op.execute(f'REVOKE DELETE, TRUNCATE ON "{schema}".{table} FROM {RUNTIME_ROLE}')
    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate "
        f"BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
    op.execute(
        f"""
        CREATE POLICY workspace_isolation ON {table}
          USING (
            workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
          )
          WITH CHECK (
            workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
          )
        """
    )


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0024 requires -x tenant=<schema>")

    if not _table_exists(schema, SIGNATURE_REQUEST):
        op.create_table(
            SIGNATURE_REQUEST,
            sa.Column(
                "id", sa.UUID(), nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column("work_product_id", sa.UUID(), nullable=False),
            sa.Column(
                "provider", sa.String(length=32), nullable=False,
                comment="'docuseal' | 'manual'.",
            ),
            sa.Column("external_id", sa.String(length=256), nullable=True),
            sa.Column(
                "status", sa.String(length=24), nullable=False,
                server_default=sa.text("'created'"),
                comment="created|sent|out_for_signature|signed|declined|canceled.",
            ),
            sa.Column("signer_name", sa.String(length=300), nullable=True),
            sa.Column("signer_email", sa.String(length=320), nullable=True),
            sa.Column("signing_url", sa.Text(), nullable=True),
            sa.Column(
                "signed_garage_key", sa.String(length=512), nullable=True,
                comment="Object-store key of the completed/signed PDF (Garage).",
            ),
            sa.Column(
                "signed_path", sa.Text(), nullable=True,
                comment="Filesystem path of the signed PDF (fallback when Garage off).",
            ),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"],
                name="signature_request_entity_fk", ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["work_product_id"], ["work_product.id"],
                name="signature_request_work_product_fk", ondelete="CASCADE",
            ),
            sa.CheckConstraint(
                "status IN ('created','sent','out_for_signature','signed',"
                "'declined','canceled')",
                name="ck_signature_request_status",
            ),
            comment=(
                "A request for a client's e-signature on a finished work product. "
                "REAL (taxpayer signs their own document — no UPL/IRS concern); "
                "creation gated behind a per-workspace feature flag."
            ),
        )

    if not _index_exists(schema, "ix_signature_request_workspace_id"):
        op.create_index(
            "ix_signature_request_workspace_id", SIGNATURE_REQUEST, ["workspace_id"]
        )
    if not _index_exists(schema, "ix_signature_request_entity_id"):
        op.create_index(
            "ix_signature_request_entity_id", SIGNATURE_REQUEST, ["entity_id"]
        )
    if not _index_exists(schema, "ix_signature_request_work_product"):
        op.create_index(
            "ix_signature_request_work_product",
            SIGNATURE_REQUEST, ["work_product_id"],
        )

    _harden(schema, SIGNATURE_REQUEST)


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0024 requires -x tenant=<schema>")

    op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {SIGNATURE_REQUEST}")
    op.execute(f"ALTER TABLE {SIGNATURE_REQUEST} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS {SIGNATURE_REQUEST}_no_truncate ON {SIGNATURE_REQUEST}")
    if _table_exists(schema, SIGNATURE_REQUEST):
        op.drop_table(SIGNATURE_REQUEST)
