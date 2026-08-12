"""per-workspace schema: stop excluding corrections from monthly_account_balance

A recategorization posts THREE rows: the original is left untouched (the
journal is append-only), plus a reversal and a restatement. Migration 0016's
trigger skipped `source IN ('reversal','recategorization')` on the theory that
the pair "cancels" — it does not. Only the cash leg cancels; the pair's whole
net effect IS the move from the old category to the new one. Dropping legs 2
and 3 therefore leaves the WRONG original standing in the buckets.

The visible symptom is a trial balance that disagrees with the P&L and balance
sheet while still reporting balanced:true — the grand totals tie because both
excluded legs drop together, so nothing surfaces the split. Measured on the
rivergreen-coffee-co demo entity before this change:

    1010 Cash             231,015.21  vs  231,729.23   (714.02 adrift)
    5010 COGS · Inventory  47,484.30  vs   47,625.92   (141.62)
    6400 Office · Supplies    640.44  vs    1,212.84   (572.40)

This migration replaces the trigger function so future postings bucket every
posted row, then rebuilds the existing buckets from the journal.

⚠️ RLS: books.* is FORCE row-level-security, so even the table owner is subject
to the workspace_isolation policy, and migrations run as accounting_ops_admin
rather than a BYPASSRLS role. A plain DELETE/INSERT here silently affects ZERO
rows — no error, no row count to notice, and the revision still stamps as
applied. So the rebuild sets app.current_workspace per workspace and works one
at a time, and then VERIFIES the result rather than assuming it.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0030_mab_include_corrections"
down_revision: str | None = "0029_entity_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    return x_args.get("tenant") or "books"


_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION "{schema}".monthly_balance_apply_je()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_year INT;
    v_month INT;
    v_amount_cents BIGINT;
BEGIN
    -- Every POSTED row is bucketed, corrections included. A reversal and its
    -- restatement are real movements: excluding them leaves the superseded
    -- original standing and the aggregate disagrees with the journal.
    IF NEW.status <> 'posted' THEN
        RETURN NEW;
    END IF;
    IF NEW.debit_account_number IS NULL AND NEW.credit_account_number IS NULL THEN
        RETURN NEW;
    END IF;

    v_year := EXTRACT(YEAR FROM NEW.effective_date)::int;
    v_month := EXTRACT(MONTH FROM NEW.effective_date)::int;
    v_amount_cents := (NEW.amount * 100)::bigint;

    IF NEW.debit_account_number IS NOT NULL THEN
        INSERT INTO "{schema}".monthly_account_balance
            (workspace_id, entity_id, account_number, year, month,
             debit_cents, credit_cents)
        VALUES (NEW.workspace_id, NEW.entity_id, NEW.debit_account_number,
                v_year, v_month, v_amount_cents, 0)
        ON CONFLICT (workspace_id, entity_id, account_number, year, month)
        DO UPDATE SET debit_cents =
            "{schema}".monthly_account_balance.debit_cents + EXCLUDED.debit_cents;
    END IF;

    IF NEW.credit_account_number IS NOT NULL THEN
        INSERT INTO "{schema}".monthly_account_balance
            (workspace_id, entity_id, account_number, year, month,
             debit_cents, credit_cents)
        VALUES (NEW.workspace_id, NEW.entity_id, NEW.credit_account_number,
                v_year, v_month, 0, v_amount_cents)
        ON CONFLICT (workspace_id, entity_id, account_number, year, month)
        DO UPDATE SET credit_cents =
            "{schema}".monthly_account_balance.credit_cents + EXCLUDED.credit_cents;
    END IF;

    RETURN NEW;
END;
$$;
"""

_REBUILD = """
WITH debit_side AS (
    SELECT workspace_id, entity_id,
           debit_account_number AS account_number,
           EXTRACT(YEAR FROM effective_date)::int  AS year,
           EXTRACT(MONTH FROM effective_date)::int AS month,
           SUM((amount * 100)::bigint) AS debit_cents
    FROM "{schema}".journal_entry
    WHERE status = 'posted' AND debit_account_number IS NOT NULL
    GROUP BY 1,2,3,4,5
),
credit_side AS (
    SELECT workspace_id, entity_id,
           credit_account_number AS account_number,
           EXTRACT(YEAR FROM effective_date)::int  AS year,
           EXTRACT(MONTH FROM effective_date)::int AS month,
           SUM((amount * 100)::bigint) AS credit_cents
    FROM "{schema}".journal_entry
    WHERE status = 'posted' AND credit_account_number IS NOT NULL
    GROUP BY 1,2,3,4,5
)
INSERT INTO "{schema}".monthly_account_balance
    (workspace_id, entity_id, account_number, year, month, debit_cents, credit_cents)
SELECT
    COALESCE(d.workspace_id, c.workspace_id),
    COALESCE(d.entity_id, c.entity_id),
    COALESCE(d.account_number, c.account_number),
    COALESCE(d.year, c.year),
    COALESCE(d.month, c.month),
    COALESCE(d.debit_cents, 0),
    COALESCE(c.credit_cents, 0)
FROM debit_side d
FULL OUTER JOIN credit_side c
  ON  d.workspace_id = c.workspace_id
  AND d.entity_id = c.entity_id
  AND d.account_number = c.account_number
  AND d.year = c.year
  AND d.month = c.month
ON CONFLICT (workspace_id, entity_id, account_number, year, month)
DO UPDATE SET debit_cents = EXCLUDED.debit_cents,
              credit_cents = EXCLUDED.credit_cents;
"""


