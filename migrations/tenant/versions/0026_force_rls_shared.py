"""shared-schema consolidation, part 2: FORCE RLS + per-command isolation policies

Revision ID: 0026_force_rls_shared
Revises: 0025_prep_shared_uniques
Create Date: 2026-07-07

Part 2 of 2 (see docs/TENANCY-CONSOLIDATION-RUNBOOK.md and 0025). Applied AFTER
the data-copy so the owner-run copy is not gated by FORCE.

What it does, for EVERY workspace-scoped table in the shared schema (every base
table that carries a workspace_id column — i.e. all of them except
alembic_version, discovered dynamically so this never drifts from the model):

  1. ENABLE + FORCE ROW LEVEL SECURITY.
     - FORCE makes RLS apply to the table OWNER too (not just the runtime role).
       Because the owner/admin role is NOSUPERUSER + NOBYPASSRLS, a query that
       forgets to `SET app.current_workspace` sees ZERO rows regardless of which
       role runs it. This closes the former owner-bypass path (0005 kept RLS
       ENABLE-not-FORCE precisely so the owner bypassed; consolidation reverses
       that trade now that there is one shared schema). Operator/maintenance and
       any owner-role service (cpa-export, provisioning) MUST set the GUC (per
       workspace) to read/write rows — this is the documented new contract.

  2. Replace the single FOR ALL `workspace_isolation` policy with per-command,
     PUBLIC-targeted policies keyed on public.current_workspace_id() (mirrors
     the Contact-Ops 0015 suite-canonical shape). Splitting FOR ALL into
     per-command avoids the FORCE + ON CONFLICT / INSERT ... RETURNING footguns
     (a FOR ALL USING clause is wrongly applied to INSERT paths under FORCE).

Command AUTHORITY (who may UPDATE/DELETE which table) is unchanged — it is
enforced by the GRANTs from 0005 (append-only tables have no UPDATE/DELETE grant
to the runtime role). These policies enforce workspace ISOLATION per command;
having an UPDATE/DELETE policy present on an append-only table is harmless
because the missing grant rejects the command first.

Idempotent + re-runnable. Runs as OWNER with search_path pinned to the target
schema by migrations/tenant/env.py.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_force_rls_shared"
down_revision: str | None = "0025_prep_shared_uniques"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRED = "workspace_id = public.current_workspace_id()"


def _scoped_tables(bind) -> list[str]:
    """Every base table in the current schema that carries a workspace_id column."""
    schema = bind.execute(sa.text("SELECT current_schema()")).scalar_one()
    rows = bind.execute(
        sa.text(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
                 AND a.attname = 'workspace_id' AND NOT a.attisdropped
            WHERE n.nspname = :schema AND c.relkind = 'r'
            ORDER BY c.relname
            """
        ),
        {"schema": schema},
    ).all()
    return [r[0] for r in rows]


def upgrade() -> None:
    bind = op.get_bind()
    tables = _scoped_tables(bind)
    if not tables:
        raise RuntimeError(
            "0026 found no workspace_id-scoped tables in the target schema; "
            "check -x tenant=<schema> points at the consolidated schema."
        )

    for tbl in tables:
        op.execute(f'ALTER TABLE "{tbl}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{tbl}" FORCE ROW LEVEL SECURITY')

        # Drop the legacy FOR ALL policy and any prior run of ours.
        op.execute(f'DROP POLICY IF EXISTS workspace_isolation ON "{tbl}"')
        for suffix in ("sel", "ins", "upd", "del"):
            op.execute(f'DROP POLICY IF EXISTS {tbl}_ws_{suffix} ON "{tbl}"')

        # Per-command, PUBLIC-targeted isolation policies.
        op.execute(
            f'CREATE POLICY {tbl}_ws_sel ON "{tbl}" FOR SELECT USING ({_PRED})'
        )
        op.execute(
            f'CREATE POLICY {tbl}_ws_ins ON "{tbl}" FOR INSERT WITH CHECK ({_PRED})'
        )
        op.execute(
            f'CREATE POLICY {tbl}_ws_upd ON "{tbl}" FOR UPDATE '
            f"USING ({_PRED}) WITH CHECK ({_PRED})"
        )
        op.execute(
            f'CREATE POLICY {tbl}_ws_del ON "{tbl}" FOR DELETE USING ({_PRED})'
        )


def downgrade() -> None:
    bind = op.get_bind()
    for tbl in _scoped_tables(bind):
        for suffix in ("sel", "ins", "upd", "del"):
            op.execute(f'DROP POLICY IF EXISTS {tbl}_ws_{suffix} ON "{tbl}"')
        op.execute(f'ALTER TABLE "{tbl}" NO FORCE ROW LEVEL SECURITY')
        # Restore the legacy combined policy so the pre-consolidation behavior
        # (ENABLE, not FORCE; single FOR ALL) is recovered.
        op.execute(
            f"""
            CREATE POLICY workspace_isolation ON "{tbl}"
              USING (workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid)
              WITH CHECK (workspace_id = NULLIF(current_setting('app.current_workspace', true), '')::uuid)
            """
        )
