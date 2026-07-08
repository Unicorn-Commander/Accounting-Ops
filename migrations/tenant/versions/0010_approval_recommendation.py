"""per-workspace schema: approval_recommendation table.

Revision ID: 0010_approval_recommendation
Revises: 0009_specialist_run
Create Date: 2026-05-29

Adds the ``approval_recommendation`` table that stores the output of the
batch pre-analyzer MCP tool (``analyze_pending_approvals``).

Design notes:
  - One row per approval_queue row: the analyzer writes one recommendation
    per approval it processes. Multiple analyzer runs are additive; each run
    may overwrite via ON CONFLICT DO UPDATE (cluster_id + confidence may change
    if the source data changes between runs, but the approval_id FK is unique
    so the latest recommendation wins).
  - APPEND-ONLY: no DELETE / TRUNCATE grants. Resolving a recommendation
    (approve or flag) leaves the row intact; only the target approval_queue
    row changes via the existing decide endpoint.
  - recommended_decision: 'approve' | 'flag_for_review' | 'reject'
  - confidence: 'high' | 'medium' | 'low'
  - submitter_kind / submitter_id: who/what ran the analyzer (service token,
    uc_uid, etc.)
  - Indexes:
    (a) per-workspace/entity surface for the recommendations tab
    (b) per-cluster surface for the cluster-card UX
    (c) by approval_id (FK path)

RLS + grant mirror 0009:
  - SELECT, INSERT, UPDATE on approval_recommendation -> accounting_ops_app
    (no DELETE — recommendations are an append-only audit record)
  - BEFORE TRUNCATE guard (forbid_append_only_truncate, 0005)
  - RLS policy workspace_isolation on workspace_id GUC

Idempotent + re-runnable (information_schema guards on every DDL change).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0010_approval_recommendation"
down_revision: str | None = "0009_specialist_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"


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
        raise RuntimeError("0010 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. Create approval_recommendation table (idempotent)
    # ------------------------------------------------------------------
    if not _table_exists(schema, "approval_recommendation"):
        op.create_table(
            "approval_recommendation",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "approval_id",
                sa.UUID(),
                nullable=False,
                comment="FK to approval_queue.id (the row being analyzed)",
            ),
            sa.Column(
                "cluster_id",
                sa.Text(),
                nullable=False,
                comment=(
                    "Cluster key grouping similar approvals, e.g. "
                    "'stripe-saas-cloud-2024-q1'. Built from "
                    "category+vendor_sig+amount_bucket+quarter."
                ),
            ),
            sa.Column(
                "recommended_decision",
                sa.Text(),
                nullable=False,
                comment="'approve' | 'flag_for_review' | 'reject'",
            ),
            sa.Column(
                "recommended_category",
                sa.Text(),
                nullable=True,
                comment="Canonical COA category the analyzer recommends",
            ),
            sa.Column(
                "confidence",
                sa.Text(),
                nullable=False,
                comment="'high' | 'medium' | 'low'",
            ),
            sa.Column(
                "rationale",
                sa.Text(),
                nullable=False,
                comment="Human-readable explanation of the recommendation",
            ),
            sa.Column(
                "submitter_kind",
                sa.Text(),
                nullable=True,
                comment="'service_token' | 'user' | 'agent'",
            ),
            sa.Column(
                "submitter_id",
                sa.Text(),
                nullable=True,
                comment="uc_uid or agent_id of the submitter",
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="approval_recommendation_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "Pre-analyzer recommendations for approval_queue rows. "
                "One row per approval_id (latest recommendation wins via "
                "ON CONFLICT). Append-only; no DELETE. "
                "Surface via GET /api/workspaces/{wid}/approval-recommendations."
            ),
        )

    # ------------------------------------------------------------------
    # 2. UNIQUE constraint on approval_id so ON CONFLICT DO UPDATE works
    # ------------------------------------------------------------------
    bind = op.get_bind()
    constraint_exists = bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_schema = :schema
                  AND table_name = 'approval_recommendation'
                  AND constraint_name = 'approval_recommendation_approval_id_uq'
                """
            ),
            {"schema": schema},
        ).first()
    )
    if not constraint_exists:
        op.execute(
            f'ALTER TABLE "{schema}".approval_recommendation '
            "ADD CONSTRAINT approval_recommendation_approval_id_uq "
            "UNIQUE (approval_id)"
        )

    # ------------------------------------------------------------------
    # 3. Indexes (idempotent)
    # ------------------------------------------------------------------
    if not _index_exists(schema, "approval_recommendation_cluster"):
        op.execute(
            f'CREATE INDEX approval_recommendation_cluster '
            f'ON "{schema}".approval_recommendation (workspace_id, entity_id, cluster_id)'
        )
    if not _index_exists(schema, "approval_recommendation_approval"):
        op.execute(
            f'CREATE INDEX approval_recommendation_approval '
            f'ON "{schema}".approval_recommendation (approval_id)'
        )
    if not _index_exists(schema, "approval_recommendation_confidence"):
        op.execute(
            f'CREATE INDEX approval_recommendation_confidence '
            f'ON "{schema}".approval_recommendation (workspace_id, confidence, created_at DESC)'
        )

    # ------------------------------------------------------------------
    # 4. Grants (SELECT, INSERT, UPDATE; no DELETE)
    # ------------------------------------------------------------------
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".approval_recommendation TO {RUNTIME_ROLE}'
    )
    op.execute(
        f'REVOKE DELETE, TRUNCATE ON "{schema}".approval_recommendation FROM {RUNTIME_ROLE}'
    )

    # ------------------------------------------------------------------
    # 5. BEFORE TRUNCATE guard
    # ------------------------------------------------------------------
    op.execute(
        "DROP TRIGGER IF EXISTS approval_recommendation_no_truncate ON approval_recommendation"
    )
    op.execute(
        "CREATE TRIGGER approval_recommendation_no_truncate "
        "BEFORE TRUNCATE ON approval_recommendation "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    # ------------------------------------------------------------------
    # 6. Row-Level Security on workspace_id GUC
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE approval_recommendation ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON approval_recommendation")
    op.execute(
        """
        CREATE POLICY workspace_isolation ON approval_recommendation
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
        raise RuntimeError("0010 requires -x tenant=<schema>")

    op.execute("DROP POLICY IF EXISTS workspace_isolation ON approval_recommendation")
    op.execute(
        "DROP TRIGGER IF EXISTS approval_recommendation_no_truncate ON approval_recommendation"
    )
    op.execute(
        f'DROP INDEX IF EXISTS "{schema}".approval_recommendation_confidence'
    )
    op.execute(
        f'DROP INDEX IF EXISTS "{schema}".approval_recommendation_approval'
    )
    op.execute(
        f'DROP INDEX IF EXISTS "{schema}".approval_recommendation_cluster'
    )

    if _table_exists(schema, "approval_recommendation"):
        op.drop_table("approval_recommendation")
