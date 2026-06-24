"""per-workspace schema: document_extraction (tax-document OCR results)

Revision ID: 0022_document_extraction
Revises: 0021_sentinel_dedupe_realign
Create Date: 2026-06-23

Durable, queryable home for what the local vision VLM read off an uploaded tax
document (1099-R, W-2, SSA-1099, K-1, 1098, IRS notices, …). One row per source
``receipt``. Written best-effort by a background task after upload
(app/api/documents.py); a FAILED extraction still records a row
(status='failed', error set) so an unreadable doc is visible, never silent.

AO owns the OCR/extraction; Tax-Planning-Ops CONSUMES these rows over the
existing federation (by entity_id + tax_year) instead of re-running OCR — hence
the (entity_id, doc_type, tax_year) index. NEVER stores a computed value:
``fields`` holds verbatim transcribed labeled box values.

One entity-scoped table (carries workspace_id for RLS, mirroring every other
tenant table from 0001/0003/0019):

  document_extraction   one tax-doc extraction result, keyed to a receipt.

Idempotency
===========
``UNIQUE (receipt_id)`` — re-extracting the SAME document UPSERTs the one row
(the upload path dedups on content_hash, so a receipt is itself unique per file).

Grants / RLS / TRUNCATE posture (mirrors 0019 / 0005)
=====================================================
  - SELECT, INSERT, UPDATE -> accounting_ops_app (NO DELETE; re-extraction is an
    UPDATE via ON CONFLICT, never a row delete).
  - DELETE/TRUNCATE explicitly REVOKEd + a BEFORE TRUNCATE guard trigger.
  - RLS policy ``workspace_isolation`` keyed on the ``app.current_workspace`` GUC
    (default-deny when the GUC is unset).

Idempotent + re-runnable: every DDL is guarded by an information_schema check, so
this applies cleanly to a NEW workspace schema at provision time AND to EXISTING
schemas via upgrade_all_workspaces.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0022_document_extraction"
down_revision: str | None = "0021_sentinel_dedupe_realign"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
TABLE = "document_extraction"


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


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0022 requires -x tenant=<schema>")

    if not _table_exists(schema, TABLE):
        op.create_table(
            TABLE,
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "receipt_id",
                sa.UUID(),
                nullable=False,
                comment="The source uploaded document this extraction describes.",
            ),
            sa.Column(
                "doc_type",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'other'"),
                comment="Classified tax form type; 'other' = not a recognized tax doc.",
            ),
            sa.Column(
                "tax_year",
                sa.Integer(),
                nullable=True,
                comment="Tax year printed on the form (e.g. 2023); NULL if unread.",
            ),
            sa.Column("payer_name", sa.String(length=300), nullable=True),
            sa.Column("recipient_name", sa.String(length=300), nullable=True),
            sa.Column(
                "fields",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
                comment="Verbatim transcribed labeled box values (never computed).",
            ),
            sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'extracted'"),
                comment="'extracted' (VLM returned a result) | 'failed' (render/VLM error).",
            ),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("model", sa.String(length=128), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("extracted_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("receipt_id", name="uq_document_extraction_receipt"),
            sa.ForeignKeyConstraint(
                ["receipt_id"],
                ["receipt.id"],
                name="document_extraction_receipt_fk",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="document_extraction_entity_fk",
                ondelete="RESTRICT",
            ),
            sa.CheckConstraint(
                "status IN ('extracted','failed')",
                name="ck_document_extraction_status",
            ),
            comment=(
                "Tax-document vision-OCR result, one row per source receipt. AO owns "
                "the OCR; Tax-Planning-Ops consumes by (entity_id, tax_year)."
            ),
        )

    if not _index_exists(schema, "ix_document_extraction_workspace"):
        op.create_index(
            "ix_document_extraction_workspace", TABLE, ["workspace_id"]
        )
    # The federation read path: TPO pulls an entity's forms for a tax year.
    if not _index_exists(schema, "ix_document_extraction_entity_doctype"):
        op.create_index(
            "ix_document_extraction_entity_doctype",
            TABLE,
            ["entity_id", "doc_type", "tax_year"],
        )

    # =====================================================================
    # Grants (least privilege: SELECT/INSERT/UPDATE, NO DELETE) + RLS + guards
    # =====================================================================
    # forbid_append_only_truncate() was created in 0005; re-use it.
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{TABLE} TO {RUNTIME_ROLE}'
    )
    op.execute(f'REVOKE DELETE, TRUNCATE ON "{schema}".{TABLE} FROM {RUNTIME_ROLE}')
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_truncate ON {TABLE}")
    op.execute(
        f"CREATE TRIGGER {TABLE}_no_truncate "
        f"BEFORE TRUNCATE ON {TABLE} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {TABLE}")
    op.execute(
        f"""
        CREATE POLICY workspace_isolation ON {TABLE}
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
        raise RuntimeError("0022 requires -x tenant=<schema>")

    op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS {TABLE}_no_truncate ON {TABLE}")
    if _table_exists(schema, TABLE):
        op.drop_table(TABLE)
