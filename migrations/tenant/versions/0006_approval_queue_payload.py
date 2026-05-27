"""per-workspace schema: structured pending-action storage on approval_queue

Revision ID: 0006_approval_queue_payload
Revises: 0005_rls_and_runtime_grants
Create Date: 2026-05-27

Closes the design gap that blocked "apply on approve": when post_correction or
add_specialty_rule queued a material action, the only record of WHAT to do was
the human-readable approval_queue.reasoning string. subject_id pointed at the
*original* journal entry (a recat) or the proposed compliance_rule, and the
structured intent (e.g. the target new_category) lived only in prose. The
approve path therefore had nothing clean + machine-readable to act on.

This adds a nullable JSONB ``payload`` column that holds the structured action
the approval finalizes:

  journal_recategorization:
    {"action": "recategorize", "original_entry_id": "<uuid>",
     "from_category": "...", "new_category": "...", "agent_reasoning": "..."}

  compliance_rule:
    {"action": "activate_rule", "rule_id": "<uuid>", "kind": "...",
     "trigger": "...", "action_enum": "...", "pending_profile_id": "<uuid>"}

Backward-compatible by construction:
  - NULLABLE, no server default -> existing 'pending' rows are unaffected and
    re-running this migration on a populated table never rewrites data.
  - approval_queue is already SELECT+INSERT+UPDATE for the runtime role (it's in
    0005's RW_NO_DELETE_TABLES) and RLS-enabled; a new column inherits the
    table's existing grants + policy, so no new grant is required. The decide
    endpoint reads/writes ``payload`` under the same workspace GUC + entity
    filter every other table query uses.

Idempotent + re-runnable (applied to NEW schemas at provision time and to
EXISTING schemas via upgrade_all_workspaces): the add/drop are guarded with
information_schema checks so a second run is a no-op.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0006_approval_queue_payload"
down_revision: str | None = "0005_rls_and_runtime_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _set_search_path() -> str:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
    return schema or ""


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


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0006 requires -x tenant=<schema>")

    if not _column_exists(schema, "approval_queue", "payload"):
        op.add_column(
            "approval_queue",
            sa.Column(
                "payload",
                postgresql.JSONB(),
                nullable=True,
                comment=(
                    "Structured pending action the approval finalizes. Shape "
                    "keyed by subject_type (recategorize / activate_rule). NULL "
                    "for legacy rows queued before 0006."
                ),
            ),
        )


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0006 requires -x tenant=<schema>")
    if _column_exists(schema, "approval_queue", "payload"):
        op.drop_column("approval_queue", "payload")
