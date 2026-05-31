"""per-workspace schema: UNIQUE (entity_id, this_hash) defense-in-depth

Revision ID: 0018_journal_hash_chain_constraint
Revises: 0017_approval_status_skipped
Create Date: 2026-05-31

Adds ``uq_je_entity_this_hash`` UNIQUE constraint on
``journal_entry (entity_id, this_hash)`` as defense-in-depth for the per-entity
hash chain.

Why a DB constraint when the application already retries?
  * Today the chain-race protection lives in ``app/journal/posting.py``'s
    ``_attempt_chain_insert`` bounded retry loop — a concurrent post that
    advances the chain between ``get_last_chain_hash`` and INSERT is caught by
    PostgreSQL's IntegrityError and the post is recomputed with a fresh
    ``prev_hash`` (audit 2026-05-31 Bug 9). That retry depends on an
    IntegrityError actually firing.
  * Without a DB-level uniqueness gate on ``(entity_id, this_hash)`` two writers
    racing on the SAME prev_hash could in principle compute the SAME this_hash
    and both succeed — the chain would fork undetected. The retry loop assumes
    SOMETHING raises; without this constraint, nothing necessarily does.
  * Adding the constraint makes that "shouldn't happen" actually impossible at
    the storage layer. The retry already maps any non-``uq_je_entity_source_external``
    constraint name back to "chain race → recompute" — see Bug 9 docstring.

Safety pre-check (no data loss):
  Before adding the constraint we run a SELECT for any existing
  ``(entity_id, this_hash)`` pair that appears more than once. If even one
  duplicate exists, the migration ABORTS with a clear error pointing the
  operator at the duplicates. We do NOT silently drop or merge rows — the
  journal is append-only and any duplicate is a data-integrity event that
  needs human attention before we can lock it in.

Idempotent:
  ``CREATE UNIQUE INDEX IF NOT EXISTS`` + ``ALTER TABLE … ADD CONSTRAINT …
  USING INDEX`` is the safest pattern. A second run no-ops cleanly.

Downgrade:
  ``ALTER TABLE … DROP CONSTRAINT IF EXISTS`` + ``DROP INDEX IF EXISTS`` —
  one-way per migration only in the sense that an operator who downgrades and
  then re-upgrades will get the same defense-in-depth back; no data is
  rewritten in either direction.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0018_je_entity_this_hash_uq"
down_revision: str | None = "0017_approval_status_skipped"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT_NAME = "uq_je_entity_this_hash"
_INDEX_NAME = "uq_je_entity_this_hash_idx"


def _set_search_path() -> str:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
    return schema or ""


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0018 requires -x tenant=<schema>")

    bind = op.get_bind()

    # ---------- Safety pre-check: refuse if any duplicates exist ----------
    # The constraint we're about to add MUST NOT cause an existing row to be
    # rejected silently. Check for any (entity_id, this_hash) pair already
    # present more than once; if found, abort with a precise diagnostic so an
    # operator can investigate before we lock the invariant in.
    duplicates = bind.execute(
        sa.text(
            """
            SELECT entity_id::text AS entity_id,
                   encode(this_hash, 'hex') AS this_hash_hex,
                   COUNT(*) AS dup_count
            FROM journal_entry
            WHERE this_hash IS NOT NULL
            GROUP BY entity_id, this_hash
            HAVING COUNT(*) > 1
            ORDER BY dup_count DESC
            LIMIT 5
            """
        )
    ).mappings().all()
    if duplicates:
        sample = "; ".join(
            f"entity={row['entity_id']} hash={row['this_hash_hex'][:16]}... x{row['dup_count']}"
            for row in duplicates
        )
        raise RuntimeError(
            "0018_journal_hash_chain_constraint: REFUSING to add UNIQUE "
            "(entity_id, this_hash) — duplicates already present in "
            f"journal_entry. Sample: {sample}. The journal is append-only; "
            "do NOT delete or update rows to make this migration pass. "
            "Operator action required: investigate how the duplicate was "
            "produced (likely pre-Bug-9 fix), capture forensic evidence, "
            "then decide on a path forward (e.g. tombstone via a structured "
            "audit annotation). Re-run this migration only after the "
            "underlying anomaly is resolved."
        )

    # ---------- Idempotent constraint creation ----------
    # CREATE UNIQUE INDEX IF NOT EXISTS handles the second-run case cleanly.
    # Then ALTER TABLE … ADD CONSTRAINT … USING INDEX promotes it to a named
    # UNIQUE constraint so it shows up as a constraint (not just an index) in
    # ``pg_constraint`` and so PostgreSQL reports it by name on a violation —
    # which is what ``_integrity_constraint_name`` in app/journal/posting.py
    # scrapes to disambiguate idempotency vs chain-race conflicts. The
    # constraint name (``uq_je_entity_this_hash``) MUST match the application
    # mapping added in app/journal/posting.py.
    op.execute(
        sa.text(
            f'CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} '
            f"ON journal_entry (entity_id, this_hash) "
            f"WHERE this_hash IS NOT NULL"
        )
    )
    # Promote to a named UNIQUE constraint. Wrap in a does-it-already-exist
    # guard so a repeat run is a no-op. Postgres' ADD CONSTRAINT USING INDEX
    # requires the index to NOT be a partial index in some versions; we use
    # a separate ADD CONSTRAINT path that creates a fresh unique constraint
    # via UNIQUE on the same columns, with IF NOT EXISTS via pg_constraint.
    exists = bind.execute(
        sa.text(
            """
            SELECT 1 FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE c.conname = :name
              AND t.relname = 'journal_entry'
              AND n.nspname = :schema
            LIMIT 1
            """
        ),
        {"name": _CONSTRAINT_NAME, "schema": schema},
    ).first()
    if not exists:
        # Add the constraint backed by the partial unique index above. We can't
        # use ADD CONSTRAINT … USING INDEX with a partial index, so we add an
        # explicit unique constraint covering the same columns; PostgreSQL will
        # create its own non-partial backing index. The partial index above
        # serves the application's lookups efficiently; the constraint enforces
        # the invariant. Both can co-exist (Postgres dedupes on rows where
        # this_hash IS NULL only via the constraint, not the partial index —
        # which is correct: a NULL this_hash never collides with another NULL
        # under SQL semantics, so the constraint allows it).
        op.create_unique_constraint(
            _CONSTRAINT_NAME,
            "journal_entry",
            ["entity_id", "this_hash"],
        )


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0018 requires -x tenant=<schema>")

    # Drop in reverse order: constraint first (which depends on its backing
    # index), then the explicit partial index. IF EXISTS keeps the downgrade
    # idempotent for partial-run recovery scenarios.
    op.execute(
        sa.text(
            f"ALTER TABLE journal_entry DROP CONSTRAINT IF EXISTS {_CONSTRAINT_NAME}"
        )
    )
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
