"""public schema: least-privilege runtime grants + credential RLS + TRUNCATE guard

Revision ID: 0003_rls_and_runtime_grants
Revises: 0002_credentials
Create Date: 2026-05-26

Audit finding C3 (public-schema half). Mirrors tenant/0005 for the public
registry + credential tables:

  1. Ensure the least-privilege runtime role exists (idempotent; LOGIN, NO
     password, never BYPASSRLS) and can connect:
       GRANT CONNECT ON DATABASE <db> + USAGE ON SCHEMA public.

  2. Least-privilege table grants:
       * registry the app reads/writes at runtime:
           user_account            SELECT, INSERT, UPDATE   (auto-provision on
                                                             first login)
           workspace_membership    SELECT, INSERT, UPDATE
           agent_identity          SELECT, INSERT, UPDATE
           workspace               SELECT                   (mutated only by
                                                             provisioning = owner)
       * tenant_credentials        SELECT, INSERT, UPDATE, DELETE  (store upsert,
                                                             last_used bump, delete)
       * credential_access_log     SELECT, INSERT only      (append-only)

  3. credential_access_log: BEFORE TRUNCATE guard (row trigger from 0002 does
     not fire on TRUNCATE). TRUNCATE is also not granted to the runtime role.

  4. RLS on the two WORKSPACE-SCOPED credential tables (tenant_credentials,
     credential_access_log), keyed on the app.current_workspace GUC. The
     credential code paths set this GUC (app/security/credentials.py). The
     registry tables (workspace/user_account/workspace_membership/
     agent_identity) are intentionally NOT under RLS: they are cross-workspace
     by nature (resolve_workspace looks a user up across workspaces) and are
     accessed on a bare public session without the GUC.

Runs ONCE against the database, as the OWNER role (alembic-public.ini).
Idempotent + re-runnable.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_rls_and_runtime_grants"
down_revision: str | None = "0002_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"

# Workspace-scoped public tables that get RLS (carry workspace_id).
RLS_TABLES = ("tenant_credentials", "credential_access_log")


def upgrade() -> None:
    bind = op.get_bind()
    dbname = bind.execute(sa.text("SELECT current_database()")).scalar_one()

    # ----------------------------------------------------------------------
    # 1. Ensure runtime role (idempotent). LOGIN, NO password -> cannot connect
    #    until deploy sets it. Never BYPASSRLS / superuser / createdb /createrole.
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
            ALTER ROLE {RUNTIME_ROLE}
              NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
          END IF;
        END
        $$;
        """
    )
    op.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO {RUNTIME_ROLE}')
    op.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")

    # ----------------------------------------------------------------------
    # 2. Least-privilege table grants.
    # ----------------------------------------------------------------------
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON public.user_account TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON public.workspace_membership TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON public.agent_identity TO {RUNTIME_ROLE}")
    op.execute(f"GRANT SELECT ON public.workspace TO {RUNTIME_ROLE}")

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON public.tenant_credentials TO {RUNTIME_ROLE}"
    )
    # Append-only access log: SELECT + INSERT only, and explicitly no U/D/TRUNCATE.
    op.execute(f"GRANT SELECT, INSERT ON public.credential_access_log TO {RUNTIME_ROLE}")
    op.execute(
        f"REVOKE UPDATE, DELETE, TRUNCATE ON public.credential_access_log FROM {RUNTIME_ROLE}"
    )

    # Sequences (defensive; PKs are gen_random_uuid()).
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE}")

    # ----------------------------------------------------------------------
    # 3. credential_access_log BEFORE TRUNCATE guard.
    # ----------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.forbid_credential_access_log_truncate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'credential_access_log is append-only; TRUNCATE is forbidden';
        END;
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS credential_access_log_no_truncate ON public.credential_access_log"
    )
    op.execute(
        "CREATE TRIGGER credential_access_log_no_truncate "
        "BEFORE TRUNCATE ON public.credential_access_log "
        "FOR EACH STATEMENT EXECUTE FUNCTION public.forbid_credential_access_log_truncate()"
    )

    # ----------------------------------------------------------------------
    # 4. RLS on the workspace-scoped credential tables.
    # ----------------------------------------------------------------------
    # ENABLE but not FORCE — same rationale as tenant/0005: the runtime role is
    # a non-owner, non-BYPASSRLS role so RLS is enforced against it; the owner
    # keeps full visibility for maintenance/backup.
    for tbl in RLS_TABLES:
        op.execute(f"ALTER TABLE public.{tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON public.{tbl}")
        op.execute(
            f"""
            CREATE POLICY workspace_isolation ON public.{tbl}
              USING (
                workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
              )
              WITH CHECK (
                workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid
              )
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    dbname = bind.execute(sa.text("SELECT current_database()")).scalar_one()

    for tbl in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON public.{tbl}")
        op.execute(f"ALTER TABLE public.{tbl} DISABLE ROW LEVEL SECURITY")

    op.execute(
        "DROP TRIGGER IF EXISTS credential_access_log_no_truncate ON public.credential_access_log"
    )
    op.execute("DROP FUNCTION IF EXISTS public.forbid_credential_access_log_truncate()")

    for tbl in (
        "user_account",
        "workspace_membership",
        "agent_identity",
        "workspace",
        "tenant_credentials",
        "credential_access_log",
    ):
        op.execute(f"REVOKE ALL ON public.{tbl} FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {RUNTIME_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {RUNTIME_ROLE}")
    op.execute(f'REVOKE CONNECT ON DATABASE "{dbname}" FROM {RUNTIME_ROLE}')
    # Role is cluster-level; do not DROP here.