def upgrade() -> None:
    schema = _schema()
    bind = op.get_bind()

    bind.execute(sa.text(_TRIGGER_FN.format(schema=schema)))

    # Which workspaces actually have buckets or postings.
    workspaces = [
        r[0]
        for r in bind.execute(
            sa.text(
                f"""
                SELECT DISTINCT workspace_id FROM "{schema}".journal_entry
                UNION
                SELECT DISTINCT workspace_id FROM "{schema}".monthly_account_balance
                """
            )
        )
    ]

    for wid in workspaces:
        # FORCE RLS applies to the owner too; without this the statements below
        # match nothing and report success.
        bind.execute(
            sa.text("SELECT set_config('app.current_workspace', :wid, true)"),
            {"wid": str(wid)},
        )
        bind.execute(
            sa.text(
                f'DELETE FROM "{schema}".monthly_account_balance '
                "WHERE workspace_id = :wid"
            ),
            {"wid": str(wid)},
        )
        bind.execute(sa.text(_REBUILD.format(schema=schema)))

        # Verify, per workspace, that the buckets now equal the journal. A
        # silent no-op under RLS is exactly the failure this guards against, so
        # the migration refuses to stamp rather than leave skewed aggregates.
        drift = bind.execute(
            sa.text(
                f"""
                WITH je AS (
                    SELECT COALESCE(SUM((amount * 100)::bigint), 0) AS cents
                    FROM "{schema}".journal_entry
                    WHERE status = 'posted' AND workspace_id = :wid
                      AND debit_account_number IS NOT NULL
                ), mab AS (
                    SELECT COALESCE(SUM(debit_cents), 0) AS cents
                    FROM "{schema}".monthly_account_balance
                    WHERE workspace_id = :wid
                )
                SELECT (SELECT cents FROM je) - (SELECT cents FROM mab)
                """
            ),
            {"wid": str(wid)},
        ).scalar_one()
        if drift != 0:
            raise RuntimeError(
                f"monthly_account_balance rebuild drifted by {drift} cents for "
                f"workspace {wid} (schema {schema}). Refusing to stamp — the "
                "most likely cause is RLS silently filtering the rebuild."
            )


def downgrade() -> None:
    """Restore the correction-excluding trigger and rebuild on that basis.

    Reversible in shape, but the buckets it produces are the ones this
    migration exists to correct — the trial balance will disagree with the P&L
    again.
    """
    schema = _schema()
    bind = op.get_bind()
    bind.execute(
        sa.text(
            _TRIGGER_FN.format(schema=schema).replace(
                "    IF NEW.debit_account_number IS NULL",
                "    IF NEW.source IN ('reversal', 'recategorization') THEN\n"
                "        RETURN NEW;\n"
                "    END IF;\n"
                "    IF NEW.debit_account_number IS NULL",
                1,
            )
        )
    )
    workspaces = [
        r[0]
        for r in bind.execute(
            sa.text(f'SELECT DISTINCT workspace_id FROM "{schema}".journal_entry')
        )
    ]
    rebuild = _REBUILD.replace(
        "WHERE status = 'posted' AND debit_account_number IS NOT NULL",
        "WHERE status = 'posted' AND debit_account_number IS NOT NULL "
        "AND source NOT IN ('reversal', 'recategorization')",
    ).replace(
        "WHERE status = 'posted' AND credit_account_number IS NOT NULL",
        "WHERE status = 'posted' AND credit_account_number IS NOT NULL "
        "AND source NOT IN ('reversal', 'recategorization')",
    )
    for wid in workspaces:
        bind.execute(
            sa.text("SELECT set_config('app.current_workspace', :wid, true)"),
            {"wid": str(wid)},
        )
        bind.execute(
            sa.text(
                f'DELETE FROM "{schema}".monthly_account_balance '
                "WHERE workspace_id = :wid"
            ),
            {"wid": str(wid)},
        )
        bind.execute(sa.text(rebuild.format(schema=schema)))
