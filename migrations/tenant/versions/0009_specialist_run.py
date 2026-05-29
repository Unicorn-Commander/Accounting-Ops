"""per-workspace schema: specialist_run table for MLOps observability.

Revision ID: 0009_specialist_run
Revises: 0008_tax_review_queue
Create Date: 2026-05-29

Adds the ``specialist_run`` table that records every invocation of a tax
specialist (1120 Preparer, 1099, SC State, Coordinator, etc.) for MLOps
observability. Once specialists run against many entity-years, customers and
CPAs need to see: which runs happened, what they produced, did any fail, how
long they took, and how much they cost (cloud LLM token counts).

Schema design notes:
  - specialist_id is a free-text slug: 'tax-1120-preparer', 'tax-1099-specialist',
    'tax-sc-state', 'tax-coordinator'. Extensible without schema changes.
  - backend + model record the inference provider + model ID at run time (matches
    what the Anthropic/OpenAI SDK reported, not what the caller intended).
  - input_summary / output_summary / metrics are JSONB — flexible enough to
    capture the current specialist shapes without a columnar schema migration
    every time a specialist adds a new output field.
  - endpoint_url is stored for audit but redacted in API responses (may contain
    internal hostnames).
  - status lifecycle: running → completed | failed | cancelled.
  - No DELETE on accounting_ops_app (append-only audit trail).

Grants / RLS / TRUNCATE guard mirror 0008:
  - SELECT, INSERT, UPDATE on specialist_run -> accounting_ops_app
    (no DELETE — run records are an audit trail)
  - BEFORE TRUNCATE guard (forbid_append_only_truncate, created in 0005)
  - RLS policy workspace_isolation on workspace_id GUC

Idempotent + re-runnable (information_schema guards on every DDL change).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0009_specialist_run"
down_revision: str | None = "0008_tax_review_queue"
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
        raise RuntimeError("0009 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. Create specialist_run table (idempotent)
    # ------------------------------------------------------------------
    if not _table_exists(schema, "specialist_run"):
        op.create_table(
            "specialist_run",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column("tax_year", sa.Integer(), nullable=False),
            sa.Column(
                "specialist_id",
                sa.Text(),
                nullable=False,
                comment=(
                    "Specialist slug: 'tax-1120-preparer' | 'tax-1099-specialist' "
                    "| 'tax-sc-state' | 'tax-coordinator' (extensible)"
                ),
            ),
            sa.Column(
                "agent_token_id",
                sa.Text(),
                nullable=True,
                comment="Service-token agent_id from the JWT (audit trail)",
            ),
            sa.Column(
                "invoker_uc_uid",
                sa.Text(),
                nullable=False,
                comment="uc_uid of the human or service that triggered the run",
            ),
            sa.Column(
                "backend",
                sa.Text(),
                nullable=False,
                comment="'anthropic' | 'openai' | 'local'",
            ),
            sa.Column(
                "model",
                sa.Text(),
                nullable=False,
                comment="Model ID as reported by the SDK, e.g. 'claude-sonnet-4-6'",
            ),
            sa.Column(
                "endpoint_url",
                sa.Text(),
                nullable=True,
                comment="MCP/API endpoint URL; redacted in API responses",
            ),
            sa.Column(
                "started_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "finished_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "duration_ms",
                sa.BigInteger(),
                nullable=True,
                comment="Wall-clock ms from runner start to finish",
            ),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'running'"),
                comment="'running' | 'completed' | 'failed' | 'cancelled'",
            ),
            sa.Column(
                "input_summary",
                postgresql.JSONB(),
                nullable=True,
                comment=(
                    "Sanitised run inputs: {workspace_id, entity_id, tax_year, args:[…]}. "
                    "No secrets or PII."
                ),
            ),
            sa.Column(
                "output_summary",
                postgresql.JSONB(),
                nullable=True,
                comment=(
                    "Run outputs: {output_file, output_bytes, queue_rows_created, …}"
                ),
            ),
            sa.Column(
                "metrics",
                postgresql.JSONB(),
                nullable=True,
                comment=(
                    "Token + tool metrics: "
                    "{input_tokens, output_tokens, total_tool_calls, "
                    "mcp_tool_breakdown:{tool_name: count, …}}"
                ),
            ),
            sa.Column(
                "error_summary",
                sa.Text(),
                nullable=True,
                comment="Exception type + message if status='failed'",
            ),
            sa.Column(
                "trace_id",
                sa.Text(),
                nullable=True,
                comment="OTEL trace ID for correlation with spans",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"],
                name="specialist_run_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "MLOps observability record for every specialist run. "
                "Captures who ran what (specialist_id + model + backend), "
                "when (started_at, finished_at, duration_ms), and how it went "
                "(status, metrics, error_summary). Append-only; no DELETE. "
                "Surface via GET /api/workspaces/{wid}/specialist-runs."
            ),
        )

    # ------------------------------------------------------------------
    # 2. Indexes (idempotent via explicit check)
    #    (a) per-entity/year surface ordered by most-recent-first
    #    (b) cross-entity status sweep for the workspace dashboard
    # ------------------------------------------------------------------
    if not _index_exists(schema, "specialist_run_lookup"):
        op.execute(
            f'CREATE INDEX specialist_run_lookup '
            f'ON "{schema}".specialist_run (entity_id, tax_year, started_at DESC)'
        )
    if not _index_exists(schema, "specialist_run_status"):
        op.execute(
            f'CREATE INDEX specialist_run_status '
            f'ON "{schema}".specialist_run (workspace_id, status, started_at DESC)'
        )

    # ------------------------------------------------------------------
    # 3. Least-privilege grants for the runtime role
    #    SELECT, INSERT, UPDATE (no DELETE — run records are audit trail)
    # ------------------------------------------------------------------
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".specialist_run TO {RUNTIME_ROLE}'
    )
    # Belt-and-suspenders: strip DELETE/TRUNCATE if they ever leaked in.
    op.execute(
        f'REVOKE DELETE, TRUNCATE ON "{schema}".specialist_run FROM {RUNTIME_ROLE}'
    )

    # ------------------------------------------------------------------
    # 4. BEFORE TRUNCATE guard
    #    forbid_append_only_truncate() was created in 0005; re-use it.
    # ------------------------------------------------------------------
    op.execute(
        "DROP TRIGGER IF EXISTS specialist_run_no_truncate ON specialist_run"
    )
    op.execute(
        "CREATE TRIGGER specialist_run_no_truncate "
        "BEFORE TRUNCATE ON specialist_run "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    # ------------------------------------------------------------------
    # 5. Row-Level Security on workspace_id GUC
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE specialist_run ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON specialist_run")
    op.execute(
        """
        CREATE POLICY workspace_isolation ON specialist_run
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
        raise RuntimeError("0009 requires -x tenant=<schema>")

    op.execute("DROP POLICY IF EXISTS workspace_isolation ON specialist_run")
    op.execute("DROP TRIGGER IF EXISTS specialist_run_no_truncate ON specialist_run")
    op.execute(
        f'DROP INDEX IF EXISTS "{schema}".specialist_run_status'
    )
    op.execute(
        f'DROP INDEX IF EXISTS "{schema}".specialist_run_lookup'
    )

    if _table_exists(schema, "specialist_run"):
        op.drop_table("specialist_run")
