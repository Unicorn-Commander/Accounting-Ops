"""per-workspace schema: least-privilege runtime grants + RLS + TRUNCATE guards

Revision ID: 0005_rls_and_runtime_grants
Revises: 0004_journal_relax_fks
Create Date: 2026-05-26

Audit finding C3 — make the DATABASE enforce what the app only promised:

  1. Least-privilege runtime role (accounting_ops_app)
     - The role is created here (idempotent DO block) with LOGIN but NO
       password and NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOBYPASSRLS, so it
       cannot connect until the deploy sets its password out-of-band
       (ALTER ROLE ... PASSWORD '...'). Secrets never live in the repo.
     - It is NOT the owner of any object (the owner stays `accounting_ops`,
       used only by migrations + provisioning), so it cannot ALTER/DISABLE
       triggers, TRUNCATE, or change table structure.
     - Grants in THIS schema:
         * SELECT, INSERT only           -> journal_entry, event_log,
                                            posting_log, decision_audit,
                                            activity_event   (append-only)
         * SELECT, INSERT, UPDATE, DELETE-> category_override
         * SELECT, INSERT, UPDATE        -> everything else mutable
       So the runtime role canNOT UPDATE or DELETE the ledger tables.
     - ALTER DEFAULT PRIVILEGES so future tables the owner creates in this
       schema auto-grant SELECT, INSERT to the runtime role (forward-safe).

  2. BEFORE TRUNCATE statement-level guards on journal_entry + event_log.
     Row-level triggers do NOT fire on TRUNCATE, so the existing
     forbid_*_mutation row triggers don't cover it. These RAISE. (TRUNCATE is
     also not granted to the runtime role; the trigger blocks the owner too,
     which is the genuine attack surface — see safety-invariant tests.)

  3. Row-Level Security backstop on every workspace-scoped table (all tenant
     tables carry workspace_id). Policy: workspace_id = the
     `app.current_workspace` GUC the app sets per request
     (app/workspace_session.py). Tables are FORCE ROW LEVEL SECURITY so even
     the owner is subject to RLS, and the runtime role is not BYPASSRLS.
     Because each schema holds exactly one workspace's rows, a query that
     lands in the WRONG schema (stale/missing search_path) sees ZERO rows
     instead of another tenant's data.

This migration is idempotent and re-runnable: it is applied to every NEW
workspace schema at provision time and to EXISTING schemas via
upgrade_all_workspaces. It runs as the OWNER role with search_path set to the
target schema (env.py + the belt-and-suspenders SET below).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0005_rls_and_runtime_grants"
down_revision: str | None = "0004_journal_relax_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The least-privilege runtime role. Cluster-level (shared across DBs); created
# idempotently. Keep in lockstep with app/config.py + DEPLOY.md.
RUNTIME_ROLE = "accounting_ops_app"

# Append-only tables: SELECT + INSERT only (no UPDATE/DELETE), TRUNCATE guarded.
APPEND_ONLY_TABLES = (
    "journal_entry",
    "event_log",
    "posting_log",
    "decision_audit",
    "activity_event",
)

# Statement-level TRUNCATE guards required by the audit (the ledger + its
# event stream). posting_log/decision_audit/activity_event are also
# SELECT+INSERT-only by grant, but the hard immutability guard is the ledger.
TRUNCATE_GUARDED_TABLES = (
    "journal_entry",
    "event_log",
)

# Fully mutable: app does upsert + delete.
RW_DELETE_TABLES = (
    "category_override",
)

# Mutable working tables: SELECT + INSERT + UPDATE (no DELETE). Everything in
# the schema that is not append-only and not in RW_DELETE_TABLES.
RW_NO_DELETE_TABLES = (
    "entity",
    "vendor",
    "counterparty",
    "receipt",
    "compliance_profile",
    "compliance_rule",
    "approval_queue",
    "connector_state",
    "agent_session",
    "fx_rate",
    "recon_link",
    "account_entity_mapping",
    "chart_of_account",
)

# Every workspace-scoped table gets RLS (all carry a workspace_id column).
ALL_TABLES = (
    APPEND_ONLY_TABLES + RW_DELETE_TABLES + RW_NO_DELETE_TABLES
)


def _set_search_path() -> str:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
    return schema


def _owner_role() -> str:
    """The role that owns objects in this schema (= the migration's role)."""
    bind = op.get_bind()
    return bind.execute(sa.text("SELECT current_user")).scalar_one()


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0005 requires -x tenant=<schema>")

    # ----------------------------------------------------------------------
    # 1. Ensure the runtime role exists (cluster-level, idempotent).
    #    LOGIN but NO password -> cannot connect until deploy sets it.
    #    Never BYPASSRLS; never superuser/createdb/createrole.
    # ----------------------------------------------------------------------
    op.execute(
        f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
            CREATE ROLE {RUNTIME_ROLE}
              LOGIN
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
          ELSE
            -- Re-assert the safe attributes in case the role drifted.
            ALTER ROLE {RUNTIME_ROLE}
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
          END IF;
        END
        $$;
        """
    )

    # Connect + schema usage. CONNECT is on the database (granted once in the
    # public migration); USAGE on this schema is granted here per-schema.
    op.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {RUNTIME_ROLE}')

    # ----------------------------------------------------------------------
    # 2. Per-table privileges (least privilege).
    # ----------------------------------------------------------------------
    for tbl in APPEND_ONLY_TABLES:
        op.execute(f'GRANT SELECT, INSERT ON "{schema}".{tbl} TO {RUNTIME_ROLE}')
        # Defense in depth: make absolutely sure no UPDATE/DELETE/TRUNCATE leaked
        # in (e.g. from a prior PUBLIC grant) for the append-only set.
        op.execute(
            f'REVOKE UPDATE, DELETE, TRUNCATE ON "{schema}".{tbl} FROM {RUNTIME_ROLE}'
        )

    for tbl in RW_DELETE_TABLES:
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{schema}".{tbl} TO {RUNTIME_ROLE}'
        )

    for tbl in RW_NO_DELETE_TABLES:
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{tbl} TO {RUNTIME_ROLE}'
        )

    # Any sequences in this schema (none today — all PKs are gen_random_uuid()
    # — but defensive for future SERIAL/IDENTITY columns).
    op.execute(
        f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO {RUNTIME_ROLE}'
    )

    # Forward-safe defaults: future tables the OWNER creates in this schema
    # auto-grant SELECT, INSERT to the runtime role. (UPDATE is added per-table
    # by the migration that introduces a mutable table, keeping append-only the
    # safe default.) FOR ROLE names the creating role so the default applies to
    # objects it creates.
    owner = _owner_role()
    op.execute(
        f'ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA "{schema}" '
        f"GRANT SELECT, INSERT ON TABLES TO {RUNTIME_ROLE}"
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA "{schema}" '
        f"GRANT USAGE, SELECT ON SEQUENCES TO {RUNTIME_ROLE}"
    )

    # ----------------------------------------------------------------------
    # 3. BEFORE TRUNCATE guards (statement-level; row triggers skip TRUNCATE).
    # ----------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION forbid_append_only_truncate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only; TRUNCATE is forbidden', TG_TABLE_NAME;
        END;
        $$;
        """
    )
    for tbl in TRUNCATE_GUARDED_TABLES:
        # Idempotent: drop-if-exists then create.
        op.execute(f"DROP TRIGGER IF EXISTS {tbl}_no_truncate ON {tbl}")
        op.execute(
            f"CREATE TRIGGER {tbl}_no_truncate "
            f"BEFORE TRUNCATE ON {tbl} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
        )

    # ----------------------------------------------------------------------
    # 4. Row-Level Security backstop, keyed on the app.current_workspace GUC.
    # ----------------------------------------------------------------------
    # NOTE on FORCE: we deliberately ENABLE but do NOT FORCE row level security.
    #   - The RUNTIME role (accounting_ops_app) is NOT the table owner and is
    #     NOT BYPASSRLS, so RLS is fully enforced against it — that IS the
    #     security boundary the audit requires.
    #   - The OWNER role (accounting_ops) bypasses RLS by default. We keep it
    #     that way ON PURPOSE: the owner is used only by migrations,
    #     provisioning, and operator maintenance/backup, all of which
    #     legitimately need full cross-workspace visibility. FORCE here would be
    #     the footgun — it would silently make owner-run maintenance + backups
    #     return zero rows unless they remembered to set app.current_workspace.
    for tbl in ALL_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {tbl}")
        # NULLIF(...,'') coerces an unset OR empty GUC to NULL; NULL = workspace_id
        # is NULL (never true) => default-deny when the GUC isn't set. The cast to
        # uuid matches the workspace_id column type.
        op.execute(
            f"""
            CREATE POLICY workspace_isolation ON {tbl}
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
        raise RuntimeError("0005 requires -x tenant=<schema>")

    for tbl in ALL_TABLES:
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")

    for tbl in TRUNCATE_GUARDED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {tbl}_no_truncate ON {tbl}")
    op.execute("DROP FUNCTION IF EXISTS forbid_append_only_truncate()")

    owner = _owner_role()
    op.execute(
        f'ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA "{schema}" '
        f"REVOKE SELECT, INSERT ON TABLES FROM {RUNTIME_ROLE}"
    )
    op.execute(
        f'ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA "{schema}" '
        f"REVOKE USAGE, SELECT ON SEQUENCES FROM {RUNTIME_ROLE}"
    )
    op.execute(
        f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema}" FROM {RUNTIME_ROLE}'
    )
    op.execute(
        f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA "{schema}" FROM {RUNTIME_ROLE}'
    )
    op.execute(f'REVOKE USAGE ON SCHEMA "{schema}" FROM {RUNTIME_ROLE}')
    # Intentionally do NOT DROP ROLE here: it is cluster-level and may own
    # grants in other workspace schemas. Role lifecycle is a deploy concern.
