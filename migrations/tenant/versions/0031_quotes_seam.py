"""Quotes seam: invoice.quote_ref + invoice_schedules + bill (AP draft)

Revision ID: 0031_quotes_seam
Revises: 0030_mab_include_corrections
Create Date: 2026-09-12

Money seam for Quotes & Proposals (QUOTES-CONTRACT-v1 §3 "Accounting-Ops" +
"Partner split", as amended by AMENDMENT-01 A5/A7/A12). When Customer-Ops
emits ``quote.accepted`` this app creates DRAFT paper only: the deposit
invoice, one draft per remaining payment part, a recurring schedule per
recurring line (which generates a draft per period), and — when a delivery
partner + split rule are present — a DRAFT partner BILL (accounts payable)
in the vendor of record's books. Nothing here posts to the journal; a human
issues.

Four schema changes, all idempotent + re-runnable on a FRESH database (review of
PR #13 folded the partner-per-period, anchor-date, net-terms and supersede
columns in here rather than a 0032 because 0031 had not been applied on any
ring).

⚠️  NOT re-runnable on a database that already applied the FIRST shape of
0031 (commit 700f5b7): the columns added by the review — ``anchor_date``,
``net_terms``, the six partner columns (``partner_supplier_id``,
``vendor_entity_id``, ``partner_amount_cents``, ``partner_split``,
``partner_periods_billed``, ``generated_bill_ids``), ``bill.voided_at``,
``bill.void_reason`` and ``uq_bill_entity_number`` — live inside
``create_table`` behind ``_table_exists`` with an UNCHANGED revision id. Such a
database is already stamped at ``0031_quotes_seam`` (head), ``upgrade head`` is
a no-op, and the seam fails at runtime (``column "anchor_date" does not
exist``). Fix on that database only:

    alembic -c alembic-tenant.ini -x tenant=books downgrade 0030_mab_include_corrections
    alembic -c alembic-tenant.ini -x tenant=books upgrade head

or drop ``invoice_schedules`` and ``bill`` (both draft-only tables) and
``upgrade head``. Fresh installs and every ring (none applied the first shape)
are unaffected.

  invoice.quote_ref     nullable text "number + version" (e.g. ``Q-2026-0001 v2``)
                        stamped on every invoice the seam creates, so the
                        quote → invoices link survives without copying the
                        quote in. Partial index for the reverse lookup.

  invoice_schedules     one row per accepted ``recurring`` quote line. The
                        schedule runner (app.ar.quotes_seam.generate_due_schedule_invoices)
                        creates the next period's DRAFT when ``next_run_at``
                        is due, appends its id to ``generated_invoice_ids``,
                        advances ``next_run_at`` by one period and retires the
                        row when ``periods_total`` is reached. Idempotent on
                        (workspace_id, quote_ref, line_position).

  engagement_policy     + ``invoice_net_terms`` (nullable): the workspace default
                        Net-N for seam drafts when the payload does not say
                        (payload -> workspace setting -> 30).

  bill                  accounts-PAYABLE document (a supplier's invoice to one
                        of our entities). Until now AO only modelled AR
                        (``invoice``). AMENDMENT-01 A12: the partner's invoice
                        is a payable for the vendor of record, so it is a
                        ``bill`` draft from the delivery partner (a supplier
                        counterparty) to the vendor entity — never AR, never
                        auto-issued. ``journal_entry_id`` stays NULL for a
                        draft; the AP posting path (DR expense / CR Accounts
                        Payable) is a documented follow-up
                        (docs/quotes/proposals/L3-ap-bill-draft-and-issue-path.md).

Grants / RLS / TRUNCATE posture (mirrors 0019 + 0029)
=====================================================
  - SELECT, INSERT, UPDATE -> accounting_ops_app on both new tables. NO DELETE:
    schedules are retired via ``active=false``; bills are corrected via status
    (void) like invoices. DELETE/TRUNCATE explicitly REVOKEd + the
    forbid_append_only_truncate() trigger from 0005.
  - RLS ENABLE + FORCE with the per-command policy set from 0026, applied here
    explicitly because 0026 discovers tables dynamically and has already run —
    a table created afterwards would otherwise be the one unprotected
    workspace-scoped table in the schema.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0031_quotes_seam"
down_revision: str | None = "0030_mab_include_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
SCHEDULES = "invoice_schedules"
BILL = "bill"

# Same predicate + per-command policy shape as 0026 / 0029.
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


def _column_exists(schema: str, table: str, column: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = :schema AND table_name = :table
                  AND column_name = :column
                """
            ),
            {"schema": schema, "table": table, "column": column},
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
    """Least-privilege grants (no DELETE) + TRUNCATE guard + 0026-shape FORCE RLS."""
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{table} TO {RUNTIME_ROLE}'
    )
    op.execute(f'REVOKE DELETE, TRUNCATE ON "{schema}".{table} FROM {RUNTIME_ROLE}')
    op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate "
        f"BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

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

    # updated_at maintenance — reuse coa_set_updated_at() from 0003.
    op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")
    op.execute(
        f"CREATE TRIGGER trg_{table}_updated_at "
        f"BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION coa_set_updated_at()"
    )


