"""per-workspace schema: entity_owner (shareholders / partners)

Revision ID: 0027_entity_owner
Revises: 0026_force_rls_shared
Create Date: 2026-07-20

The ownership layer that Forms 1120-S and 1065 need. Until now the platform
recorded corporate legal facts on ``entity`` (authorized_shares, par_value_cents
— see 0011) but had NO record of WHO owns an entity or in what proportion, so a
flow-through return could compute entity-level income but not issue a single
Schedule K-1.

One tenant table:

  entity_owner   A shareholder (1120-S) or partner/member (1065) of an entity,
                 for a period. Multiple rows per owner are expected: ownership
                 is time-sliced by ``effective_from`` / ``effective_to`` so a
                 mid-year transfer is representable, which is what the
                 §1377(a)(1) per-share-per-day allocation consumes.

WHY BOTH shares AND percentages
-------------------------------
They are not redundant — the two entity types allocate under different regimes:

  * S corporation: ``shares``. §1377(a)(1) requires a pro rata allocation "per
    share, per day", so the actual share COUNT on each day is the input. A
    percentage cannot express a mid-year transfer.
  * Partnership: ``profit_pct`` / ``loss_pct`` / ``capital_pct``. §704(a) puts
    allocation in the partnership agreement, and the three percentages
    routinely differ from one another.

``allocation_basis`` IS THE SAFETY INTERLOCK
--------------------------------------------
A partnership may allocate disproportionately, so pro rata is NOT a safe
default and the derivation refuses to assume it. This column is how a human
affirmatively states what the partnership agreement says:

  'per_share_per_day'              S corp; the statutory method (§1377(a)(1)).
  'partnership_agreement_pro_rata' Attested: the agreement allocates strictly
                                   by percentage. Only this value unlocks an
                                   automatic 1065 K-1 allocation.
  'partnership_agreement_special'  Attested: the agreement makes special
                                   allocations (§704(b)). The derivation
                                   deliberately REFUSES to split items and
                                   demands the CPA's allocation schedule.

NULL means "not recorded" and is treated as a refusal, never as pro rata.

TAX IDs
-------
Only ``tax_id_last4`` is stored. A K-1 needs the owner's full SSN/EIN, but that
belongs in the encrypted-document path alongside the other PII the platform
already handles — not in a table that a reporting sidecar reads. The derivation
emits last4 so a reviewer can identify the owner without this table becoming a
new SSN store.

Grants / RLS / TRUNCATE posture
===============================
  - SELECT, INSERT, UPDATE, DELETE -> accounting_ops_app. Unlike the append-only
    ledger tables, ownership is CORRECTABLE reference data: a typo'd share count
    must be fixable, and an erroneously-added owner must be removable.
  - RLS: ENABLE + FORCE, with the per-command policy set from 0026.

    ⚠️  0026 discovers workspace-scoped tables DYNAMICALLY and has already run,
    so it will NOT retroactively cover a table created afterwards. This
    migration therefore applies the identical policy shape itself. Without this,
    entity_owner would be the one workspace-scoped table in the schema with no
    FORCE-RLS protection — a cross-tenant read of exactly the data (owner names,
    ownership percentages) that is most sensitive.

Idempotent + re-runnable: every DDL is guarded, so this applies cleanly to a NEW
workspace schema at provision time AND to EXISTING schemas via
upgrade_all_workspaces.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0027_entity_owner"
down_revision: str | None = "0026_force_rls_shared"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
ENTITY_OWNER = "entity_owner"

# Same predicate + per-command policy shape as 0026.
_PRED = "workspace_id = public.current_workspace_id()"


def _set_search_path() -> str:
    bind = op.get_bind()
    x_args = context.get_x_argument(as_dictionary=True)
    schema = x_args.get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))
        return schema
    return bind.execute(sa.text("SELECT current_schema()")).scalar_one()


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
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = :schema AND indexname = :index"
            ),
            {"schema": schema, "index": index},
        ).first()
    )


def _harden(schema: str, table: str) -> None:
    """Least-privilege grants + the 0026-shape FORCE-RLS policy set."""
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{schema}".{table} '
        f"TO {RUNTIME_ROLE}"
    )
    op.execute(f'REVOKE TRUNCATE ON "{schema}".{table} FROM {RUNTIME_ROLE}')

    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {table}")
    for suffix in ("sel", "ins", "upd", "del"):
        op.execute(f"DROP POLICY IF EXISTS {table}_ws_{suffix} ON {table}")

    op.execute(f"CREATE POLICY {table}_ws_sel ON {table} FOR SELECT USING ({_PRED})")
    op.execute(
        f"CREATE POLICY {table}_ws_ins ON {table} FOR INSERT WITH CHECK ({_PRED})"
    )
    op.execute(
        f"CREATE POLICY {table}_ws_upd ON {table} FOR UPDATE "
        f"USING ({_PRED}) WITH CHECK ({_PRED})"
    )
    op.execute(f"CREATE POLICY {table}_ws_del ON {table} FOR DELETE USING ({_PRED})")


def upgrade() -> None:
    schema = _set_search_path()

    if not _table_exists(schema, ENTITY_OWNER):
        op.create_table(
            ENTITY_OWNER,
            sa.Column(
                "id", sa.UUID(), nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "owner_name", sa.String(length=300), nullable=False,
                comment="Legal name as it must appear on the Schedule K-1.",
            ),
            sa.Column(
                "owner_type", sa.String(length=24), nullable=False,
                server_default=sa.text("'individual'"),
                comment="individual|entity|trust|estate. An S corp may only "
                        "have eligible shareholders (§1361(b)(1)(B)).",
            ),
            sa.Column(
                "tax_id_last4", sa.String(length=4), nullable=True,
                comment="Last 4 of SSN/EIN only. The full identifier lives in "
                        "the encrypted document path, not here.",
            ),
            # --- period of ownership ---
            sa.Column(
                "effective_from", sa.Date(), nullable=False,
                comment="First day this ownership record is in effect.",
            ),
            sa.Column(
                "effective_to", sa.Date(), nullable=True,
                comment="Last day in effect; NULL = still an owner. Drives the "
                        "§1377(a)(1) per-share-per-day allocation.",
            ),
            # --- S corporation ---
            sa.Column(
                "shares", sa.Numeric(precision=20, scale=4), nullable=True,
                comment="Shares held during this period (S corp). Required for "
                        "the per-share-per-day allocation; a percentage cannot "
                        "express a mid-year transfer.",
            ),
            # --- partnership / LLC ---
            sa.Column(
                "profit_pct", sa.Numeric(precision=9, scale=6), nullable=True,
                comment="Profit-sharing percentage 0..100 (1065).",
            ),
            sa.Column(
                "loss_pct", sa.Numeric(precision=9, scale=6), nullable=True,
                comment="Loss-sharing percentage 0..100; may differ from profit.",
            ),
            sa.Column(
                "capital_pct", sa.Numeric(precision=9, scale=6), nullable=True,
                comment="Capital percentage 0..100; may differ from profit.",
            ),
            sa.Column(
                "is_general_partner", sa.Boolean(), nullable=True,
                comment="TRUE general / FALSE limited (1065 K-1 item G). "
                        "Affects §469 passive treatment and recourse debt.",
            ),
            sa.Column(
                "is_officer", sa.Boolean(), nullable=True,
                comment="Shareholder is also an officer — relevant to the "
                        "S-corp reasonable-compensation requirement.",
            ),
            # --- the allocation interlock ---
            sa.Column(
                "allocation_basis", sa.String(length=48), nullable=True,
                comment="NULL = not recorded, treated as a REFUSAL to allocate "
                        "(never as pro rata). See the module docstring.",
            ),
            sa.Column("notes", sa.Text(), nullable=True),
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
                name="entity_owner_entity_fk", ondelete="RESTRICT",
            ),
            sa.CheckConstraint(
                "owner_type IN ('individual','entity','trust','estate')",
                name="ck_entity_owner_type",
            ),
            sa.CheckConstraint(
                "allocation_basis IS NULL OR allocation_basis IN ("
                "'per_share_per_day',"
                "'partnership_agreement_pro_rata',"
                "'partnership_agreement_special')",
                name="ck_entity_owner_allocation_basis",
            ),
            sa.CheckConstraint(
                "effective_to IS NULL OR effective_to >= effective_from",
                name="ck_entity_owner_period",
            ),
            sa.CheckConstraint(
                "shares IS NULL OR shares >= 0",
                name="ck_entity_owner_shares_nonneg",
            ),
            sa.CheckConstraint(
                "(profit_pct IS NULL OR (profit_pct >= 0 AND profit_pct <= 100)) "
                "AND (loss_pct IS NULL OR (loss_pct >= 0 AND loss_pct <= 100)) "
                "AND (capital_pct IS NULL OR "
                "     (capital_pct >= 0 AND capital_pct <= 100))",
                name="ck_entity_owner_pct_range",
            ),
            comment=(
                "Shareholders (1120-S) / partners (1065) of an entity, "
                "time-sliced by effective_from/effective_to. Drives Schedule "
                "K-1 allocation."
            ),
        )

    for idx, cols in (
        ("ix_entity_owner_workspace_id", ["workspace_id"]),
        ("ix_entity_owner_entity_id", ["entity_id"]),
        ("ix_entity_owner_entity_period", ["entity_id", "effective_from"]),
    ):
        if not _index_exists(schema, idx):
            op.create_index(idx, ENTITY_OWNER, cols)

    _harden(schema, ENTITY_OWNER)


def downgrade() -> None:
    schema = _set_search_path()
    for suffix in ("sel", "ins", "upd", "del"):
        op.execute(f"DROP POLICY IF EXISTS {ENTITY_OWNER}_ws_{suffix} ON {ENTITY_OWNER}")
    op.execute(f"ALTER TABLE {ENTITY_OWNER} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {ENTITY_OWNER} DISABLE ROW LEVEL SECURITY")
    if _table_exists(schema, ENTITY_OWNER):
        op.drop_table(ENTITY_OWNER)
