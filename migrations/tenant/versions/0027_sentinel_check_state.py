"""per-workspace schema: sentinel_check_state (per-check cadence bookkeeping)

Revision ID: 0027_sentinel_check_state
Revises: 0026_force_rls_shared
Create Date: 2026-07-20

Why
===
``agents/sentinel/config.py`` has always declared a per-check cadence
(``CHECK_CADENCE``: daily / weekly / monthly) and ``sentinel_config``
has always carried an editable ``check_interval_minutes`` — but NEITHER was
enforced. ``run_workspace_cycle`` ran every enabled check on every tick of one
GLOBAL env interval (``SENTINEL_DEFAULT_INTERVAL_MINUTES``, default 360), so a
check labelled "monthly" actually ran 4x a day and the per-workspace interval
in the settings UI did nothing at all.

Enforcing cadence needs durable per-(workspace, check) last-run state:
in-memory bookkeeping would reset on every deploy/restart of the sentinel
container, which would re-fire every monthly check on each restart and quietly
reintroduce the bug. ``sentinel_check_run`` (the per-run audit log) cannot
serve this role — it is append-only, unbounded, and answering "when did check X
last SUCCEED for workspace W" from it is a growing MAX() scan on every cycle.
This table is the small, indexed, one-row-per-check projection.

Table
=====
  sentinel_check_state   PK (workspace_id, check_type) — one row per check per
                         workspace. Upserted at the END of each check run.

    last_run_at            When the check last EXECUTED (success or failure).
                           The cadence gate reads this. Failures count as runs
                           so a persistently crashing check cannot spin at
                           every-cycle frequency (it stays on its cadence and
                           surfaces via consecutive_failures instead).
    last_status            'completed' | 'failed'.
    last_cycle_id          The cycle_id this check last participated in — joins
                           back to sentinel_check_run for the full detail.
    last_findings_count    Findings the last run produced (cheap UI rollup).
    consecutive_failures   Reset to 0 on success, incremented on failure. A
                           non-zero value is the "this check is broken" signal.

A row's ABSENCE means "never run" — which the cadence gate treats as DUE. That
is deliberate: a freshly provisioned workspace (or a newly added check_type on
an existing workspace) runs its full set on the first cycle instead of waiting
up to a month for the monthly checks.

Grants / RLS / TRUNCATE posture (mirrors 0024 / 0023 / 0022 and the 0026 FORCE
consolidation)
==============================================================================
  - SELECT, INSERT, UPDATE -> accounting_ops_app. NO DELETE: state rows are
    upserted in place, never removed.
  - DELETE/TRUNCATE explicitly REVOKEd + the BEFORE TRUNCATE guard trigger
    (``forbid_append_only_truncate()``, created in 0005).
  - RLS ENABLE **and FORCE** with the per-command, PUBLIC-targeted policies
    keyed on ``public.current_workspace_id()`` — the 0026 shape, NOT the older
    single FOR ALL ``workspace_isolation`` policy. A table created after 0026
    must harden itself, because 0026 only swept the tables that existed when it
    ran. Per-command policies (not FOR ALL) also avoid the FORCE + ON CONFLICT
    footgun documented in 0026 — which matters here specifically, since the
    runtime write path for this table IS an ``INSERT ... ON CONFLICT DO UPDATE``.

Idempotent + re-runnable: every DDL is guarded, so this applies cleanly to a
NEW workspace schema at provision time AND to EXISTING schemas via
upgrade_all_workspaces.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0027_sentinel_check_state"
down_revision: str | None = "0026_force_rls_shared"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
CHECK_STATE = "sentinel_check_state"

# Same predicate 0026 installs on every workspace-scoped table.
_PRED = "workspace_id = public.current_workspace_id()"


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


def _harden(schema: str, table: str) -> None:
    """Least-privilege grants + TRUNCATE guard + FORCE RLS (0026 shape)."""
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{table} TO {RUNTIME_ROLE}')
    op.execute(f'REVOKE DELETE, TRUNCATE ON "{schema}".{table} FROM {RUNTIME_ROLE}')

    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate "
        f"BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # Drop the legacy combined policy name and any prior run of ours.
    op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
    for suffix in ("sel", "ins", "upd", "del"):
        op.execute(f"DROP POLICY IF EXISTS {table}_ws_{suffix} ON {table}")

    op.execute(f"CREATE POLICY {table}_ws_sel ON {table} FOR SELECT USING ({_PRED})")
    op.execute(f"CREATE POLICY {table}_ws_ins ON {table} FOR INSERT WITH CHECK ({_PRED})")
    op.execute(
        f"CREATE POLICY {table}_ws_upd ON {table} FOR UPDATE "
        f"USING ({_PRED}) WITH CHECK ({_PRED})"
    )
    op.execute(f"CREATE POLICY {table}_ws_del ON {table} FOR DELETE USING ({_PRED})")


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0027 requires -x tenant=<schema>")

    if not _table_exists(schema, CHECK_STATE):
        op.create_table(
            CHECK_STATE,
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column(
                "check_type", sa.String(length=64), nullable=False,
                comment="Matches agents.sentinel.config.ALL_CHECK_TYPES.",
            ),
            sa.Column(
                "last_run_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
                comment=(
                    "When this check last EXECUTED (success OR failure). The "
                    "cadence gate in agents/sentinel/cadence.py reads this. "
                    "Row absent == never run == always due (first-cycle rule)."
                ),
            ),
            sa.Column(
                "last_status", sa.String(length=16), nullable=False,
                server_default=sa.text("'completed'"),
                comment="'completed' | 'failed'.",
            ),
            sa.Column("last_cycle_id", sa.UUID(), nullable=True),
            sa.Column(
                "last_findings_count", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "consecutive_failures", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
                comment="Reset to 0 on success; incremented on failure.",
            ),
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("workspace_id", "check_type"),
            sa.CheckConstraint(
                "last_status IN ('completed','failed')",
                name="ck_sentinel_check_state_status",
            ),
            comment=(
                "Per-(workspace, check_type) last-run bookkeeping that makes "
                "CHECK_CADENCE enforceable across restarts. One row per check."
            ),
        )

    # PK already covers (workspace_id, check_type); this index serves the
    # cadence read, which loads every row for one workspace at cycle start.
    if not _index_exists(schema, "ix_sentinel_check_state_workspace"):
        op.create_index(
            "ix_sentinel_check_state_workspace", CHECK_STATE, ["workspace_id"]
        )

    _harden(schema, CHECK_STATE)


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0027 requires -x tenant=<schema>")

    for suffix in ("sel", "ins", "upd", "del"):
        op.execute(f"DROP POLICY IF EXISTS {CHECK_STATE}_ws_{suffix} ON {CHECK_STATE}")
    op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {CHECK_STATE}")
    op.execute(f"ALTER TABLE {CHECK_STATE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {CHECK_STATE} DISABLE ROW LEVEL SECURITY")
    op.execute(f"DROP TRIGGER IF EXISTS {CHECK_STATE}_no_truncate ON {CHECK_STATE}")
    if _table_exists(schema, CHECK_STATE):
        op.drop_table(CHECK_STATE)
