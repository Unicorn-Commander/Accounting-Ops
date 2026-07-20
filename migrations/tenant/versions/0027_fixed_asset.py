"""per-workspace schema: fixed_asset (depreciation / Form 4562)

Revision ID: 0027_fixed_asset
Revises: 0026_force_rls_shared
Create Date: 2026-07-20

One tenant table:

  fixed_asset   A depreciable asset on an entity's books. Depreciation is
                inherently multi-year and stateful — an asset placed in service
                in 2023 must still produce the right 2026 deduction, using the
                convention and the §168(k) percentage fixed AT PLACEMENT. Making
                the asset a first-class row (rather than a per-call list) keeps
                that history auditable, lets the §179 carryforward chain year to
                year, and gives every Form 4562 figure a source_ref anchor.
                Entity-scoped.

Grants / RLS posture
====================
This migration lands AFTER 0026 (shared-schema consolidation part 2), which
applied ENABLE + FORCE ROW LEVEL SECURITY and per-command isolation policies to
every workspace_id-scoped table it found AT THAT TIME. 0026 discovers tables
DYNAMICALLY, so it will not retroactively cover a table created later — this
migration must therefore apply the SAME hardening itself, or fixed_asset would
be the one table in the schema without FORCE RLS (an owner-role service such as
cpa-export would then read every workspace's assets).

So, mirroring 0026 exactly:
  - ENABLE + FORCE ROW LEVEL SECURITY.
  - Four per-command, PUBLIC-targeted policies keyed on
    public.current_workspace_id() (named ``fixed_asset_ws_{sel,ins,upd,del}``),
    matching the suite-canonical shape. Per-command rather than a single FOR ALL
    policy, because a FOR ALL USING clause is wrongly applied to INSERT paths
    under FORCE.
  - SELECT, INSERT, UPDATE to the runtime role. Unlike the append-only ledger
    tables, fixed_asset IS mutable (an asset gets disposed, a business-use
    percentage is corrected) — but DELETE is still withheld: removing an asset
    would silently change a prior year's basis history. Dispose it instead
    (``disposed_on``). TRUNCATE is revoked + guarded, like the other tables.

Idempotent + re-runnable: every DDL is guarded, so this applies cleanly to a NEW
workspace schema at provision time AND to EXISTING schemas via
upgrade_all_workspaces.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0027_fixed_asset"
down_revision: str | None = "0026_force_rls_shared"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
FIXED_ASSET = "fixed_asset"

# Same predicate 0026 uses.
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
                "SELECT 1 FROM pg_indexes WHERE schemaname = :schema AND indexname = :index"
            ),
            {"schema": schema, "index": index},
        ).first()
    )


def _harden(schema: str, table: str) -> None:
    """Least-privilege grants + TRUNCATE guard + FORCE RLS (the 0026 shape)."""
    op.execute(f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{table} TO {RUNTIME_ROLE}')
    op.execute(f'REVOKE DELETE, TRUNCATE ON "{schema}".{table} FROM {RUNTIME_ROLE}')
    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # Drop the legacy combined policy (if a pre-0026 schema ever had one) and
    # any prior run of ours, then create the four per-command policies.
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

    if not _table_exists(schema, FIXED_ASSET):
        op.create_table(
            FIXED_ASSET,
            sa.Column(
                "id", sa.UUID(), nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column("description", sa.String(length=300), nullable=False),
            sa.Column("asset_tag", sa.String(length=64), nullable=True),
            sa.Column("placed_in_service", sa.Date(), nullable=False),
            sa.Column(
                "acquired_on", sa.Date(), nullable=True,
                comment="Acquisition (binding contract) date. From TY2025 the "
                        "§168(k) percentage turns on whether this is after the "
                        "OBBBA 2025-01-19 cutoff — the engine raises when null.",
            ),
            sa.Column("cost_cents", sa.BigInteger(), nullable=False),
            sa.Column(
                "recovery_period_years", sa.Numeric(precision=4, scale=1),
                nullable=False,
                comment="GDS/ADS recovery period in years (3, 5, 7, 27.5, 39, …).",
            ),
            sa.Column(
                "depreciation_system", sa.String(length=3), nullable=False,
                server_default=sa.text("'GDS'"), comment="GDS (§168(a)) or ADS (§168(g)).",
            ),
            sa.Column(
                "method", sa.String(length=8), nullable=True,
                comment="NULL = derive per §168(b). Set only to record an election.",
            ),
            sa.Column(
                "convention", sa.String(length=2), nullable=True,
                comment="NULL = derive per §168(d). Set to pin a prior year's convention.",
            ),
            sa.Column(
                "business_use_pct", sa.Numeric(precision=5, scale=2), nullable=False,
                server_default=sa.text("100"),
                comment="§280F qualified business/investment use percentage.",
            ),
            sa.Column(
                "is_listed_property", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "section_179_elected_cents", sa.BigInteger(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "bonus_elected_out", sa.Boolean(), nullable=False,
                server_default=sa.text("false"),
                comment="§168(k)(7) irrevocable election out of the special allowance.",
            ),
            sa.Column(
                "prior_section_179_cents", sa.BigInteger(), nullable=True,
                comment="§179 actually taken, for assets placed in service before "
                        "the engine's supported config window.",
            ),
            sa.Column("prior_bonus_cents", sa.BigInteger(), nullable=True),
            sa.Column("disposed_on", sa.Date(), nullable=True),
            sa.Column("disposal_proceeds_cents", sa.BigInteger(), nullable=True),
            sa.Column(
                "source_ref", sa.String(length=300), nullable=True,
                comment="Provenance anchor carried onto every derived Form 4562 figure.",
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_by_uc_uid", sa.String(length=128), nullable=True),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"],
                name="fixed_asset_entity_fk", ondelete="RESTRICT",
            ),
            sa.CheckConstraint("cost_cents >= 0", name="ck_fixed_asset_cost_nonneg"),
            sa.CheckConstraint(
                "business_use_pct >= 0 AND business_use_pct <= 100",
                name="ck_fixed_asset_business_use_pct",
            ),
            sa.CheckConstraint(
                "recovery_period_years > 0", name="ck_fixed_asset_recovery_positive"
            ),
            sa.CheckConstraint(
                "depreciation_system IN ('GDS','ADS')", name="ck_fixed_asset_system"
            ),
            sa.CheckConstraint(
                "method IS NULL OR method IN ('200DB','150DB','S/L')",
                name="ck_fixed_asset_method",
            ),
            sa.CheckConstraint(
                "convention IS NULL OR convention IN ('HY','MQ','MM')",
                name="ck_fixed_asset_convention",
            ),
            comment=(
                "A depreciable fixed asset. Multi-year state: the convention and "
                "§168(k) percentage are fixed at placement and must survive to "
                "later returns."
            ),
        )

    if not _index_exists(schema, "ix_fixed_asset_workspace"):
        op.create_index("ix_fixed_asset_workspace", FIXED_ASSET, ["workspace_id"])
    if not _index_exists(schema, "ix_fixed_asset_entity_placed"):
        op.create_index(
            "ix_fixed_asset_entity_placed", FIXED_ASSET,
            ["entity_id", "placed_in_service"],
        )

    _harden(schema, FIXED_ASSET)


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0027 requires -x tenant=<schema>")
    for suffix in ("sel", "ins", "upd", "del"):
        op.execute(f"DROP POLICY IF EXISTS {FIXED_ASSET}_ws_{suffix} ON {FIXED_ASSET}")
    op.execute(f"DROP TRIGGER IF EXISTS {FIXED_ASSET}_no_truncate ON {FIXED_ASSET}")
    op.execute("DROP INDEX IF EXISTS ix_fixed_asset_entity_placed")
    op.execute("DROP INDEX IF EXISTS ix_fixed_asset_workspace")
    op.execute(f"DROP TABLE IF EXISTS {FIXED_ASSET}")
