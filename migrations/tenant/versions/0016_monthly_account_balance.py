"""per-workspace schema: monthly_account_balance materialized aggregates.

Revision ID: 0016_monthly_account_balance
Revises: 0015_journal_account_numbers
Create Date: 2026-05-31

Bug 13 (L-effort), part 2 of 2 — pre-aggregated monthly balances.

Why
===
Trial Balance and P&L can be answered by summing pre-computed monthly buckets
instead of scanning the journal. For a 100K-entry workspace, the trial balance
goes from "scan + group 200K UNION ALL rows" to "sum ~120 buckets per account"
— two orders of magnitude less work, comfortably under the 200ms target.

Cash flow doesn't fit this shape (it joins counter-account types per row), so
it stays on the indexed scan path from 0015. Reports still produce identical
numbers because both paths read the same underlying postings.

Schema
======
One row per (entity_id, account_number, year, month). Stores debit_total_cents,
credit_total_cents — keeping integer cents avoids the Decimal-vs-NUMERIC drift
issue we hit in earlier reports. ``ending_balance_cents`` is debit-minus-credit
for the bucket (debit-normal convention).

Maintained by an AFTER INSERT trigger on journal_entry: each insert UPSERTs
into BOTH bucket rows (debit side + credit side) using ON CONFLICT … DO UPDATE
SET total = monthly_account_balance.total + EXCLUDED.total. This is additive
and idempotent at the row level (a re-fired trigger on the same row would
double-count, but each journal_entry row triggers exactly once).

The trigger is AFTER INSERT only — journal_entry is append-only (the BEFORE
UPDATE OR DELETE trigger from 0003 prevents mutation), so we never need to
rebalance on UPDATE/DELETE.

Initial population
==================
The migration runs an INSERT ... SELECT to backfill all historical entries.
ON CONFLICT DO UPDATE so re-running the migration is a no-op (the same row
contributes once because the SELECT GROUP BYs the same key).

Refresh strategy
================
- Steady state: AFTER INSERT trigger keeps the table current per-entry.
- Period close / month rollover: no special action — the trigger has already
  bucketed every row by its effective_date.
- Manual catch-up: if a workspace is suspected of drift (rare), DELETE the
  rows + re-run the INSERT … SELECT … ON CONFLICT … upsert. See
  agents/_shared/reports_performance.md for the operational runbook.

Safety
======
- Append-only by policy: GRANT SELECT, INSERT, UPDATE — no DELETE. Trigger-
  driven inserts use UPSERT so no row is ever deleted.
- RLS-gated on workspace_id GUC (mirrors 0005's pattern for tenant tables).
- BEFORE TRUNCATE guard via the existing forbid_append_only_truncate() function
  from 0005 — same defense in depth as the other ledger tables.

Idempotency
===========
- Every DDL is information_schema-guarded.
- Initial population is INSERT … ON CONFLICT DO UPDATE — re-runs are safe.
- The trigger is CREATE OR REPLACE FUNCTION + DROP TRIGGER IF EXISTS / CREATE.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0016_monthly_account_balance"
down_revision: str | None = "0015_journal_account_numbers"
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
        raise RuntimeError("0016 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. monthly_account_balance: one row per (entity, account_number, y, m).
    #
    # Built with raw DDL (not op.create_table) so we can use Postgres'
    # GENERATED ALWAYS AS … STORED for ending_balance_cents without going
    # through sa.Computed (sa.Computed in SQLAlchemy 2.x has worked in our
    # other migrations, but raw DDL keeps the generated-column behaviour
    # explicit + parallels the 0015 pattern).
    # ------------------------------------------------------------------
    if not _table_exists(schema, "monthly_account_balance"):
        op.execute(
            f"""
            CREATE TABLE "{schema}".monthly_account_balance (
                id UUID NOT NULL DEFAULT gen_random_uuid(),
                workspace_id UUID NOT NULL,
                entity_id UUID NOT NULL,
                account_number INT NOT NULL,
                year INT NOT NULL,
                month INT NOT NULL,
                debit_total_cents BIGINT NOT NULL DEFAULT 0,
                credit_total_cents BIGINT NOT NULL DEFAULT 0,
                ending_balance_cents BIGINT NOT NULL
                    GENERATED ALWAYS AS (debit_total_cents - credit_total_cents) STORED,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (id),
                CONSTRAINT uq_monthly_balance_bucket
                    UNIQUE (entity_id, account_number, year, month),
                CONSTRAINT ck_monthly_balance_month
                    CHECK (month >= 1 AND month <= 12)
            )
            """
        )
        op.execute(
            f"""
            COMMENT ON TABLE "{schema}".monthly_account_balance IS
            'Pre-aggregated monthly debit/credit totals per (entity, GL account). '
            'Maintained by an AFTER INSERT trigger on journal_entry. Used by the '
            'Trial Balance + P&L report queries for sub-200ms response at 100K+ '
            'journal entries.'
            """
        )

    # Indexes — the main read pattern is "(entity, account_number) range scan
    # over (year, month)" for an as_of cutoff or period filter.
    if not _index_exists(schema, "ix_mab_entity_account"):
        op.execute(
            f'CREATE INDEX ix_mab_entity_account '
            f'ON "{schema}".monthly_account_balance '
            f'(entity_id, account_number, year, month)'
        )
    # Workspace-wide (consolidated) reads scan by (workspace, year, month).
    if not _index_exists(schema, "ix_mab_workspace_period"):
        op.execute(
            f'CREATE INDEX ix_mab_workspace_period '
            f'ON "{schema}".monthly_account_balance '
            f'(workspace_id, year, month, account_number)'
        )

    # ------------------------------------------------------------------
    # 2. Trigger function: incrementally maintain the bucket on INSERT.
    # ------------------------------------------------------------------
    # Exclusion of correction sources mirrors the trial-balance query
    # convention (reversal/recategorization cancel in pairs; including them
    # would double-count). The trigger therefore SKIPs them: their absence
    # from monthly_account_balance is intentional.
    #
    # We multiply by 100 + cast to BIGINT so we store integer cents — avoids
    # the NUMERIC drift problem. amount is NUMERIC(20,4) absolute value.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION "{schema}".monthly_balance_apply_je()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_year INT;
            v_month INT;
            v_amount_cents BIGINT;
        BEGIN
            -- Skip non-posted + correction sources to match the report-query
            -- exclusion convention.
            IF NEW.status <> 'posted' THEN
                RETURN NEW;
            END IF;
            IF NEW.source IN ('reversal', 'recategorization') THEN
                RETURN NEW;
            END IF;
            -- Defensive: missing account numbers => nothing to bucket.
            IF NEW.debit_account_number IS NULL AND NEW.credit_account_number IS NULL THEN
                RETURN NEW;
            END IF;

            v_year := EXTRACT(YEAR FROM NEW.effective_date)::int;
            v_month := EXTRACT(MONTH FROM NEW.effective_date)::int;
            v_amount_cents := (NEW.amount * 100)::bigint;

            -- Debit side
            IF NEW.debit_account_number IS NOT NULL THEN
                INSERT INTO monthly_account_balance (
                    workspace_id, entity_id, account_number,
                    year, month, debit_total_cents, credit_total_cents
                )
                VALUES (
                    NEW.workspace_id, NEW.entity_id, NEW.debit_account_number,
                    v_year, v_month, v_amount_cents, 0
                )
                ON CONFLICT (entity_id, account_number, year, month) DO UPDATE
                SET debit_total_cents = monthly_account_balance.debit_total_cents
                                        + EXCLUDED.debit_total_cents,
                    updated_at = now();
            END IF;

            -- Credit side
            IF NEW.credit_account_number IS NOT NULL THEN
                INSERT INTO monthly_account_balance (
                    workspace_id, entity_id, account_number,
                    year, month, debit_total_cents, credit_total_cents
                )
                VALUES (
                    NEW.workspace_id, NEW.entity_id, NEW.credit_account_number,
                    v_year, v_month, 0, v_amount_cents
                )
                ON CONFLICT (entity_id, account_number, year, month) DO UPDATE
                SET credit_total_cents = monthly_account_balance.credit_total_cents
                                         + EXCLUDED.credit_total_cents,
                    updated_at = now();
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )

    op.execute(
        f'DROP TRIGGER IF EXISTS trg_je_monthly_balance '
        f'ON "{schema}".journal_entry'
    )
    op.execute(
        f"""
        CREATE TRIGGER trg_je_monthly_balance
        AFTER INSERT ON "{schema}".journal_entry
        FOR EACH ROW
        EXECUTE FUNCTION "{schema}".monthly_balance_apply_je()
        """
    )

    # ------------------------------------------------------------------
    # 3. Initial population for any pre-existing journal_entry rows.
    #
    # ON CONFLICT … DO UPDATE so a re-run of the migration is a no-op (each
    # entry is summed into the bucket exactly once because the source query
    # already GROUP BYs the bucket key).
    # ------------------------------------------------------------------
    op.execute(
        f"""
        WITH debit_side AS (
            SELECT
                workspace_id,
                entity_id,
                debit_account_number AS account_number,
                EXTRACT(YEAR FROM effective_date)::int AS year,
                EXTRACT(MONTH FROM effective_date)::int AS month,
                SUM((amount * 100)::bigint) AS debit_cents
            FROM "{schema}".journal_entry
            WHERE status = 'posted'
              AND source NOT IN ('reversal', 'recategorization')
              AND debit_account_number IS NOT NULL
            GROUP BY workspace_id, entity_id, debit_account_number,
                     EXTRACT(YEAR FROM effective_date),
                     EXTRACT(MONTH FROM effective_date)
        ),
        credit_side AS (
            SELECT
                workspace_id,
                entity_id,
                credit_account_number AS account_number,
                EXTRACT(YEAR FROM effective_date)::int AS year,
                EXTRACT(MONTH FROM effective_date)::int AS month,
                SUM((amount * 100)::bigint) AS credit_cents
            FROM "{schema}".journal_entry
            WHERE status = 'posted'
              AND source NOT IN ('reversal', 'recategorization')
              AND credit_account_number IS NOT NULL
            GROUP BY workspace_id, entity_id, credit_account_number,
                     EXTRACT(YEAR FROM effective_date),
                     EXTRACT(MONTH FROM effective_date)
        ),
        combined AS (
            SELECT
                COALESCE(d.workspace_id, c.workspace_id) AS workspace_id,
                COALESCE(d.entity_id, c.entity_id)       AS entity_id,
                COALESCE(d.account_number, c.account_number) AS account_number,
                COALESCE(d.year, c.year)                 AS year,
                COALESCE(d.month, c.month)               AS month,
                COALESCE(d.debit_cents, 0)               AS debit_total_cents,
                COALESCE(c.credit_cents, 0)              AS credit_total_cents
            FROM debit_side d
            FULL OUTER JOIN credit_side c
              ON  c.workspace_id   = d.workspace_id
              AND c.entity_id      = d.entity_id
              AND c.account_number = d.account_number
              AND c.year           = d.year
              AND c.month          = d.month
        )
        INSERT INTO "{schema}".monthly_account_balance (
            workspace_id, entity_id, account_number,
            year, month, debit_total_cents, credit_total_cents
        )
        SELECT
            workspace_id, entity_id, account_number,
            year, month, debit_total_cents, credit_total_cents
        FROM combined
        ON CONFLICT (entity_id, account_number, year, month) DO UPDATE
        SET debit_total_cents  = EXCLUDED.debit_total_cents,
            credit_total_cents = EXCLUDED.credit_total_cents,
            updated_at         = now()
        """
    )

    # ------------------------------------------------------------------
    # 4. Least-privilege grants for the runtime role. SELECT + INSERT +
    #    UPDATE so the trigger (run as the inserting role) can upsert.
    #    No DELETE.
    # ------------------------------------------------------------------
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".monthly_account_balance TO {RUNTIME_ROLE}'
    )
    op.execute(
        f'REVOKE DELETE, TRUNCATE ON "{schema}".monthly_account_balance FROM {RUNTIME_ROLE}'
    )

    # BEFORE TRUNCATE guard — re-uses the function from 0005.
    op.execute(
        f'DROP TRIGGER IF EXISTS monthly_account_balance_no_truncate '
        f'ON "{schema}".monthly_account_balance'
    )
    op.execute(
        f"""
        CREATE TRIGGER monthly_account_balance_no_truncate
        BEFORE TRUNCATE ON "{schema}".monthly_account_balance
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()
        """
    )

    # ------------------------------------------------------------------
    # 5. RLS — mirror the workspace_isolation policy from 0005.
    # ------------------------------------------------------------------
    op.execute(
        f'ALTER TABLE "{schema}".monthly_account_balance ENABLE ROW LEVEL SECURITY'
    )
    op.execute(
        f'DROP POLICY IF EXISTS workspace_isolation '
        f'ON "{schema}".monthly_account_balance'
    )
    op.execute(
        f"""
        CREATE POLICY workspace_isolation ON "{schema}".monthly_account_balance
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
        raise RuntimeError("0016 requires -x tenant=<schema>")

    op.execute(
        f'DROP TRIGGER IF EXISTS trg_je_monthly_balance ON "{schema}".journal_entry'
    )
    op.execute(f'DROP FUNCTION IF EXISTS "{schema}".monthly_balance_apply_je()')

    if _table_exists(schema, "monthly_account_balance"):
        op.execute(
            f'DROP POLICY IF EXISTS workspace_isolation '
            f'ON "{schema}".monthly_account_balance'
        )
        op.execute(
            f'DROP TRIGGER IF EXISTS monthly_account_balance_no_truncate '
            f'ON "{schema}".monthly_account_balance'
        )
        for idx in ("ix_mab_workspace_period", "ix_mab_entity_account"):
            if _index_exists(schema, idx):
                op.execute(f'DROP INDEX "{schema}".{idx}')
        op.execute(f'DROP TABLE "{schema}".monthly_account_balance')
