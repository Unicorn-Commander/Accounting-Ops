"""per-workspace schema: Sentinel finding de-duplication (dedupe_key + open-unique index)

Revision ID: 0020_sentinel_finding_dedupe
Revises: 0019_ar_invoicing
Create Date: 2026-06-02

Why
===
``run_sentinel.py`` documents that ``execute_action()`` "de-dupes within a UTC
day on topic key" — but ``_write_finding`` only ever did a plain INSERT with a
fresh uuid. So every 6-hour cycle that re-detected the same condition wrote a
BRAND-NEW ``sentinel_finding`` row. Over weeks that floods ``/open-items``, the
activity feed, the CFO brief, and the CFO's own context window with identical
findings. (A live brief showed one §174 finding three times.) The CFO-brief
headline-dedup (e5b245e) only hid the symptom; this fixes the source.

What this migration does (idempotent + re-runnable; safe on an empty new-schema
table at provision time AND on existing schemas with historical dupes)
======================================================================
1. Add ``dedupe_key TEXT`` (the incident identity), ``last_seen_at`` and
   ``seen_count`` (so a recurring finding shows "seen N times, last X" instead
   of N rows).
2. Backfill ``dedupe_key`` to MATCH the runtime key the app now computes:
     - single-finding-per-entity checks -> ``{check_type}:{entity_id}``
       (so a volatile-title finding like "Cash runway 2.3 months" /
       "12 uncategorized" updates ONE row in place as the number drifts,
       instead of spawning a new row every cycle).
     - the looping checks that emit one finding PER SUBJECT keep a per-subject
       key reconstructed from ``details`` (approval id / connector / filing
       form+due / regulatory feed id), so two genuinely-different subjects are
       NEVER merged.
3. Collapse pre-existing OPEN duplicates: keep the newest row per
   (workspace_id, dedupe_key); mark the rest ``status='dismissed'`` with
   ``details.auto_deduped=true`` + ``superseded_by`` (AUDITABLE — never
   row-deleted), and roll their count onto the keeper's ``seen_count``.
4. Create a PARTIAL UNIQUE INDEX on (workspace_id, dedupe_key) WHERE
   status='open'. This is the DB-enforced backstop (mirrors the 0018 hash-chain
   constraint + the AR (workspace_id, external_source, external_ref) idempotency
   index) so even a race can't reopen a duplicate.

The runtime side (agents/sentinel/actions.py) checks for an existing OPEN
finding before acting (refresh + skip the side effect, so no duplicate
memo/flag either) and writes with ``ON CONFLICT (workspace_id, dedupe_key)
WHERE status='open' DO UPDATE``.

Note on the historical/runtime key match: the only check whose backfilled key
isn't byte-identical to the forward key is ``tax_credit_scan`` (forward key
slugs the program name + appends the quarter; the backfill keys it by title,
which is per-program and stable). That can leave at most one stale open
tax-credit nudge per program until it's resolved — a quarterly, low-volume
finding — which is acceptable and self-heals.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0020_sentinel_finding_dedupe"
down_revision: str | None = "0019_ar_invoicing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Looping checks that emit one finding PER SUBJECT (not one per entity); their
# historical rows must be keyed per-subject so distinct subjects don't merge.
# Everything NOT in here is one-finding-per-entity -> keyed {check_type}:{entity}.


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
                WHERE table_schema = :schema AND table_name = :table
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


# Backfill key expression — kept in ONE place so upgrade() reuses it. Mirrors
# the runtime key in agents/sentinel/actions.py:_finding_dedupe_key. Truncated
# to 200 chars to match the runtime cap.
_BACKFILL_KEY_SQL = """
left(CASE
  WHEN check_type = 'approval_aging'     AND details ? 'approval_id'
       THEN 'sentinel-approval-aging-' || (details->>'approval_id')
  WHEN check_type = 'connector_health'   AND details ? 'connector_id'
       THEN 'sentinel-connector-' || coalesce(details->>'connector_name','')
            || '-' || left(details->>'connector_id', 8)
  WHEN check_type = 'filing_deadlines'   AND details ? 'form'
       THEN 'sentinel-deadline-' || (details->>'form')
            || '-' || coalesce(details->>'due','')
  WHEN check_type = 'regulatory_updates' AND details ? 'feed_external_id'
       THEN 'sentinel-reg-' || coalesce(details->>'feed_source','')
            || '-' || left(details->>'feed_external_id', 60)
  WHEN check_type = 'tax_credit_scan'
       THEN 'tax_credit_scan:' || coalesce(entity_id::text, '-')
            || ':' || left(title, 160)
  ELSE check_type || ':' || coalesce(entity_id::text, '-')
END, 200)
"""


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0020 requires -x tenant=<schema>")

    if not _table_exists(schema, "sentinel_finding"):
        # Sentinel tables (0014) not present in this schema yet — nothing to do.
        return

    # 1. Columns -----------------------------------------------------------
    if not _column_exists(schema, "sentinel_finding", "dedupe_key"):
        op.execute('ALTER TABLE sentinel_finding ADD COLUMN dedupe_key TEXT')
    if not _column_exists(schema, "sentinel_finding", "last_seen_at"):
        op.execute(
            'ALTER TABLE sentinel_finding '
            'ADD COLUMN last_seen_at TIMESTAMPTZ'
        )
    if not _column_exists(schema, "sentinel_finding", "seen_count"):
        op.execute(
            'ALTER TABLE sentinel_finding '
            'ADD COLUMN seen_count INTEGER NOT NULL DEFAULT 1'
        )

    # 2. Backfill dedupe_key + last_seen_at -------------------------------
    op.execute(
        f"UPDATE sentinel_finding SET dedupe_key = {_BACKFILL_KEY_SQL} "
        f"WHERE dedupe_key IS NULL"
    )
    op.execute(
        "UPDATE sentinel_finding SET last_seen_at = created_at "
        "WHERE last_seen_at IS NULL"
    )

    # 3a. Roll the collapsed-dup count + latest timestamp onto the keeper
    #     (the newest open row per (workspace_id, dedupe_key)) BEFORE we
    #     dismiss the losers, so the count isn't lost.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY workspace_id, dedupe_key
                       ORDER BY created_at DESC, id DESC
                   ) AS rn,
                   count(*)   OVER (PARTITION BY workspace_id, dedupe_key) AS grp_n,
                   max(created_at) OVER (PARTITION BY workspace_id, dedupe_key) AS grp_last
            FROM sentinel_finding
            WHERE status = 'open'
        )
        UPDATE sentinel_finding f
        SET seen_count   = GREATEST(f.seen_count, r.grp_n),
            last_seen_at = GREATEST(COALESCE(f.last_seen_at, f.created_at), r.grp_last)
        FROM ranked r
        WHERE f.id = r.id AND r.rn = 1 AND r.grp_n > 1
        """
    )

    # 3b. Dismiss the duplicate OPEN losers (keep newest), auditable.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, workspace_id, dedupe_key, created_at,
                   row_number() OVER (
                       PARTITION BY workspace_id, dedupe_key
                       ORDER BY created_at DESC, id DESC
                   ) AS rn,
                   first_value(id) OVER (
                       PARTITION BY workspace_id, dedupe_key
                       ORDER BY created_at DESC, id DESC
                   ) AS keeper_id
            FROM sentinel_finding
            WHERE status = 'open'
        )
        UPDATE sentinel_finding f
        SET status  = 'dismissed',
            details = COALESCE(f.details, '{}'::jsonb)
                      || jsonb_build_object(
                           'auto_deduped', true,
                           'superseded_by', r.keeper_id::text
                         )
        FROM ranked r
        WHERE f.id = r.id AND r.rn > 1
        """
    )

    # 4. dedupe_key is now populated for every row -> enforce NOT NULL +
    #    the partial unique index (DB-enforced idempotency for open findings).
    op.execute('ALTER TABLE sentinel_finding ALTER COLUMN dedupe_key SET NOT NULL')
    if not _index_exists(schema, "sentinel_finding_open_dedupe_idx"):
        op.execute(
            f'CREATE UNIQUE INDEX sentinel_finding_open_dedupe_idx '
            f'ON "{schema}".sentinel_finding (workspace_id, dedupe_key) '
            f"WHERE status = 'open'"
        )


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0020 requires -x tenant=<schema>")
    if not _table_exists(schema, "sentinel_finding"):
        return

    if _index_exists(schema, "sentinel_finding_open_dedupe_idx"):
        op.execute(
            f'DROP INDEX "{schema}".sentinel_finding_open_dedupe_idx'
        )
    # Note: dismissed-as-duplicate rows are NOT reopened (the collapse is a
    # one-way data correction). Only the schema additions are reverted.
    for col in ("seen_count", "last_seen_at", "dedupe_key"):
        if _column_exists(schema, "sentinel_finding", col):
            op.execute(f'ALTER TABLE sentinel_finding DROP COLUMN {col}')