def upgrade() -> None:
    schema = _set_search_path()

    # =====================================================================
    # invoice.quote_ref
    # =====================================================================
    if not _column_exists(schema, "invoice", "quote_ref"):
        op.add_column(
            "invoice",
            sa.Column(
                "quote_ref",
                sa.String(length=160),
                nullable=True,
                comment=(
                    "Customer-Ops quote reference 'number + version' "
                    "(e.g. Q-2026-0001 v2) for invoices created by the quotes seam."
                ),
            ),
        )
    if not _index_exists(schema, "ix_invoice_workspace_quote_ref"):
        op.execute(
            "CREATE INDEX ix_invoice_workspace_quote_ref "
            f'ON "{schema}".invoice (workspace_id, quote_ref) '
            "WHERE quote_ref IS NOT NULL"
        )

    # =====================================================================
    # invoice_schedules
    # =====================================================================
    if not _table_exists(schema, SCHEDULES):
        op.create_table(
            SCHEDULES,
            sa.Column(
                "id", sa.UUID(), nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "quote_ref", sa.String(length=160), nullable=False,
                comment="Quote 'number + version' this schedule was accepted under.",
            ),
            sa.Column(
                "ar_customer_id", sa.UUID(), nullable=False,
                comment="The AR customer (counterparty) every generated draft bills.",
            ),
            sa.Column(
                "line_position", sa.Integer(), nullable=False,
                comment="Position of the recurring quote line; part of the idempotency key.",
            ),
            sa.Column(
                "cadence", sa.String(length=8), nullable=False,
                comment="month | quarter | year — one draft per period.",
            ),
            sa.Column(
                "amount_cents", sa.BigInteger(), nullable=False,
                comment="Per-period amount in integer cents (tax excluded).",
            ),
            sa.Column(
                "currency", sa.CHAR(length=3), nullable=False,
                server_default=sa.text("'USD'"),
            ),
            sa.Column("description", sa.String(length=512), nullable=False),
            sa.Column(
                "anchor_date", sa.Date(), nullable=False,
                comment="Period 1 date (the acceptance date). Every later period is "
                        "anchor + n periods with month-end clamping, never a drift "
                        "from the previously clamped date (Jan 31 -> Feb 28 -> Mar 31).",
            ),
            sa.Column(
                "net_terms", sa.Integer(), nullable=False,
                server_default=sa.text("30"),
                comment="Net-N days for every generated draft (payload -> workspace default -> 30).",
            ),
            sa.Column(
                "term_months", sa.Integer(), nullable=True,
                comment="Committed term from the quote line; NULL = open-ended.",
            ),
            sa.Column(
                "periods_total", sa.Integer(), nullable=True,
                comment="Number of periods to generate over the term; NULL = open-ended.",
            ),
            sa.Column(
                "periods_generated", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "next_run_at", sa.TIMESTAMP(timezone=True), nullable=True,
                comment="When the next period's draft is due to be generated; NULL = done.",
            ),
            sa.Column(
                "generated_invoice_ids", JSONB(),
                nullable=False, server_default=sa.text("'[]'::jsonb"),
                comment="Ordered list of the invoice ids generated by this schedule.",
            ),
            sa.Column(
                "active", sa.Boolean(), nullable=False,
                server_default=sa.text("true"),
            ),
            # --- partner split (AMENDMENT-01 A12; review H3): the AP side of every
            #     period is recoverable from the row and generated by the runner ---
            sa.Column(
                "partner_supplier_id", sa.UUID(), nullable=True,
                comment="Supplier counterparty (kind='vendor') billed per period; NULL = no partner share.",
            ),
            sa.Column(
                "vendor_entity_id", sa.UUID(), nullable=True,
                comment="The entity that OWES the per-period partner bill (vendor of record).",
            ),
            sa.Column(
                "partner_amount_cents", sa.BigInteger(), nullable=True,
                comment="Partner's share of ONE period of this line (hosting in full when "
                        "hosting_to_partner); NULL/0 = nothing to bill.",
            ),
            sa.Column(
                "partner_split", JSONB(), nullable=True,
                comment="Split summary {kind, percent, hosting_to_partner, is_hosting, partner_name, basis}.",
            ),
            sa.Column(
                "partner_periods_billed", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
                comment="Partner-side cursor: periods whose partner bill exists (period 1 is in the acceptance bill).",
            ),
            sa.Column(
                "generated_bill_ids", JSONB(), nullable=False,
                server_default=sa.text("'[]'::jsonb"),
                comment="Ordered list of the per-period partner bill ids generated by the runner.",
            ),
            sa.Column("created_by_uc_uid", sa.String(length=256), nullable=False),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "workspace_id", "quote_ref", "line_position",
                name="uq_invoice_schedules_ws_quote_line",
            ),
            sa.ForeignKeyConstraint(
                ["partner_supplier_id"], ["counterparty.id"],
                name="invoice_schedules_partner_supplier_fk", ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["vendor_entity_id"], ["entity.id"],
                name="invoice_schedules_vendor_entity_fk", ondelete="RESTRICT",
            ),
            sa.CheckConstraint(
                "partner_amount_cents IS NULL OR partner_amount_cents >= 0",
                name="ck_invoice_schedules_partner_amount_nonneg",
            ),
            sa.CheckConstraint(
                "net_terms >= 0", name="ck_invoice_schedules_net_terms_nonneg"
            ),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"],
                name="invoice_schedules_entity_fk", ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["ar_customer_id"], ["counterparty.id"],
                name="invoice_schedules_customer_fk", ondelete="RESTRICT",
            ),
            sa.CheckConstraint(
                "cadence IN ('month','quarter','year')",
                name="ck_invoice_schedules_cadence",
            ),
            sa.CheckConstraint(
                "amount_cents >= 0", name="ck_invoice_schedules_amount_nonneg"
            ),
            sa.CheckConstraint(
                "currency = upper(currency)", name="ck_invoice_schedules_currency_upper"
            ),
            comment=(
                "Recurring-invoice schedule created when a quote with recurring "
                "lines is accepted. Generates one DRAFT invoice per period."
            ),
        )
    for idx, cols in (
        ("ix_invoice_schedules_workspace", ["workspace_id"]),
        ("ix_invoice_schedules_entity", ["entity_id"]),
        ("ix_invoice_schedules_due", ["active", "next_run_at"]),
    ):
        if not _index_exists(schema, idx):
            op.create_index(idx, SCHEDULES, cols)
    _harden(schema, SCHEDULES)

    # =====================================================================
    # bill (accounts payable)
    # =====================================================================
    if not _table_exists(schema, BILL):
        op.create_table(
            BILL,
            sa.Column(
                "id", sa.UUID(), nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column(
                "entity_id", sa.UUID(), nullable=False,
                comment="The entity that OWES this bill (the vendor of record).",
            ),
            sa.Column(
                "supplier_counterparty_id", sa.UUID(), nullable=False,
                comment="The supplier who billed us (a counterparty row, kind='vendor').",
            ),
            sa.Column(
                "bill_number", sa.String(length=64), nullable=True,
                comment="Our reference for the bill (the supplier's own number may differ).",
            ),
            sa.Column(
                "status", sa.String(length=16), nullable=False,
                server_default=sa.text("'draft'"),
                comment="draft|approved|posted|void",
            ),
            sa.Column(
                "currency", sa.CHAR(length=3), nullable=False,
                server_default=sa.text("'USD'"),
            ),
            sa.Column("issue_date", sa.Date(), nullable=False),
            sa.Column("due_date", sa.Date(), nullable=False),
            sa.Column("subtotal", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column("tax_total", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column("total", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column("balance_due", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "lines", JSONB(), nullable=False,
                server_default=sa.text("'[]'::jsonb"),
                comment="[{line_number, description, quantity, unit_price, amount}] — draft lines.",
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "quote_ref", sa.String(length=160), nullable=True,
                comment="Quote 'number + version' when the bill came from the quotes seam.",
            ),
            sa.Column(
                "external_source", sa.String(length=64), nullable=False,
                server_default=sa.text("'manual'"),
            ),
            sa.Column(
                "external_ref", sa.String(length=256), nullable=True,
                comment="Origin system's unique id for idempotency; NULL for pure-manual.",
            ),
            sa.Column(
                "journal_entry_id", sa.UUID(), nullable=True,
                comment="NULL while draft. The AP posting path is a follow-up (never auto).",
            ),
            sa.Column("voided_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column(
                "void_reason", sa.Text(), nullable=True,
                comment="Why a draft was voided (e.g. superseded by a newer accepted quote version).",
            ),
            sa.Column("created_by_uc_uid", sa.String(length=256), nullable=False),
            sa.Column(
                "created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            # Mirrors uq_invoice_entity_number (0019): two concurrent seam calls in
            # the same entity/year cannot both mint BILL-YYYY-NNNN.
            sa.UniqueConstraint("entity_id", "bill_number", name="uq_bill_entity_number"),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"], name="bill_entity_fk", ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["supplier_counterparty_id"], ["counterparty.id"],
                name="bill_supplier_fk", ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["journal_entry_id"], ["journal_entry.id"],
                name="bill_je_fk", ondelete="SET NULL",
            ),
            sa.CheckConstraint(
                "status IN ('draft','approved','posted','void')", name="ck_bill_status"
            ),
            sa.CheckConstraint("currency = upper(currency)", name="ck_bill_currency_upper"),
            comment=(
                "Accounts-payable bill received by an entity from a supplier. "
                "v1: DRAFT documents only (quotes seam partner split); posting is a follow-up."
            ),
        )
    if not _index_exists(schema, "uq_bill_workspace_source_ref"):
        op.execute(
            "CREATE UNIQUE INDEX uq_bill_workspace_source_ref "
            f'ON "{schema}".bill (workspace_id, external_source, external_ref) '
            "WHERE external_ref IS NOT NULL"
        )
    for idx, cols in (
        ("ix_bill_workspace", ["workspace_id"]),
        ("ix_bill_entity_status", ["entity_id", "status", "due_date"]),
        ("ix_bill_supplier", ["supplier_counterparty_id"]),
    ):
        if not _index_exists(schema, idx):
            op.create_index(idx, BILL, cols)
    _harden(schema, BILL)

    # =====================================================================
    # engagement_policy.invoice_net_terms — workspace default Net-N for seam drafts
    # =====================================================================
    if _table_exists(schema, "engagement_policy") and not _column_exists(
        schema, "engagement_policy", "invoice_net_terms"
    ):
        op.add_column(
            "engagement_policy",
            sa.Column(
                "invoice_net_terms",
                sa.Integer(),
                nullable=True,
                comment=(
                    "Workspace default Net-N days for invoices the quotes seam drafts "
                    "when the payload does not say. NULL = the documented default (30)."
                ),
            ),
        )


def downgrade() -> None:
    schema = _set_search_path()
    for tbl in (BILL, SCHEDULES):
        if _table_exists(schema, tbl):
            for suffix in ("sel", "ins", "upd", "del"):
                op.execute(f"DROP POLICY IF EXISTS {tbl}_ws_{suffix} ON {tbl}")
            op.execute(f"ALTER TABLE {tbl} NO FORCE ROW LEVEL SECURITY")
            op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
            op.execute(f"DROP TRIGGER IF EXISTS trg_{tbl}_updated_at ON {tbl}")
            op.execute(f"DROP TRIGGER IF EXISTS {tbl}_no_truncate ON {tbl}")
            op.drop_table(tbl)
    op.execute("DROP INDEX IF EXISTS ix_invoice_workspace_quote_ref")
    if _column_exists(schema, "invoice", "quote_ref"):
        op.drop_column("invoice", "quote_ref")
    if _column_exists(schema, "engagement_policy", "invoice_net_terms"):
        op.drop_column("engagement_policy", "invoice_net_terms")
