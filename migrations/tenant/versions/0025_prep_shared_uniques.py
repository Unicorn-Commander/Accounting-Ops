"""shared-schema consolidation, part 1: composite uniques + current_workspace_id()

Revision ID: 0025_prep_shared_uniques
Revises: 0024_signature_request
Create Date: 2026-07-07

Part 1 of 2 for the schema-per-workspace -> shared-table RLS+GUC consolidation
(see docs/TENANCY-CONSOLIDATION-RUNBOOK.md). This runs on the single shared
tenant schema (`books`).

WHY TWO MIGRATIONS: at cutover the data-copy step (scripts/consolidate_workspaces.py)
runs BETWEEN the two migrations —
    alembic -x tenant=books upgrade 0025_prep_shared_uniques   # this file
    python -m scripts.consolidate_workspaces                    # copy other tenants' rows in
    alembic -x tenant=books upgrade head                        # -> 0026 (FORCE RLS)
The composite-unique conversions here MUST land BEFORE any foreign rows are
copied (so tenants' now-merged rows don't collide on a global unique), and the
FORCE flip in 0026 MUST land AFTER the copy (so the owner-run copy isn't gated).
On a fresh bootstrap (empty schema) both run back-to-back harmlessly.

The 4 conversions are the ONLY unique constraints/indexes that dropped
workspace_id and would collide when multiple workspaces share one table. Every
other unique is entity_id-scoped (entity.id is a global UUID), already
workspace_id-scoped, or a global-UUID key. Confirmed by a full audit of
app/models/ledger.py + all tenant migrations, and by a live pre-check that
found zero pre-existing intra-workspace duplicates on these columns.

Idempotent + re-runnable (drop-if-exists then add), like the other tenant
migrations. Runs as the OWNER role with search_path pinned to the target schema
by migrations/tenant/env.py.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0025_prep_shared_uniques"
down_revision: str | None = "0024_signature_request"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. current_workspace_id(): the STABLE helper the FORCE RLS policies
    #    (0026) key on. In `public` so it resolves regardless of search_path.
    #    Returns NULL when the GUC is unset/empty -> workspace_id = NULL is
    #    never true -> default-deny.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.current_workspace_id() RETURNS uuid
          LANGUAGE sql STABLE
          AS $$ SELECT NULLIF(current_setting('app.current_workspace', true), '')::uuid $$
        """
    )

    # ------------------------------------------------------------------
    # 2. Convert the 4 collision-prone uniques to composite (workspace_id, ...).
    #    search_path is pinned to the target schema, so unqualified names
    #    resolve there.
    # ------------------------------------------------------------------
    # entity.slug  (constraint entity_slug_key)
    op.execute("ALTER TABLE entity DROP CONSTRAINT IF EXISTS entity_slug_key")
    op.execute("ALTER TABLE entity DROP CONSTRAINT IF EXISTS uq_entity_workspace_slug")
    op.execute(
        "ALTER TABLE entity ADD CONSTRAINT uq_entity_workspace_slug "
        "UNIQUE (workspace_id, slug)"
    )

    # event_log.event_hash  (constraint event_log_event_hash_key)
    op.execute("ALTER TABLE event_log DROP CONSTRAINT IF EXISTS event_log_event_hash_key")
    op.execute(
        "ALTER TABLE event_log DROP CONSTRAINT IF EXISTS uq_event_log_workspace_event_hash"
    )
    op.execute(
        "ALTER TABLE event_log ADD CONSTRAINT uq_event_log_workspace_event_hash "
        "UNIQUE (workspace_id, event_hash)"
    )

    # receipt.content_hash  (UNIQUE INDEX ix_receipt_content_hash — an index, not a constraint)
    op.execute("DROP INDEX IF EXISTS ix_receipt_content_hash")
    op.execute("DROP INDEX IF EXISTS ix_receipt_workspace_content_hash")
    op.execute(
        "CREATE UNIQUE INDEX ix_receipt_workspace_content_hash "
        "ON receipt (workspace_id, content_hash)"
    )

    # account_entity_mapping (source, external_account_id)
    #   (constraint uq_account_entity_mapping_source_account)
    op.execute(
        "ALTER TABLE account_entity_mapping "
        "DROP CONSTRAINT IF EXISTS uq_account_entity_mapping_source_account"
    )
    op.execute(
        "ALTER TABLE account_entity_mapping "
        "DROP CONSTRAINT IF EXISTS uq_account_entity_mapping_ws_source_account"
    )
    op.execute(
        "ALTER TABLE account_entity_mapping "
        "ADD CONSTRAINT uq_account_entity_mapping_ws_source_account "
        "UNIQUE (workspace_id, source, external_account_id)"
    )


def downgrade() -> None:
    # Revert to the pre-consolidation global uniques. Best-effort; only safe
    # while a single workspace's rows are present (the reason we consolidated).
    op.execute(
        "ALTER TABLE account_entity_mapping "
        "DROP CONSTRAINT IF EXISTS uq_account_entity_mapping_ws_source_account"
    )
    op.execute(
        "ALTER TABLE account_entity_mapping "
        "ADD CONSTRAINT uq_account_entity_mapping_source_account "
        "UNIQUE (source, external_account_id)"
    )

    op.execute("DROP INDEX IF EXISTS ix_receipt_workspace_content_hash")
    op.execute(
        "CREATE UNIQUE INDEX ix_receipt_content_hash ON receipt (content_hash)"
    )

    op.execute("ALTER TABLE event_log DROP CONSTRAINT IF EXISTS uq_event_log_workspace_event_hash")
    op.execute(
        "ALTER TABLE event_log ADD CONSTRAINT event_log_event_hash_key UNIQUE (event_hash)"
    )

    op.execute("ALTER TABLE entity DROP CONSTRAINT IF EXISTS uq_entity_workspace_slug")
    op.execute("ALTER TABLE entity ADD CONSTRAINT entity_slug_key UNIQUE (slug)")

    op.execute("DROP FUNCTION IF EXISTS public.current_workspace_id()")
