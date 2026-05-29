"""per-workspace schema: tax_review_queue for 1120 preparer specialist

Revision ID: 0008_tax_review_queue
Revises: 0007_closed_period
Create Date: 2026-05-29

Adds the ``tax_review_queue`` table used by the 1120 Preparer specialist agent
to surface form-line proposals, flagged positions, memos, and questions for CPA
review. The specialist calls the draft_memo and flag_for_cpa_review MCP tools to
insert rows; the CPA reviewer reads + resolves them through the HTTP API.

Schema design notes:
  - item_type discriminates four row shapes: form_line_proposal, flagged_position,
    memo, question. Each is flexible via the JSONB content column.
  - form_line_proposal content shape:
      {"line": "26", "label": "Other deductions",
       "proposed_amount_cents": 123456,
       "source_categories": [{"category": "SaaS · AI", "amount_cents": 98765}],
       "notes": "Proposed by specialist; verify COGS vs. deduction split."}
  - flagged_position content shape:
      {"issue": "§163(j) limitation may apply to interest expense.",
       "affected_line": "18", "context": "...", "recommendation": "..."}
  - memo / question content shape:
      {"text": "Confirm whether the $600 contractor threshold applies here."}
  - citations JSONB shape (optional array):
      [{"type": "section", "value": "§163(j)"},
       {"type": "journal_entry", "value": "<uuid>"}]

Grants / RLS / TRUNCATE guard mirror 0007:
  - SELECT, INSERT, UPDATE on tax_review_queue -> accounting_ops_app
    (no DELETE — review records are append-audit; status transitions only)
  - BEFORE TRUNCATE guard (forbid_append_only_truncate, created in 0005)
  - RLS policy workspace_isolation on workspace_id GUC

Idempotent + re-runnable (information_schema guards on every DDL change).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0008_tax_review_queue"
down_revision: str | None = "0007_closed_period"
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


def _trigger_exists(schema: str, table: str, trigger: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.triggers
                WHERE trigger_schema = :schema
                  AND event_object_table = :table
                  AND trigger_name = :trigger
                """
            ),
            {"schema": schema, "table": table, "trigger": trigger},
        ).first()
    )


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0008 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. Create tax_review_queue table (idempotent)
    # ------------------------------------------------------------------
    if not _table_exists(schema, "tax_review_queue"):
        op.create_table(
            "tax_review_queue",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column("tax_year", sa.Integer(), nullable=False),
            sa.Column("item_type", sa.Text(), nullable=False,
                      comment=(
                          "Discriminator: 'form_line_proposal' | 'flagged_position' "
                          "| 'memo' | 'question'"
                      )),
            sa.Column("form_reference", sa.Text(), nullable=True,
                      comment="e.g. '1120-line-26', '1125-A-line-4', NULL for memos"),
            sa.Column(
                "content",
                postgresql.JSONB(),
                nullable=False,
                comment=(
                    "Flexible payload. form_line_proposal: {line, label, "
                    "proposed_amount_cents, source_categories, notes}. "
                    "flagged_position: {issue, affected_line, context, recommendation}. "
                    "memo/question: {text}."
                ),
            ),
            sa.Column("confidence", sa.Text(), nullable=False,
                      comment="'low' | 'medium' | 'high'"),
            sa.Column(
                "citations",
                postgresql.JSONB(),
                nullable=True,
                comment=(
                    '[{"type":"section","value":"§163(j)"}, '
                    '{"type":"journal_entry","value":"<uuid>"}]'
                ),
            ),
            sa.Column("status", sa.Text(), nullable=False,
                      server_default=sa.text("'pending'"),
                      comment="'pending' | 'reviewed' | 'resolved' | 'rejected'"),
            sa.Column("submitter_kind", sa.Text(), nullable=False,
                      comment="'agent' | 'human'"),
            sa.Column("submitter_id", sa.Text(), nullable=False,
                      comment="agent_id like 'tax-1120-preparer' or a uc_uid"),
            sa.Column("reviewer_uc_uid", sa.Text(), nullable=True),
            sa.Column("resolution_notes", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"],
                name="tax_review_queue_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "Holds form-line proposals, flagged positions, memos, and questions "
                "generated by the 1120 Preparer specialist for CPA review. "
                "Status transitions (pending → reviewed/resolved/rejected) are the "
                "only mutations; rows are never deleted (audit trail)."
            ),
        )

    # ------------------------------------------------------------------
    # 2. Indexes (idempotent via IF NOT EXISTS)
    #    (a) per-year review surface: entity + year + status
    #    (b) cross-entity list:        workspace + status + created_at DESC
    # ------------------------------------------------------------------
    if not _index_exists(schema, "tax_review_queue_entity_year_status_idx"):
        op.execute(
            f'CREATE INDEX tax_review_queue_entity_year_status_idx '
            f'ON "{schema}".tax_review_queue (entity_id, tax_year, status)'
        )
    if not _index_exists(schema, "tax_review_queue_workspace_status_created_idx"):
        op.execute(
            f'CREATE INDEX tax_review_queue_workspace_status_created_idx '
            f'ON "{schema}".tax_review_queue (workspace_id, status, created_at DESC)'
        )

    # ------------------------------------------------------------------
    # 3. Least-privilege grants for the runtime role
    #    SELECT, INSERT, UPDATE (no DELETE — review trail must be append-only)
    # ------------------------------------------------------------------
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".tax_review_queue TO {RUNTIME_ROLE}'
    )
    # Belt-and-suspenders: strip DELETE/TRUNCATE if they ever leaked in.
    op.execute(
        f'REVOKE DELETE, TRUNCATE ON "{schema}".tax_review_queue FROM {RUNTIME_ROLE}'
    )

    # ------------------------------------------------------------------
    # 4. BEFORE TRUNCATE guard
    #    forbid_append_only_truncate() was created in 0005; re-use it.
    # ------------------------------------------------------------------
    op.execute(
        "DROP TRIGGER IF EXISTS tax_review_queue_no_truncate ON tax_review_queue"
    )
    op.execute(
        "CREATE TRIGGER tax_review_queue_no_truncate "
        "BEFORE TRUNCATE ON tax_review_queue "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    # ------------------------------------------------------------------
    # 5. Row-Level Security on workspace_id GUC
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE tax_review_queue ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON tax_review_queue")
    op.execute(
        """
        CREATE POLICY workspace_isolation ON tax_review_queue
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
        raise RuntimeError("0008 requires -x tenant=<schema>")

    op.execute("DROP POLICY IF EXISTS workspace_isolation ON tax_review_queue")
    op.execute("DROP TRIGGER IF EXISTS tax_review_queue_no_truncate ON tax_review_queue")
    op.execute(
        f'DROP INDEX IF EXISTS "{schema}".tax_review_queue_workspace_status_created_idx'
    )
    op.execute(
        f'DROP INDEX IF EXISTS "{schema}".tax_review_queue_entity_year_status_idx'
    )

    if _table_exists(schema, "tax_review_queue"):
        op.drop_table("tax_review_queue")
