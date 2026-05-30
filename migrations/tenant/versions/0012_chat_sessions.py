"""per-workspace schema: extend agent_session with chat fields + add chat_message.

Revision ID: 0012_chat_sessions
Revises: 0011_entity_corp_facts
Create Date: 2026-05-30

Adds the columns and indexes that the in-app /chat surface needs:

  agent_session
    + title              text     (user-facing session title, derived from first user message)
    + agent_persona      text     (slug of the persona — e.g. 'cpa-tax-resolver')
    + tax_year           int      (optional context bound at session create)
    + last_activity_at   timestamptz (touched on every message; defaults to started_at)
    + cancelled_at       timestamptz (set when the user cancels an in-flight stream)

  chat_message  (new)
    one row per turn (user OR assistant) with full content + tool-call metadata
    in JSONB. Append-only with the same RLS / TRUNCATE guard as the other
    auditable tables.

The base ``agent_session`` table already exists from 0001_tenant_initial. This
migration only ADDS columns / tables so it is safe on an existing tenant.

Grants / RLS / TRUNCATE guard mirror 0010:
  - SELECT, INSERT, UPDATE on agent_session (no new grant needed — already RW)
  - SELECT, INSERT, UPDATE on chat_message -> accounting_ops_app
  - No DELETE on chat_message (full conversation audit trail)
  - BEFORE TRUNCATE guard
  - RLS policy workspace_isolation

Idempotent + re-runnable (information_schema guards on every DDL change).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0012_chat_sessions"
down_revision: str | None = "0011_entity_corp_facts"
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


def _column_exists(schema: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = :schema
                  AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"schema": schema, "table": table, "column": column},
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
        raise RuntimeError("0011 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. Extend agent_session with chat-friendly columns
    # ------------------------------------------------------------------
    if not _column_exists(schema, "agent_session", "title"):
        op.execute(
            f'ALTER TABLE "{schema}".agent_session '
            "ADD COLUMN title text NULL"
        )
    if not _column_exists(schema, "agent_session", "agent_persona"):
        op.execute(
            f'ALTER TABLE "{schema}".agent_session '
            "ADD COLUMN agent_persona text NULL"
        )
    if not _column_exists(schema, "agent_session", "tax_year"):
        op.execute(
            f'ALTER TABLE "{schema}".agent_session '
            "ADD COLUMN tax_year integer NULL"
        )
    if not _column_exists(schema, "agent_session", "last_activity_at"):
        op.execute(
            f'ALTER TABLE "{schema}".agent_session '
            "ADD COLUMN last_activity_at timestamptz NULL"
        )
        # Backfill so existing rows have a sensible value to sort on.
        op.execute(
            f'UPDATE "{schema}".agent_session '
            "SET last_activity_at = started_at WHERE last_activity_at IS NULL"
        )
    if not _column_exists(schema, "agent_session", "cancelled_at"):
        op.execute(
            f'ALTER TABLE "{schema}".agent_session '
            "ADD COLUMN cancelled_at timestamptz NULL"
        )

    # Indexes for the sidebar (sort by recency per user / entity).
    if not _index_exists(schema, "ix_agent_session_user_activity"):
        op.execute(
            f'CREATE INDEX ix_agent_session_user_activity '
            f'ON "{schema}".agent_session (user_uc_uid, last_activity_at DESC NULLS LAST)'
        )
    if not _index_exists(schema, "ix_agent_session_entity_activity"):
        op.execute(
            f'CREATE INDEX ix_agent_session_entity_activity '
            f'ON "{schema}".agent_session (entity_id, last_activity_at DESC NULLS LAST)'
        )

    # ------------------------------------------------------------------
    # 2. chat_message table (one row per turn)
    # ------------------------------------------------------------------
    if not _table_exists(schema, "chat_message"):
        op.create_table(
            "chat_message",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "session_id",
                sa.UUID(),
                nullable=False,
                comment="FK to agent_session.id (the chat conversation)",
            ),
            sa.Column(
                "role",
                sa.Text(),
                nullable=False,
                comment="'user' | 'assistant' | 'system' | 'tool'",
            ),
            sa.Column(
                "content",
                postgresql.JSONB(),
                nullable=False,
                comment=(
                    "Rich message payload. Shape: "
                    "{type: 'text'|'tool_call'|'tool_result'|'mixed', "
                    " text?: str, "
                    " tool_calls?: [{id, name, args}], "
                    " tool_results?: [{id, name, result, artifact_kind?}], "
                    " stop_reason?: str}"
                ),
            ),
            sa.Column(
                "submitter_uc_uid",
                sa.String(length=128),
                nullable=True,
                comment="uc_uid of the user (NULL for assistant/tool messages)",
            ),
            sa.Column(
                "model",
                sa.Text(),
                nullable=True,
                comment="Model ID for assistant turns (e.g. 'anthropic/claude-sonnet-4.5')",
            ),
            sa.Column(
                "backend",
                sa.Text(),
                nullable=True,
                comment="'anthropic' | 'openrouter' | 'openai' | 'local'",
            ),
            sa.Column(
                "input_tokens",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "output_tokens",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "duration_ms",
                sa.Integer(),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["agent_session.id"],
                name="chat_message_session_fk",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="chat_message_entity_fk",
                ondelete="RESTRICT",
            ),
            comment=(
                "One row per chat turn (user prompt, assistant response, tool call/result). "
                "Append-only audit trail of the in-app /chat conversation."
            ),
        )

    if not _index_exists(schema, "ix_chat_message_session_created"):
        op.execute(
            f'CREATE INDEX ix_chat_message_session_created '
            f'ON "{schema}".chat_message (session_id, created_at ASC)'
        )

    # ------------------------------------------------------------------
    # 3. Grants (SELECT, INSERT, UPDATE; no DELETE)
    # ------------------------------------------------------------------
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".chat_message TO {RUNTIME_ROLE}'
    )
    op.execute(
        f'REVOKE DELETE, TRUNCATE ON "{schema}".chat_message FROM {RUNTIME_ROLE}'
    )

    # ------------------------------------------------------------------
    # 4. BEFORE TRUNCATE guard
    # ------------------------------------------------------------------
    op.execute(
        "DROP TRIGGER IF EXISTS chat_message_no_truncate ON chat_message"
    )
    op.execute(
        "CREATE TRIGGER chat_message_no_truncate "
        "BEFORE TRUNCATE ON chat_message "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    # ------------------------------------------------------------------
    # 5. Row-Level Security on workspace_id GUC
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE chat_message ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON chat_message")
    op.execute(
        """
        CREATE POLICY workspace_isolation ON chat_message
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
        raise RuntimeError("0011 requires -x tenant=<schema>")

    op.execute("DROP POLICY IF EXISTS workspace_isolation ON chat_message")
    op.execute("DROP TRIGGER IF EXISTS chat_message_no_truncate ON chat_message")
    op.execute(f'DROP INDEX IF EXISTS "{schema}".ix_chat_message_session_created')

    if _table_exists(schema, "chat_message"):
        op.drop_table("chat_message")

    op.execute(f'DROP INDEX IF EXISTS "{schema}".ix_agent_session_user_activity')
    op.execute(f'DROP INDEX IF EXISTS "{schema}".ix_agent_session_entity_activity')

    for col in ("cancelled_at", "last_activity_at", "tax_year", "agent_persona", "title"):
        if _column_exists(schema, "agent_session", col):
            op.execute(f'ALTER TABLE "{schema}".agent_session DROP COLUMN {col}')
