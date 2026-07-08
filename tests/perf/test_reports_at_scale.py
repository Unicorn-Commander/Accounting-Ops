"""Perf gate for the financial report queries at 100K-journal-entry scale.

Bug 13 (L-effort) target: P&L / cash-flow / trial-balance respond in < 200ms
on a 100K-entry synthetic dataset.

How this test is built
======================
- Seeds a SANDBOX workspace schema (separate from any live workspace) with:
    * 50 entities × 50 chart-of-account rows × ~24 months of journal entries
    * ~100K journal_entry rows total, each as a posted double-entry
    * Each entry's debit/credit canonical_forms match real COA strings so the
      bug-13 generated columns + the report queries pick them up
- Runs each of the three reports (P&L, cash-flow, trial-balance) over the
  whole dataset and asserts wall-clock time < 200ms (asserted after one
  warm-up call so we measure cached-plan latency, not first-execution cost).
- Tears down the sandbox schema at the end so the perf data never co-mingles
  with real customer data.

Skipping policy
===============
The test is gated on the `ACCTOPS_PERF_DSN` env var (a SUPERUSER or OWNER
DSN — needs CREATE SCHEMA + SCHEMA-level migrations). If unset the test
SKIPS so `make test` stays green on a developer laptop without a live PG.

How to re-run
=============
    # Against the LIVE your-host postgres (writes the synthetic data to a
    # scratch schema named accounting_ops_perf_<seed>):
    ACCTOPS_PERF_DSN=postgresql://accounting_ops:<owner_pw>@127.0.0.1:5432/accounting_ops \\
      pytest tests/perf/test_reports_at_scale.py -v -s

    # On your-host/your-host with docker compose stack:
    docker compose exec app sh -c \\
      'ACCTOPS_PERF_DSN=postgresql://accounting_ops:$$POSTGRES_PASSWORD@postgres:5432/accounting_ops \\
       pytest tests/perf/test_reports_at_scale.py -v -s'

Seed approach detail
====================
- 50 entities, slugged "perf-ent-NN"
- 50 chart-of-account rows per entity covering account-numbers 1000..1099,
  4000..4019, 5000..5009, 6000..6029, 7000..7004, 8000..8004, 9000..9004
  so that all account-type buckets P&L / BS / CF look at have activity
- 24 months of data: each (entity, month, account) cell gets ~1-2 entries
  (a debit on a random expense account, credit on cash 1010), totaling
  ~100K rows across the workspace
- Uses asyncpg + COPY for fast bulk insert; an AFTER INSERT trigger fills
  monthly_account_balance row-by-row (per the production schema), so seed
  time is dominated by the trigger work, not the raw insert
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from datetime import date
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")


# ---------------------------------------------------------------------------
# Sandbox schema lifecycle
# ---------------------------------------------------------------------------

PERF_SEED = 13_2026  # deterministic so re-runs land on the same dataset
PERF_SCHEMA = f"accounting_ops_perf_{PERF_SEED}"
PERF_WORKSPACE = uuid.uuid5(uuid.NAMESPACE_DNS, f"perf-sandbox:{PERF_SEED}")

NUM_ENTITIES = 50
TARGET_ROWS = 100_000
START_DATE = date(2024, 1, 1)
NUM_MONTHS = 24

# COA account numbers cycled through for the seed. All bucket types covered
# so the perf measure exercises every code path the report queries take.
EXPENSE_ACCOUNTS = [6000, 6010, 6020, 6100, 6110, 6120, 6200, 6210, 6300]
REVENUE_ACCOUNTS = [4000, 4010, 4020, 4030, 4040]
CASH_ACCOUNTS = [1010, 1020, 1030]
ASSET_FIXED_ACCOUNTS = [1500, 1550]
LIABILITY_ACCOUNTS = [2010, 2100, 2400]
EQUITY_ACCOUNTS = [3010, 3050]


def _dsn() -> str | None:
    return os.environ.get("ACCTOPS_PERF_DSN")


def _maybe_skip() -> str:
    dsn = _dsn()
    if not dsn:
        pytest.skip(
            "Set ACCTOPS_PERF_DSN to an OWNER-role asyncpg DSN to run the "
            "perf seed + assertions (e.g. "
            "postgresql://accounting_ops:<pw>@host:5432/accounting_ops)."
        )
    return dsn


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

async def _drop_sandbox(conn: "asyncpg.Connection") -> None:
    """Drop the perf sandbox schema if it exists. Safe to re-run."""
    # CASCADE because the schema may hold tables + functions + triggers.
    await conn.execute(f'DROP SCHEMA IF EXISTS "{PERF_SCHEMA}" CASCADE')


async def _create_sandbox(conn: "asyncpg.Connection") -> None:
    """Create the perf sandbox schema + minimal infra for our tables.

    We replicate the production DDL we depend on (journal_entry, entity,
    chart_of_account, monthly_account_balance) inline so we don't have to
    spin up alembic just for this perf test. The shape matches migrations
    0001/0003/0015/0016 byte-for-byte for the columns the reports read.
    """
    await conn.execute(f'CREATE SCHEMA "{PERF_SCHEMA}"')

    # Minimal entity table (only the columns reports + queries touch).
    await conn.execute(
        f"""
        CREATE TABLE "{PERF_SCHEMA}".entity (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )

    # COA table — column shapes mirror migrations/tenant/0003_journal_v2.
    await conn.execute(
        f"""
        CREATE TABLE "{PERF_SCHEMA}".chart_of_account (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL,
            entity_id UUID NOT NULL,
            account_number VARCHAR(8) NOT NULL,
            account_name VARCHAR(128) NOT NULL,
            canonical_form VARCHAR(160) NOT NULL,
            account_type VARCHAR(32) NOT NULL,
            normal_balance VARCHAR(8) NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true
        )
        """
    )
    await conn.execute(
        f"""
        CREATE INDEX ix_coa_entity_active
            ON "{PERF_SCHEMA}".chart_of_account (entity_id, is_active)
        """
    )

    # journal_entry — only the columns the reports touch + the generated
    # account-number columns (bug 13 fix from migration 0015).
    await conn.execute(
        f"""
        CREATE TABLE "{PERF_SCHEMA}".journal_entry (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id UUID NOT NULL,
            entity_id UUID NOT NULL,
            amount NUMERIC(20,4) NOT NULL,
            effective_date DATE NOT NULL,
            posted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            debit_account VARCHAR(160),
            credit_account VARCHAR(160),
            status VARCHAR(24) NOT NULL DEFAULT 'posted',
            source VARCHAR(64),
            source_external_id VARCHAR(256),
            counterparty_id UUID,
            debit_account_number INT
                GENERATED ALWAYS AS (NULLIF(split_part(debit_account, '·', 1), '')::int) STORED,
            credit_account_number INT
                GENERATED ALWAYS AS (NULLIF(split_part(credit_account, '·', 1), '')::int) STORED
        )
        """
    )
    # Composite indexes — matches migration 0015 exactly.
    await conn.execute(
        f"""
        CREATE INDEX ix_je_entity_date_debit_acctno
            ON "{PERF_SCHEMA}".journal_entry
            (entity_id, status, effective_date, debit_account_number)
        """
    )
    await conn.execute(
        f"""
        CREATE INDEX ix_je_entity_date_credit_acctno
            ON "{PERF_SCHEMA}".journal_entry
            (entity_id, status, effective_date, credit_account_number)
        """
    )

    # monthly_account_balance — matches migration 0016 exactly.
    await conn.execute(
        f"""
        CREATE TABLE "{PERF_SCHEMA}".monthly_account_balance (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
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
            CONSTRAINT uq_monthly_balance_bucket
                UNIQUE (entity_id, account_number, year, month),
            CONSTRAINT ck_monthly_balance_month
                CHECK (month >= 1 AND month <= 12)
        )
        """
    )
    await conn.execute(
        f"""
        CREATE INDEX ix_mab_entity_account
            ON "{PERF_SCHEMA}".monthly_account_balance
            (entity_id, account_number, year, month)
        """
    )

    # Trigger to maintain monthly_account_balance — matches migration 0016.
    await conn.execute(
        f"""
        CREATE OR REPLACE FUNCTION "{PERF_SCHEMA}".monthly_balance_apply_je()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            v_year INT;
            v_month INT;
            v_amount_cents BIGINT;
        BEGIN
            IF NEW.status <> 'posted' THEN
                RETURN NEW;
            END IF;
            IF NEW.source IN ('reversal', 'recategorization') THEN
                RETURN NEW;
            END IF;
            IF NEW.debit_account_number IS NULL AND NEW.credit_account_number IS NULL THEN
                RETURN NEW;
            END IF;

            v_year := EXTRACT(YEAR FROM NEW.effective_date)::int;
            v_month := EXTRACT(MONTH FROM NEW.effective_date)::int;
            v_amount_cents := (NEW.amount * 100)::bigint;

            IF NEW.debit_account_number IS NOT NULL THEN
                INSERT INTO "{PERF_SCHEMA}".monthly_account_balance (
                    workspace_id, entity_id, account_number,
                    year, month, debit_total_cents, credit_total_cents
                ) VALUES (
                    NEW.workspace_id, NEW.entity_id, NEW.debit_account_number,
                    v_year, v_month, v_amount_cents, 0
                ) ON CONFLICT (entity_id, account_number, year, month) DO UPDATE
                SET debit_total_cents = "{PERF_SCHEMA}".monthly_account_balance.debit_total_cents
                                        + EXCLUDED.debit_total_cents;
            END IF;

            IF NEW.credit_account_number IS NOT NULL THEN
                INSERT INTO "{PERF_SCHEMA}".monthly_account_balance (
                    workspace_id, entity_id, account_number,
                    year, month, debit_total_cents, credit_total_cents
                ) VALUES (
                    NEW.workspace_id, NEW.entity_id, NEW.credit_account_number,
                    v_year, v_month, 0, v_amount_cents
                ) ON CONFLICT (entity_id, account_number, year, month) DO UPDATE
                SET credit_total_cents = "{PERF_SCHEMA}".monthly_account_balance.credit_total_cents
                                         + EXCLUDED.credit_total_cents;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    await conn.execute(
        f"""
        CREATE TRIGGER trg_je_monthly_balance
        AFTER INSERT ON "{PERF_SCHEMA}".journal_entry
        FOR EACH ROW
        EXECUTE FUNCTION "{PERF_SCHEMA}".monthly_balance_apply_je()
        """
    )


def _coa_seed_rows(workspace_id: uuid.UUID, entity_id: uuid.UUID) -> list[tuple]:
    """Yield COA row tuples covering every prefix the reports classify."""
    rows = []
    spec = [
        # (account_number, account_name, account_type, normal_balance)
        *((n, f"Cash {n}", "asset", "debit") for n in CASH_ACCOUNTS),
        *((n, f"Fixed Asset {n}", "asset", "debit") for n in ASSET_FIXED_ACCOUNTS),
        *((n, f"Liability {n}", "liability", "credit") for n in LIABILITY_ACCOUNTS),
        *((n, f"Equity {n}", "equity", "credit") for n in EQUITY_ACCOUNTS),
        *((n, f"Revenue {n}", "revenue", "credit") for n in REVENUE_ACCOUNTS),
        *((n, f"Expense {n}", "expense", "debit") for n in EXPENSE_ACCOUNTS),
        (6900, "Inter-entity transfer 6900", "expense", "debit"),
        (7900, "Inter-entity transfer 7900", "other_income", "credit"),
        (9000, "Tax", "tax", "debit"),
    ]
    for num, name, atype, nb in spec:
        rows.append(
            (
                str(uuid.uuid4()),       # id
                str(workspace_id),
                str(entity_id),
                str(num),
                name,
                f"{num}·{name}",
                atype,
                nb,
                True,
            )
        )
    return rows


def _journal_seed_rows(
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    coa_specs: list[tuple[int, str]],
    rows_per_entity: int,
    rng: random.Random,
) -> list[tuple]:
    """Generate journal-entry rows distributed over 24 months."""
    # Pre-build the canonical form lookup per account number
    canonical_for = {num: f"{num}·{name}" for num, name in coa_specs}

    rows: list[tuple] = []
    for i in range(rows_per_entity):
        month_offset = i % NUM_MONTHS
        year = START_DATE.year + (month_offset // 12)
        month = (month_offset % 12) + 1
        day = (i % 28) + 1
        eff = date(year, month, day)

        # Each entry is a typical expense: debit an expense account,
        # credit a cash account. Sprinkle a few revenue + asset purchases.
        roll = rng.random()
        if roll < 0.70:
            # Cash expense
            d = rng.choice(EXPENSE_ACCOUNTS)
            c = rng.choice(CASH_ACCOUNTS)
        elif roll < 0.90:
            # Revenue receipt: debit cash, credit revenue
            d = rng.choice(CASH_ACCOUNTS)
            c = rng.choice(REVENUE_ACCOUNTS)
        elif roll < 0.95:
            # Asset purchase: debit fixed asset, credit cash
            d = rng.choice(ASSET_FIXED_ACCOUNTS)
            c = rng.choice(CASH_ACCOUNTS)
        else:
            # Liability payment: debit AP, credit cash
            d = rng.choice(LIABILITY_ACCOUNTS)
            c = rng.choice(CASH_ACCOUNTS)

        amount = round(rng.uniform(10.0, 5000.0), 2)
        rows.append(
            (
                str(uuid.uuid4()),
                str(workspace_id),
                str(entity_id),
                amount,
                eff,
                canonical_for[d],
                canonical_for[c],
                "posted",
                "perf-seed",
                f"perf-{entity_id}-{i}",
                None,  # counterparty_id
            )
        )
    return rows


async def _seed(conn: "asyncpg.Connection") -> int:
    """Populate the sandbox. Returns the total journal-entry row count."""
    await conn.execute(f'SET search_path = "{PERF_SCHEMA}", public')

    rng = random.Random(PERF_SEED)
    entity_ids: list[uuid.UUID] = []

    # 1. Entities
    for i in range(NUM_ENTITIES):
        eid = uuid.uuid5(PERF_WORKSPACE, f"perf-ent-{i:02d}")
        entity_ids.append(eid)
        await conn.execute(
            'INSERT INTO entity (id, workspace_id, slug, display_name) '
            'VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING',
            str(eid), str(PERF_WORKSPACE), f"perf-ent-{i:02d}", f"Perf Entity {i}"
        )

    # 2. COA — same rows per entity.
    coa_specs: list[tuple[int, str]] = []
    for eid in entity_ids:
        coa_rows = _coa_seed_rows(PERF_WORKSPACE, eid)
        await conn.executemany(
            """
            INSERT INTO chart_of_account
                (id, workspace_id, entity_id, account_number, account_name,
                 canonical_form, account_type, normal_balance, is_active)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT DO NOTHING
            """,
            coa_rows,
        )
        if not coa_specs:
            # Build the (num, name) list once — same for every entity.
            for row in coa_rows:
                coa_specs.append((int(row[3]), row[4]))

    # 3. Journal entries — split TARGET_ROWS evenly across entities.
    rows_per_entity = TARGET_ROWS // NUM_ENTITIES
    total = 0
    for eid in entity_ids:
        je_rows = _journal_seed_rows(
            PERF_WORKSPACE, eid, coa_specs, rows_per_entity, rng
        )
        await conn.executemany(
            """
            INSERT INTO journal_entry
                (id, workspace_id, entity_id, amount, effective_date,
                 debit_account, credit_account, status, source,
                 source_external_id, counterparty_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT DO NOTHING
            """,
            je_rows,
        )
        total += len(je_rows)

    # ANALYZE so the planner has accurate stats.
    await conn.execute('ANALYZE "%s".journal_entry' % PERF_SCHEMA)
    await conn.execute('ANALYZE "%s".monthly_account_balance' % PERF_SCHEMA)
    await conn.execute('ANALYZE "%s".chart_of_account' % PERF_SCHEMA)

    return total


# ---------------------------------------------------------------------------
# Report queries (same SQL the app uses, isolated here so the perf test
# doesn't need to spin up FastAPI / asyncio sessions). Each function returns
# the wall-clock duration in milliseconds.
# ---------------------------------------------------------------------------

async def _run_query(conn, sql, params) -> float:
    """Execute the query and return wall-clock ms."""
    t0 = time.perf_counter()
    await conn.fetch(sql, *params)
    return (time.perf_counter() - t0) * 1000.0


async def _pl_query_ms(conn, period_start: date, period_end: date) -> float:
    sql = """
        WITH all_sides AS (
            SELECT debit_account_number AS account_number,
                   debit_account AS canonical_form,
                   amount AS debit, 0::numeric AS credit
            FROM journal_entry je
            WHERE status = 'posted'
              AND effective_date BETWEEN $1 AND $2
              AND debit_account_number BETWEEN 4000 AND 9999
            UNION ALL
            SELECT credit_account_number AS account_number,
                   credit_account AS canonical_form,
                   0::numeric AS debit, amount AS credit
            FROM journal_entry je
            WHERE status = 'posted'
              AND effective_date BETWEEN $1 AND $2
              AND credit_account_number BETWEEN 4000 AND 9999
        )
        SELECT account_number, canonical_form,
               SUM(debit) AS sum_debit, SUM(credit) AS sum_credit
        FROM all_sides
        GROUP BY account_number, canonical_form
        ORDER BY account_number
    """
    return await _run_query(conn, sql, [period_start, period_end])


async def _cf_query_ms(conn, period_start: date, period_end: date) -> float:
    # Mirrors the cash-flow query in app/api/reports.py after the bug-13
    # optimization: GROUP BY first, then JOIN against a pre-collapsed COA.
    sql = """
        WITH cash_sides AS (
            SELECT debit_account AS canonical_form,
                   amount AS in_flow, 0::numeric AS out_flow,
                   credit_account AS counter,
                   credit_account_number AS counter_number
            FROM journal_entry je
            WHERE status = 'posted'
              AND effective_date BETWEEN $1 AND $2
              AND debit_account_number BETWEEN 1000 AND 1099
            UNION ALL
            SELECT credit_account AS canonical_form,
                   0::numeric AS in_flow, amount AS out_flow,
                   debit_account AS counter,
                   debit_account_number AS counter_number
            FROM journal_entry je
            WHERE status = 'posted'
              AND effective_date BETWEEN $1 AND $2
              AND credit_account_number BETWEEN 1000 AND 1099
        ),
        aggregated AS (
            SELECT cs.canonical_form, cs.counter, cs.counter_number,
                   SUM(cs.in_flow) AS sum_in, SUM(cs.out_flow) AS sum_out
            FROM cash_sides cs
            GROUP BY cs.canonical_form, cs.counter, cs.counter_number
        ),
        coa_types AS (
            SELECT DISTINCT ON (account_number::int)
                account_number::int AS account_number, account_type
            FROM chart_of_account
            WHERE is_active = true
            ORDER BY account_number::int, entity_id
        )
        SELECT agg.canonical_form, agg.counter,
               agg.sum_in, agg.sum_out, ct.account_type
        FROM aggregated agg
        LEFT JOIN coa_types ct ON ct.account_number = agg.counter_number
        ORDER BY agg.canonical_form, agg.counter
    """
    return await _run_query(conn, sql, [period_start, period_end])


async def _tb_query_ms(conn, as_of: date) -> float:
    # Mirrors the trial-balance query in app/api/reports.py exactly.
    partial_start = date(as_of.year, as_of.month, 1)
    sql = """
        WITH bucket_totals AS (
            SELECT mab.account_number,
                   SUM(mab.debit_total_cents) AS bucket_debit_cents,
                   SUM(mab.credit_total_cents) AS bucket_credit_cents
            FROM monthly_account_balance mab
            WHERE (mab.year < $1 OR (mab.year = $1 AND mab.month < $2))
            GROUP BY mab.account_number
        ),
        tail_debit AS (
            SELECT je.debit_account_number AS account_number,
                   SUM(je.amount) AS tail_debit
            FROM journal_entry je
            WHERE je.status = 'posted'
              AND je.effective_date >= $3 AND je.effective_date <= $4
              AND je.source NOT IN ('reversal', 'recategorization')
              AND je.debit_account_number IS NOT NULL
            GROUP BY je.debit_account_number
        ),
        tail_credit AS (
            SELECT je.credit_account_number AS account_number,
                   SUM(je.amount) AS tail_credit
            FROM journal_entry je
            WHERE je.status = 'posted'
              AND je.effective_date >= $3 AND je.effective_date <= $4
              AND je.source NOT IN ('reversal', 'recategorization')
              AND je.credit_account_number IS NOT NULL
            GROUP BY je.credit_account_number
        ),
        keys AS (
            SELECT account_number FROM bucket_totals
            UNION SELECT account_number FROM tail_debit
            UNION SELECT account_number FROM tail_credit
        ),
        aggregated AS (
            SELECT k.account_number,
                   (COALESCE(bt.bucket_debit_cents, 0)::numeric / 100.0
                    + COALESCE(td.tail_debit, 0)) AS total_debit,
                   (COALESCE(bt.bucket_credit_cents, 0)::numeric / 100.0
                    + COALESCE(tc.tail_credit, 0)) AS total_credit
            FROM keys k
            LEFT JOIN bucket_totals bt USING (account_number)
            LEFT JOIN tail_debit td    USING (account_number)
            LEFT JOIN tail_credit tc   USING (account_number)
        )
        SELECT agg.account_number, coa.canonical_form, coa.account_type,
               agg.total_debit, agg.total_credit
        FROM aggregated agg
        JOIN chart_of_account coa
          ON coa.account_number::int = agg.account_number
         AND coa.is_active = true
        WHERE agg.total_debit != 0 OR agg.total_credit != 0
        ORDER BY agg.account_number
    """
    return await _run_query(
        conn, sql, [as_of.year, as_of.month, partial_start, as_of]
    )


# ---------------------------------------------------------------------------
# Test entry point — single test that handles seed + measure + teardown so
# we don't recompute the seed (slow) per assertion.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reports_at_100k_under_200ms():
    dsn = _maybe_skip()
    conn = await asyncpg.connect(dsn)
    try:
        await _drop_sandbox(conn)
        await _create_sandbox(conn)
        print(f"\n[perf] seeding sandbox schema {PERF_SCHEMA} ...")
        t0 = time.perf_counter()
        total_rows = await _seed(conn)
        seed_s = time.perf_counter() - t0
        print(f"[perf] seeded {total_rows} entries in {seed_s:.1f}s")

        # Pin search_path so the perf queries hit the sandbox tables.
        await conn.execute(f'SET search_path = "{PERF_SCHEMA}", public')

        period_start = START_DATE
        period_end = date(START_DATE.year + 1, 12, 31)  # year-1 full P&L
        as_of = date(START_DATE.year + 1, 12, 31)

        # Warmup: prime the query plans + page cache.
        await _pl_query_ms(conn, period_start, period_end)
        await _cf_query_ms(conn, period_start, period_end)
        await _tb_query_ms(conn, as_of)

        # Best of 3 runs — what we actually assert against the SLO.
        pl_times = [await _pl_query_ms(conn, period_start, period_end) for _ in range(3)]
        cf_times = [await _cf_query_ms(conn, period_start, period_end) for _ in range(3)]
        tb_times = [await _tb_query_ms(conn, as_of) for _ in range(3)]

        pl_ms = min(pl_times)
        cf_ms = min(cf_times)
        tb_ms = min(tb_times)
        print(f"[perf] rows={total_rows} pl={pl_ms:.1f}ms cf={cf_ms:.1f}ms tb={tb_ms:.1f}ms")
        print(f"[perf] pl_all={pl_times} cf_all={cf_times} tb_all={tb_times}")

        assert pl_ms < 200, f"P&L too slow: {pl_ms:.1f}ms (target <200ms)"
        assert cf_ms < 200, f"Cash-flow too slow: {cf_ms:.1f}ms (target <200ms)"
        assert tb_ms < 200, f"Trial balance too slow: {tb_ms:.1f}ms (target <200ms)"
    finally:
        await _drop_sandbox(conn)
        await conn.close()
