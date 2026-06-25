"""per-workspace schema: work_product + engagement_policy

Revision ID: 0023_professional_substitute
Revises: 0022_document_extraction
Create Date: 2026-06-25

The professional-substitute layer. Two tenant tables:

  work_product       One finished, submission-ready tax artifact (a filled W-4P /
                     ST-3 / 1040 / CP504 response, …) for an entity. The PDF is
                     re-rendered on demand from ``inputs`` (each {value,
                     source_ref, origin}); ``status`` drives the watermark. Never
                     stores a fabricated figure — a non-blank line without a
                     source_ref is rejected at render time. Entity-scoped.

  engagement_policy  One row per workspace: the "is a professional available?"
                     switch that makes the CPA-review gate OPTIONAL. Governs
                     self-attest, local-first inference, and the dormant
                     submission rail (all transmission channels default-OFF).
                     Workspace-scoped (UNIQUE workspace_id), mirroring
                     sentinel_config.

Grants / RLS / TRUNCATE posture (mirrors 0019 / 0022 / 0005)
============================================================
  - SELECT, INSERT, UPDATE -> accounting_ops_app (NO DELETE: status transitions
    are UPDATEs; work products + policies are append-only at the row level).
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
from sqlalchemy.dialects import postgresql

revision: str = "0023_professional_substitute"
down_revision: str | None = "0022_document_extraction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
WORK_PRODUCT = "work_product"
ENGAGEMENT_POLICY = "engagement_policy"


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
        raise RuntimeError("0023 requires -x tenant=<schema>")

    # -- work_product ------------------------------------------------------
    if not _table_exists(schema, WORK_PRODUCT):
        op.create_table(
            WORK_PRODUCT,
            sa.Column(
                "id", sa.UUID(), nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "kind", sa.String(length=32), nullable=False,
                comment="e.g. 'w-4p', 'st-3', 'cp504', '1040'.",
            ),
            sa.Column("tax_year", sa.Integer(), nullable=True),
            sa.Column("title", sa.String(length=300), nullable=True),
            sa.Column(
                "status", sa.String(length=24), nullable=False,
                server_default=sa.text("'draft'"),
                comment="draft|self_approved|pro_reviewed|finalized.",
            ),
            sa.Column(
                "inputs", postgresql.JSONB(), nullable=False,
                server_default=sa.text("'{}'::jsonb"),
                comment="{field_key: {value, source_ref, origin}} — provenance-bearing.",
            ),
            sa.Column(
                "provenance", postgresql.JSONB(), nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("source_extraction_id", sa.UUID(), nullable=True),
            sa.Column("attestation_kind", sa.String(length=16), nullable=True),
            sa.Column("attested_by_uc_uid", sa.String(length=128), nullable=True),
            sa.Column("attested_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("garage_key", sa.String(length=512), nullable=True),
            sa.Column("created_by_uc_uid", sa.String(length=128), nullable=True),
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
                name="work_product_entity_fk", ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["source_extraction_id"], ["document_extraction.id"],
                name="work_product_source_extraction_fk", ondelete="SET NULL",
            ),
            sa.CheckConstraint(
                "status IN ('draft','self_approved','pro_reviewed','finalized')",
                name="ck_work_product_status",
            ),
            sa.CheckConstraint(
                "attestation_kind IS NULL OR attestation_kind IN ('self','licensed_pro')",
                name="ck_work_product_attestation_kind",
            ),
            comment=(
                "A finished, submission-ready tax work product for an entity. "
                "Re-rendered on demand from inputs; status drives the watermark."
            ),
        )

    if not _index_exists(schema, "ix_work_product_workspace"):
        op.create_index("ix_work_product_workspace", WORK_PRODUCT, ["workspace_id"])
    if not _index_exists(schema, "ix_work_product_entity_kind"):
        op.create_index(
            "ix_work_product_entity_kind",
            WORK_PRODUCT, ["entity_id", "kind", "tax_year"],
        )

    _harden(schema, WORK_PRODUCT)

    # -- engagement_policy -------------------------------------------------
    if not _table_exists(schema, ENGAGEMENT_POLICY):
        op.create_table(
            ENGAGEMENT_POLICY,
            sa.Column(
                "id", sa.UUID(), nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column(
                "review_mode", sa.String(length=32), nullable=False,
                server_default=sa.text("'pro_required'"),
                comment="pro_required|self_attest_allowed|autonomous_off.",
            ),
            sa.Column(
                "has_licensed_pro", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "prefer_local_inference", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "submission_channels", postgresql.JSONB(), nullable=False,
                server_default=sa.text("'{}'::jsonb"),
                comment="Per-channel rail enable flags; empty = all transmission OFF.",
            ),
            sa.Column(
                "rail_kill_switch", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
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
            sa.UniqueConstraint(
                "workspace_id", name="uq_engagement_policy_workspace"
            ),
            sa.CheckConstraint(
                "review_mode IN ('pro_required','self_attest_allowed','autonomous_off')",
                name="ck_engagement_policy_review_mode",
            ),
            comment=(
                "Per-workspace professional-substitute policy: review mode, "
                "local-first inference, and dormant submission-rail flags."
            ),
        )

    if not _index_exists(schema, "ix_engagement_policy_workspace"):
        op.create_index(
            "ix_engagement_policy_workspace", ENGAGEMENT_POLICY, ["workspace_id"]
        )

    _harden(schema, ENGAGEMENT_POLICY)


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0023 requires -x tenant=<schema>")

    for table in (ENGAGEMENT_POLICY, WORK_PRODUCT):
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        if _table_exists(schema, table):
            op.drop_table(table)
