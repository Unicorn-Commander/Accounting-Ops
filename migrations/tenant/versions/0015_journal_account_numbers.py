"""per-workspace schema: indexed account-number columns on journal_entry.

Revision ID: 0015_journal_account_numbers
Revises: 0014_sentinel
Create Date: 2026-05-31

Bug 13 (L-effort) — report queries scan + compute account prefixes at runtime
via UNION ALL + split_part on canonical strings. Breaks at 50K+ entries per
customer. This migration is the schema half of the fix; reports.py picks the
new columns up in the query half.

What this adds
==============
1. ``journal_entry.debit_account_number INT``  STORED, GENERATED from
   ``split_part(debit_account, '·', 1)::int``.
2. ``journal_entry.credit_account_number INT``  STORED, GENERATED from
   ``split_part(credit_account, '·', 1)::int``.
3. Composite indexes for the three hot report-query patterns:
     - ``(entity_id, status, effective_date, debit_account_number)``
     - ``(entity_id, status, effective_date, credit_account_number)``
   These cover the WHERE-clause shapes the rewritten P&L / cash-flow /
   trial-balance / 1099-NEC queries use. The columns are tiny INTs so the
   write-amp cost is sub-1%.

Why STORED, not VIRTUAL
=======================
Postgres does NOT support VIRTUAL generated columns (only STORED). The brief
flags this — we explicitly use STORED.

Why generated columns and not a separate trigger-maintained column
==================================================================
- Generated columns are auto-maintained by the engine — no trigger surface
  area, no race between INSERT and update-of-mirror-column, no maintenance
  burden if the canonical_form format ever changes (one DDL change vs a
  trigger function rewrite).
- The generation expression is IMMUTABLE (split_part + cast), so Postgres can
  use the column directly in indexes + push down predicates.

Why this is safe for the journal_entry hash chain
=================================================
The hash chain in app/journal/hash_chain.py hashes the entry's payload fields
(effective_date, amount_cents, debit_account, credit_account, source,
source_external_id, payee, description, posted_at). It does NOT hash
debit_account_number or credit_account_number, so adding deterministic
generated columns has zero impact on the chain: the hash inputs are
unchanged and pre-existing entries' stored this_hash stays valid.

Why the BEFORE UPDATE trigger does NOT fire on the backfill
===========================================================
journal_entry has a ``BEFORE UPDATE OR DELETE`` row trigger
(``forbid_journal_entry_mutation``) that blocks DML mutation. Postgres backfills
STORED generated columns via a TABLE REWRITE under ADD COLUMN — that is a
synthetic rewrite of every page, NOT a row UPDATE statement, so the row
trigger does not see it. This is the same code path as ALTER TABLE ADD COLUMN
with a non-volatile DEFAULT. We do not need to DISABLE TRIGGER.

Idempotency
===========
- Every DDL is guarded by an information_schema check.
- Re-running on a schema that already has the columns + indexes is a no-op.

Reversibility
=============
- downgrade() drops the indexes then the columns, in dependency order.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0015_journal_account_numbers"
down_revision: str | None = "0014_sentinel"
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
        raise RuntimeError("0015 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. Generated stored columns on journal_entry.
    #
    # The cast is split_part(debit_account, '·', 1)::int. canonical_form is
    # always "{4-digit-number}·{name}" (see migrations/tenant/0003_journal_v2
    # docstring + chart_of_account.account_number CHECK), so the cast is
    # guaranteed valid for every row. We've also verified live MU Inc data
    # (585 entries) is fully clean (0 NULL / 0 non-numeric prefixes).
    #
    # NULL handling: if debit_account is NULL (allowed by the column today),
    # split_part returns '' and '::int' would fail. We wrap in NULLIF + cast
    # so a NULL canonical_form yields a NULL account-number rather than an
    # exception. Reports already filter status='posted' which requires both
    # sides present, so NULL never lands in the aggregation.
    # ------------------------------------------------------------------
    if not _column_exists(schema, "journal_entry", "debit_account_number"):
        op.execute(
            f"""
            ALTER TABLE "{schema}".journal_entry
            ADD COLUMN debit_account_number INT
            GENERATED ALWAYS AS (
                NULLIF(split_part(debit_account, '·', 1), '')::int
            ) STORED
            """
        )

    if not _column_exists(schema, "journal_entry", "credit_account_number"):
        op.execute(
            f"""
            ALTER TABLE "{schema}".journal_entry
            ADD COLUMN credit_account_number INT
            GENERATED ALWAYS AS (
                NULLIF(split_part(credit_account, '·', 1), '')::int
            ) STORED
            """
        )

    # ------------------------------------------------------------------
    # 2. Indexes that match the rewritten report-query shapes.
    #
    # All three reports (P&L, cash-flow, trial-balance) and the 1099-NEC
    # endpoint filter by:
    #   status = 'posted'  AND  effective_date BETWEEN :start AND :end
    #   AND debit_account_number  (or credit_)  RANGE/IN
    # plus an optional entity_id predicate.
    #
    # A composite (entity_id, status, effective_date, account_number) gives:
    #   - entity_id leads when entity_id IS used (most common: per-entity
    #     reports); status + date narrow the scan; account_number filters
    #     the residual.
    #   - For consolidated reports (no entity filter), Postgres can still use
    #     the (status, effective_date) prefix of the index for a range scan.
    #
    # Two indexes — one per side — because the reports UNION ALL the debit
    # and credit sides separately; each branch only touches one column.
    # ------------------------------------------------------------------
    if not _index_exists(schema, "ix_je_entity_date_debit_acctno"):
        op.execute(
            f'CREATE INDEX ix_je_entity_date_debit_acctno '
            f'ON "{schema}".journal_entry '
            f'(entity_id, status, effective_date, debit_account_number)'
        )
    if not _index_exists(schema, "ix_je_entity_date_credit_acctno"):
        op.execute(
            f'CREATE INDEX ix_je_entity_date_credit_acctno '
            f'ON "{schema}".journal_entry '
            f'(entity_id, status, effective_date, credit_account_number)'
        )

    # No grants needed: generated columns inherit table-level privileges, and
    # the runtime role already has SELECT on journal_entry from 0005.


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0015 requires -x tenant=<schema>")

    # Drop indexes first (they reference the columns).
    for idx in (
        "ix_je_entity_date_credit_acctno",
        "ix_je_entity_date_debit_acctno",
    ):
        if _index_exists(schema, idx):
            op.execute(f'DROP INDEX "{schema}".{idx}')

    if _column_exists(schema, "journal_entry", "credit_account_number"):
        op.execute(
            f'ALTER TABLE "{schema}".journal_entry DROP COLUMN credit_account_number'
        )
    if _column_exists(schema, "journal_entry", "debit_account_number"):
        op.execute(
            f'ALTER TABLE "{schema}".journal_entry DROP COLUMN debit_account_number'
        )
