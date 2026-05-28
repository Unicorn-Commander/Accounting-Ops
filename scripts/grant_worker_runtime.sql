-- grant_worker_runtime.sql
--
-- PURPOSE
-- -------
-- Grant the least-privilege runtime role (accounting_ops_app) exactly the
-- privileges it needs on the worker-managed public tables.
--
-- WHY THIS IS NOT IN ALEMBIC
-- --------------------------
-- The worker tables (worker_runs, worker_extractions, worker_dedup_index,
-- worker_reconcile_runs, receipt_transaction_link) are created by the SIBLING
-- repos' own Alembic migrations (receipt-worker 0001_initial,
-- reconciler 0001_reconciler), not by this main-repo's migrations. Alembic
-- provides no mechanism to express cross-repo migration ordering, so a GRANT
-- inside main-repo public/0004 runs BEFORE the sibling tables exist on a fresh
-- stack, making it a no-op. Because Alembic never re-runs a stamped revision,
-- those grants are silently skipped and accounting_ops_app ends up with zero
-- privileges on worker tables. This script lives outside Alembic precisely to
-- break that ordering dependency.
--
-- WHEN TO RUN
-- -----------
-- Run this AFTER both sibling repos' migrations have created their tables, NOT
-- before. If you run it before a table exists the DO block for that table will
-- be skipped (see IF EXISTS guards below) — simply re-run the script once the
-- table exists. The script is fully idempotent: GRANT is idempotent in Postgres
-- and re-running it on an already-granted role is harmless.
--
-- HOW TO RUN
-- ----------
-- From the midboy1 deploy host (as documented in DEPLOY.md Step 4):
--
--   docker compose exec postgres psql -U accounting_ops -d accounting_ops \
--     -f /scripts/grant_worker_runtime.sql
--
-- VERIFICATION (run after to confirm)
-- ------------
-- Positive — these must succeed:
--   SET ROLE accounting_ops_app;
--   SELECT 1 FROM worker_runs LIMIT 0;
--   INSERT INTO worker_runs ... (synthetic row — then rollback);
--
-- Negative — each of these must ERROR with "permission denied":
--   SET ROLE accounting_ops_app;
--   DELETE FROM worker_runs WHERE false;
--   TRUNCATE worker_runs;
--   DELETE FROM worker_extractions WHERE false;
--   TRUNCATE worker_extractions;
--   DELETE FROM worker_dedup_index WHERE false;
--   TRUNCATE worker_dedup_index;
--   DELETE FROM worker_reconcile_runs WHERE false;
--   TRUNCATE worker_reconcile_runs;
--   DELETE FROM receipt_transaction_link WHERE false;
--   TRUNCATE receipt_transaction_link;

-- ---------------------------------------------------------------------------
-- worker_runs  — receipt-worker: INSERT new run, UPDATE counters/ended_at,
--                SELECT for status surface (last_runs, errors_24h)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.worker_runs') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.worker_runs TO accounting_ops_app;
        REVOKE DELETE, TRUNCATE ON public.worker_runs FROM accounting_ops_app;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- worker_extractions  — receipt-worker: INSERT extracted receipt row, SELECT
--                       for dedup reads; reconciler adds UPDATE for match status
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.worker_extractions') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.worker_extractions TO accounting_ops_app;
        REVOKE DELETE, TRUNCATE ON public.worker_extractions FROM accounting_ops_app;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- worker_dedup_index  — receipt-worker: INSERT content-hash dedup ledger row,
--                       SELECT for find_by_content_hash / find_by_fingerprint_hash
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.worker_dedup_index') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.worker_dedup_index TO accounting_ops_app;
        REVOKE DELETE, TRUNCATE ON public.worker_dedup_index FROM accounting_ops_app;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- worker_reconcile_runs  — reconciler: INSERT new run, UPDATE run, SELECT status
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.worker_reconcile_runs') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE ON public.worker_reconcile_runs TO accounting_ops_app;
        REVOKE DELETE, TRUNCATE ON public.worker_reconcile_runs FROM accounting_ops_app;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- receipt_transaction_link  — reconciler: INSERT link row, SELECT read-back/status
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('public.receipt_transaction_link') IS NOT NULL THEN
        GRANT SELECT, INSERT ON public.receipt_transaction_link TO accounting_ops_app;
        REVOKE DELETE, TRUNCATE ON public.receipt_transaction_link FROM accounting_ops_app;
    END IF;
END $$;
