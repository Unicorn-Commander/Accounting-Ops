"""per-workspace schema: AR / Invoicing (invoice + invoice_line + invoice_payment)

Revision ID: 0019_ar_invoicing
Revises: 0018_journal_hash_chain_constraint
Create Date: 2026-06-02

AR / Invoicing MVP (Phase 1). Native Accounts-Receivable invoicing, fully usable
STANDALONE (an invoice needs no PO at all); the Project-Ops cross-app path is just
one entitlement-gated caller of the same native tables.

Three entity-scoped tables (every row carries workspace_id for RLS, mirroring
every other tenant table from 0001/0003):

  invoice          one AR invoice issued by a billing entity to a customer
                   (a Counterparty). Status lifecycle:
                   draft|sent|viewed|partial|paid|overdue|void.
  invoice_line     one line item on an invoice (qty * unit_price = amount).
  invoice_payment  one payment received against an invoice (DR Cash / CR AR).

Idempotency
===========
``UNIQUE (workspace_id, external_source, external_ref)`` on invoice. A repeated
``create_invoice_from_draft`` call with the same (external_source, external_ref)
returns the EXISTING invoice and posts NO second journal entry — mirrors the
connector-ingest idempotency pattern (Stripe/eBay). external_ref is NULLABLE so
a purely-manual invoice (no upstream id) does not collide on a shared NULL: a
partial unique index only enforces uniqueness when external_ref IS NOT NULL.

Ledger linkage (NEVER writes the ledger directly)
=================================================
``invoice.journal_entry_id`` and ``invoice_payment.journal_entry_id`` reference
the journal_entry row(s) posted via the existing journal primitives
(app.journal._insert_journal_entry + the per-entity SHA-256 hash chain). Void /
credit-note is a NEW reversing entry (never an UPDATE of a posted entry); the
reversal's id is recorded on ``invoice.void_journal_entry_id``.

ReconLink hook (Phase 2)
========================
``invoice_payment.recon_link_id`` is the nullable wiring point for matching a
SimpleFIN bank payment to an open invoice via the existing ``recon_link`` table.
Left NULL for MVP (payments captured outside AO carry NULL); no FK is declared
because recon_link rows are created by a later phase.

Grants / RLS / TRUNCATE posture (mirrors 0005 / 0007)
=====================================================
  - SELECT, INSERT, UPDATE on all three tables -> accounting_ops_app
    (NO DELETE — invoices/payments are corrected via status changes + reversing
    journal entries, never row-deleted; an accidental DELETE grant would let the
    runtime role erase AR history).
  - DELETE/TRUNCATE explicitly REVOKEd (belt-and-suspenders).
  - RLS policy ``workspace_isolation`` keyed on the ``app.current_workspace`` GUC
    on every table (default-deny when the GUC is unset).

Idempotent + re-runnable: every DDL is guarded by an information_schema check, so
this applies cleanly to a NEW workspace schema at provision time AND to EXISTING
schemas via upgrade_all_workspaces.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0019_ar_invoicing"
down_revision: str | None = "0018_je_entity_this_hash_uq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"

# All three AR tables get SELECT/INSERT/UPDATE (no DELETE) + RLS + TRUNCATE guard.
AR_TABLES = ("invoice", "invoice_line", "invoice_payment")


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


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0019 requires -x tenant=<schema>")

    # =====================================================================
    # invoice
    # =====================================================================
    if not _table_exists(schema, "invoice"):
        op.create_table(
            "invoice",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "counterparty_id",
                sa.UUID(),
                nullable=False,
                comment="The AR customer — a counterparty row (is_customer flavor).",
            ),
            sa.Column(
                "invoice_number",
                sa.String(length=64),
                nullable=False,
                comment="Human-facing number, unique per (entity). e.g. INV-2026-0001",
            ),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'draft'"),
                comment="draft|sent|viewed|partial|paid|overdue|void",
            ),
            sa.Column(
                "currency",
                sa.CHAR(length=3),
                nullable=False,
                server_default=sa.text("'USD'"),
            ),
            sa.Column("issue_date", sa.Date(), nullable=False),
            sa.Column("due_date", sa.Date(), nullable=False),
            sa.Column(
                "net_terms",
                sa.Integer(),
                nullable=True,
                comment="Net-N days used to derive due_date when not given explicitly.",
            ),
            # Money columns: NUMERIC(20,4) to match journal_entry.amount precision.
            sa.Column("subtotal", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column("discount_total", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column("tax_total", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column("total", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "balance_due",
                sa.Numeric(20, 4),
                nullable=False,
                server_default=sa.text("0"),
                comment="total - sum(payments). 0 => paid.",
            ),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "po_number",
                sa.String(length=128),
                nullable=True,
                comment="Optional buyer PO reference (free text; the cross-app path also fills it).",
            ),
            sa.Column(
                "external_source",
                sa.String(length=64),
                nullable=False,
                server_default=sa.text("'manual'"),
                comment="'manual' | 'project-ops' | 'stripe' | ... — origin system label.",
            ),
            sa.Column(
                "external_ref",
                sa.String(length=256),
                nullable=True,
                comment="Origin system's unique id for idempotency; NULL for pure-manual.",
            ),
            sa.Column(
                "journal_entry_id",
                sa.UUID(),
                nullable=True,
                comment="The DR-AR/CR-Revenue(+tax) journal_entry row posted on issue.",
            ),
            sa.Column(
                "void_journal_entry_id",
                sa.UUID(),
                nullable=True,
                comment="The reversing journal_entry row appended on void/credit-note.",
            ),
            sa.Column(
                "voided_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column("void_reason", sa.Text(), nullable=True),
            sa.Column(
                "hosted_pay_url",
                sa.Text(),
                nullable=True,
                comment="Phase-2 Stripe pay link hook; NULL in MVP.",
            ),
            sa.Column(
                "pdf_key",
                sa.String(length=512),
                nullable=True,
                comment="Garage object key for a rendered PDF, if persisted (MVP renders on the fly).",
            ),
            sa.Column(
                "created_by_uc_uid",
                sa.String(length=256),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "entity_id", "invoice_number", name="uq_invoice_entity_number"
            ),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"], name="invoice_entity_fk", ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["counterparty_id"],
                ["counterparty.id"],
                name="invoice_counterparty_fk",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["journal_entry_id"],
                ["journal_entry.id"],
                name="invoice_je_fk",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["void_journal_entry_id"],
                ["journal_entry.id"],
                name="invoice_void_je_fk",
                ondelete="SET NULL",
            ),
            sa.CheckConstraint(
                "status IN ('draft','sent','viewed','partial','paid','overdue','void')",
                name="ck_invoice_status",
            ),
            sa.CheckConstraint(
                "currency = upper(currency)", name="ck_invoice_currency_upper"
            ),
            comment=(
                "AR invoice issued by an entity to a customer. Native to AO + usable "
                "standalone (no PO). Idempotent on (workspace_id, external_source, "
                "external_ref) when external_ref IS NOT NULL."
            ),
        )

    # Idempotency: a repeat ingest of the SAME upstream invoice cannot create a
    # duplicate. Partial index so many NULL external_refs (pure-manual invoices)
    # don't collide on a shared NULL.
    if not _index_exists(schema, "uq_invoice_workspace_source_ref"):
        op.execute(
            f'CREATE UNIQUE INDEX uq_invoice_workspace_source_ref '
            f'ON "{schema}".invoice (workspace_id, external_source, external_ref) '
            f'WHERE external_ref IS NOT NULL'
        )
    if not _index_exists(schema, "ix_invoice_workspace"):
        op.create_index("ix_invoice_workspace", "invoice", ["workspace_id"])
    if not _index_exists(schema, "ix_invoice_entity_status"):
        op.create_index(
            "ix_invoice_entity_status", "invoice", ["entity_id", "status", "due_date"]
        )
    if not _index_exists(schema, "ix_invoice_counterparty"):
        op.create_index("ix_invoice_counterparty", "invoice", ["counterparty_id"])

    # =====================================================================
    # invoice_line
    # =====================================================================
    if not _table_exists(schema, "invoice_line"):
        op.create_table(
            "invoice_line",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column("invoice_id", sa.UUID(), nullable=False),
            sa.Column(
                "line_number",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
                comment="1-based ordering within the invoice.",
            ),
            sa.Column("description", sa.String(length=512), nullable=False),
            sa.Column("quantity", sa.Numeric(20, 4), nullable=False, server_default=sa.text("1")),
            sa.Column("unit_price", sa.Numeric(20, 4), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "amount",
                sa.Numeric(20, 4),
                nullable=False,
                server_default=sa.text("0"),
                comment="quantity * unit_price (pre-tax line extended amount).",
            ),
            sa.Column(
                "tax_code",
                sa.String(length=32),
                nullable=True,
                comment="Free-text tax code; the sales-tax engine is a later phase.",
            ),
            sa.Column(
                "tax_amount",
                sa.Numeric(20, 4),
                nullable=False,
                server_default=sa.text("0"),
                comment="Per-line tax (caller-supplied in MVP; engine is later).",
            ),
            sa.Column(
                "revenue_account_id",
                sa.UUID(),
                nullable=True,
                comment="COA account this line credits; resolved to a default if NULL.",
            ),
            sa.Column(
                "revenue_account_canonical",
                sa.String(length=160),
                nullable=True,
                comment="Resolved canonical_form of the revenue account, snapshotted at issue.",
            ),
            sa.Column(
                "source_ref",
                sa.String(length=256),
                nullable=True,
                comment="Upstream line reference (e.g. a Project-Ops billing line id).",
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["invoice_id"],
                ["invoice.id"],
                name="invoice_line_invoice_fk",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["entity_id"], ["entity.id"], name="invoice_line_entity_fk", ondelete="RESTRICT"
            ),
            sa.ForeignKeyConstraint(
                ["revenue_account_id"],
                ["chart_of_account.id"],
                name="invoice_line_revenue_acct_fk",
                ondelete="SET NULL",
            ),
        )
    if not _index_exists(schema, "ix_invoice_line_invoice"):
        op.create_index("ix_invoice_line_invoice", "invoice_line", ["invoice_id"])
    if not _index_exists(schema, "ix_invoice_line_workspace"):
        op.create_index("ix_invoice_line_workspace", "invoice_line", ["workspace_id"])

    # =====================================================================
    # invoice_payment
    # =====================================================================
    if not _table_exists(schema, "invoice_payment"):
        op.create_table(
            "invoice_payment",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column("invoice_id", sa.UUID(), nullable=False),
            sa.Column("amount", sa.Numeric(20, 4), nullable=False),
            sa.Column(
                "method",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'manual'"),
                comment="manual|ach|card|check|wire|cash|stripe|other",
            ),
            sa.Column("received_date", sa.Date(), nullable=False),
            sa.Column(
                "recon_link_id",
                sa.UUID(),
                nullable=True,
                comment="Phase-2 hook: links to a recon_link row matching a bank txn. "
                "No FK (recon_link rows are created by a later phase).",
            ),
            sa.Column(
                "journal_entry_id",
                sa.UUID(),
                nullable=True,
                comment="The DR-Cash/CR-AR journal_entry row posted for this payment.",
            ),
            sa.Column(
                "external_ref",
                sa.String(length=256),
                nullable=True,
                comment="Idempotency key for a payment (e.g. a processor charge id).",
            ),
            sa.Column("memo", sa.Text(), nullable=True),
            sa.Column(
                "created_by_uc_uid",
                sa.String(length=256),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(
                ["invoice_id"],
                ["invoice.id"],
                name="invoice_payment_invoice_fk",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="invoice_payment_entity_fk",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["journal_entry_id"],
                ["journal_entry.id"],
                name="invoice_payment_je_fk",
                ondelete="SET NULL",
            ),
        )
    if not _index_exists(schema, "ix_invoice_payment_invoice"):
        op.create_index(
            "ix_invoice_payment_invoice", "invoice_payment", ["invoice_id"]
        )
    if not _index_exists(schema, "ix_invoice_payment_workspace"):
        op.create_index(
            "ix_invoice_payment_workspace", "invoice_payment", ["workspace_id"]
        )
    # Payment idempotency (only when an external_ref is supplied).
    if not _index_exists(schema, "uq_invoice_payment_invoice_ref"):
        op.execute(
            f'CREATE UNIQUE INDEX uq_invoice_payment_invoice_ref '
            f'ON "{schema}".invoice_payment (invoice_id, external_ref) '
            f'WHERE external_ref IS NOT NULL'
        )

    # =====================================================================
    # Grants (least privilege: SELECT/INSERT/UPDATE, NO DELETE) + RLS + guards
    # =====================================================================
    # forbid_append_only_truncate() was created in 0005; re-use it.
    for tbl in AR_TABLES:
        op.execute(
            f'GRANT SELECT, INSERT, UPDATE ON "{schema}".{tbl} TO {RUNTIME_ROLE}'
        )
        op.execute(
            f'REVOKE DELETE, TRUNCATE ON "{schema}".{tbl} FROM {RUNTIME_ROLE}'
        )
        op.execute(f"DROP TRIGGER IF EXISTS {tbl}_no_truncate ON {tbl}")
        op.execute(
            f"CREATE TRIGGER {tbl}_no_truncate "
            f"BEFORE TRUNCATE ON {tbl} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
        )
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {tbl}")
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

    # updated_at trigger for invoice (reuse the coa_set_updated_at() function
    # created in 0003 — it just sets NEW.updated_at = now()).
    op.execute("DROP TRIGGER IF EXISTS trg_invoice_updated_at ON invoice")
    op.execute(
        "CREATE TRIGGER trg_invoice_updated_at "
        "BEFORE UPDATE ON invoice "
        "FOR EACH ROW EXECUTE FUNCTION coa_set_updated_at()"
    )


def downgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0019 requires -x tenant=<schema>")

    op.execute("DROP TRIGGER IF EXISTS trg_invoice_updated_at ON invoice")
    for tbl in AR_TABLES:
        op.execute(f"DROP POLICY IF EXISTS workspace_isolation ON {tbl}")
        op.execute(f"ALTER TABLE {tbl} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP TRIGGER IF EXISTS {tbl}_no_truncate ON {tbl}")

    # Drop in FK-dependency order: payment + line reference invoice.
    if _table_exists(schema, "invoice_payment"):
        op.drop_table("invoice_payment")
    if _table_exists(schema, "invoice_line"):
        op.drop_table("invoice_line")
    if _table_exists(schema, "invoice"):
        op.drop_table("invoice")
