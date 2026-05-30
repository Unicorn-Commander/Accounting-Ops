"""per-workspace schema: tax_filing_submission table.

Revision ID: 0011_tax_filing_submission
Revises: 0010_approval_recommendation
Create Date: 2026-05-30

Adds the ``tax_filing_submission`` table that records every paper / MyDORWAY /
e-file submission the entity has made, with USPS tracking integration for
paper.

Design notes
------------
- One row per envelope dropped at USPS (or one MyDORWAY upload). Multiple
  forms can ride in a single envelope, captured as an array on
  ``forms_enclosed``.
- Pure record table — no submission *initiation* logic (the platform never
  auto-submits). All rows are created by an explicit user action:
  ``POST /api/workspaces/{wid}/tax-filing-submissions``.
- Tracking integration: when ``filing_method='paper'`` and
  ``tracking_number`` is set, the periodic poller hits USPS and updates
  ``last_tracking_status`` + ``last_tracking_at``.
- Expected-response window: a deterministic default per (jurisdiction,
  filing_method) at submission time. Daily job flips ``status`` to
  ``awaiting_followup`` if ``expected_response_by`` lapses with no ack.
- APPEND-MOSTLY: an existing row can be UPDATEd (set actual_response,
  status), but never DELETEd via the runtime role.
- RLS + grant mirror prior tenant tables.

Status state machine
--------------------
  draft           — record created, envelope not yet mailed (we know it's
                    going out but tracking-number not assigned)
  submitted       — envelope dropped at USPS / MyDORWAY upload completed
  in_transit      — USPS scanned the package (not yet delivered)
  delivered       — USPS confirms delivery to recipient address
  acknowledged    — taxing authority sent confirmation (notice received,
                    return processed, etc.)
  awaiting_followup — expected_response_by date passed without ack
  closed          — final resolution captured (acknowledged + acted on)
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0011_tax_filing_submission"
down_revision: str | None = "0010_approval_recommendation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"


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
        raise RuntimeError("0011 requires -x tenant=<schema>")

    # ------------------------------------------------------------------
    # 1. Create tax_filing_submission table (idempotent)
    # ------------------------------------------------------------------
    if not _table_exists(schema, "tax_filing_submission"):
        op.create_table(
            "tax_filing_submission",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("workspace_id", sa.UUID(), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=False),
            sa.Column(
                "jurisdiction",
                sa.Text(),
                nullable=False,
                comment="'irs' | 'scdor' | 'other_state'",
            ),
            sa.Column(
                "tax_year",
                sa.Integer(),
                nullable=True,
                comment=(
                    "Tax year this submission relates to. NULL for "
                    "non-year-specific items (e.g. POA filings)."
                ),
            ),
            sa.Column(
                "submission_title",
                sa.Text(),
                nullable=False,
                comment=(
                    "Human-readable label, e.g. "
                    "'TY 2023 SC1120 Cure Filing' or 'Form 1120 + Schedule L'."
                ),
            ),
            sa.Column(
                "forms_enclosed",
                sa.ARRAY(sa.Text()),
                nullable=False,
                server_default=sa.text("ARRAY[]::text[]"),
                comment=(
                    "List of form codes enclosed in this submission, e.g. "
                    "['SC1120', 'SC1120V', 'CURE_LETTER', 'CHECK_25']."
                ),
            ),
            sa.Column(
                "filing_method",
                sa.Text(),
                nullable=False,
                comment=(
                    "'paper' | 'mydorway' | 'efile' | 'fax' | 'eftps'. "
                    "'paper' is the only one with USPS tracking."
                ),
            ),
            sa.Column(
                "tracking_number",
                sa.Text(),
                nullable=True,
                comment=(
                    "USPS Certified Mail tracking number (if filing_method=paper). "
                    "Or upload confirmation # from MyDORWAY / e-file."
                ),
            ),
            sa.Column(
                "status",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'draft'"),
                comment=(
                    "State machine: draft → submitted → in_transit → "
                    "delivered → acknowledged → closed. "
                    "Or awaiting_followup if expected_response_by passes."
                ),
            ),
            sa.Column(
                "submitted_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                comment="When the envelope was dropped at USPS / MyDORWAY upload completed.",
            ),
            sa.Column(
                "expected_response_by",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                comment=(
                    "Date by which we expect an acknowledgement from the "
                    "taxing authority. Computed at submission time from "
                    "(jurisdiction, filing_method). Default windows: "
                    "IRS paper 56d, SCDOR paper 42d, MyDORWAY 30d."
                ),
            ),
            sa.Column(
                "last_tracking_status",
                sa.Text(),
                nullable=True,
                comment="Latest USPS event summary, e.g. 'DELIVERED'.",
            ),
            sa.Column(
                "last_tracking_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                comment="When the poller last successfully hit USPS for this row.",
            ),
            sa.Column(
                "delivered_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                comment="When USPS reported delivery to the recipient address.",
            ),
            sa.Column(
                "acknowledged_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
                comment="When the taxing authority responded (notice / approval / etc.).",
            ),
            sa.Column(
                "actual_response",
                sa.Text(),
                nullable=True,
                comment="Free-text capture of what the authority said (penalty abated, refund issued, etc.).",
            ),
            sa.Column(
                "destination_office",
                sa.Text(),
                nullable=True,
                comment=(
                    "Office address key (e.g. 'scdor_corporate', "
                    "'irs_1120_sc_nopayment'). Used to regenerate the "
                    "cover sheet + label."
                ),
            ),
            sa.Column(
                "notes",
                sa.Text(),
                nullable=True,
                comment="Free-text caller notes attached to this submission.",
            ),
            sa.Column(
                "submitter_uc_uid",
                sa.Text(),
                nullable=True,
                comment="UC user id of whoever recorded the submission.",
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
            sa.ForeignKeyConstraint(
                ["entity_id"],
                ["entity.id"],
                name="tax_filing_submission_entity_fk",
                ondelete="CASCADE",
            ),
            comment=(
                "Record of paper / MyDORWAY / e-file submissions to taxing "
                "authorities. Surface via "
                "GET /api/workspaces/{wid}/tax-filing-submissions and the "
                "/open-items 'Submissions in flight' section."
            ),
        )

    # ------------------------------------------------------------------
    # 2. Indexes (idempotent)
    # ------------------------------------------------------------------
    if not _index_exists(schema, "tfs_entity_year"):
        op.execute(
            f'CREATE INDEX tfs_entity_year '
            f'ON "{schema}".tax_filing_submission '
            f'(workspace_id, entity_id, tax_year DESC, submitted_at DESC)'
        )
    if not _index_exists(schema, "tfs_status"):
        op.execute(
            f'CREATE INDEX tfs_status '
            f'ON "{schema}".tax_filing_submission '
            f'(workspace_id, status, expected_response_by NULLS LAST)'
        )
    if not _index_exists(schema, "tfs_tracking_number"):
        op.execute(
            f'CREATE INDEX tfs_tracking_number '
            f'ON "{schema}".tax_filing_submission (tracking_number) '
            f"WHERE tracking_number IS NOT NULL"
        )

    # ------------------------------------------------------------------
    # 3. Status check constraint
    # ------------------------------------------------------------------
    bind = op.get_bind()
    check_exists = bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_schema = :schema
                  AND table_name = 'tax_filing_submission'
                  AND constraint_name = 'tfs_status_check'
                """
            ),
            {"schema": schema},
        ).first()
    )
    if not check_exists:
        op.execute(
            f'ALTER TABLE "{schema}".tax_filing_submission '
            "ADD CONSTRAINT tfs_status_check CHECK ("
            "status IN ('draft', 'submitted', 'in_transit', 'delivered', "
            "'acknowledged', 'awaiting_followup', 'closed'))"
        )

    method_check_exists = bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_schema = :schema
                  AND table_name = 'tax_filing_submission'
                  AND constraint_name = 'tfs_method_check'
                """
            ),
            {"schema": schema},
        ).first()
    )
    if not method_check_exists:
        op.execute(
            f'ALTER TABLE "{schema}".tax_filing_submission '
            "ADD CONSTRAINT tfs_method_check CHECK ("
            "filing_method IN ('paper', 'mydorway', 'efile', 'fax', 'eftps'))"
        )

    jurisdiction_check_exists = bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_schema = :schema
                  AND table_name = 'tax_filing_submission'
                  AND constraint_name = 'tfs_jurisdiction_check'
                """
            ),
            {"schema": schema},
        ).first()
    )
    if not jurisdiction_check_exists:
        op.execute(
            f'ALTER TABLE "{schema}".tax_filing_submission '
            "ADD CONSTRAINT tfs_jurisdiction_check CHECK ("
            "jurisdiction IN ('irs', 'scdor', 'other_state'))"
        )

    # ------------------------------------------------------------------
    # 4. updated_at trigger
    # ------------------------------------------------------------------
    op.execute(
        "DROP TRIGGER IF EXISTS tfs_updated_at_trigger ON tax_filing_submission"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION tfs_set_updated_at()
        RETURNS trigger AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER tfs_updated_at_trigger "
        "BEFORE UPDATE ON tax_filing_submission "
        "FOR EACH ROW EXECUTE FUNCTION tfs_set_updated_at()"
    )

    # ------------------------------------------------------------------
    # 5. Grants (SELECT, INSERT, UPDATE — no DELETE)
    # ------------------------------------------------------------------
    op.execute(
        f'GRANT SELECT, INSERT, UPDATE ON "{schema}".tax_filing_submission TO {RUNTIME_ROLE}'
    )
    op.execute(
        f'REVOKE DELETE, TRUNCATE ON "{schema}".tax_filing_submission FROM {RUNTIME_ROLE}'
    )

    # ------------------------------------------------------------------
    # 6. BEFORE TRUNCATE guard (reuse function from 0005)
    # ------------------------------------------------------------------
    op.execute(
        "DROP TRIGGER IF EXISTS tfs_no_truncate ON tax_filing_submission"
    )
    op.execute(
        "CREATE TRIGGER tfs_no_truncate "
        "BEFORE TRUNCATE ON tax_filing_submission "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_append_only_truncate()"
    )

    # ------------------------------------------------------------------
    # 7. Row-Level Security on workspace_id GUC
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE tax_filing_submission ENABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspace_isolation ON tax_filing_submission")
    op.execute(
        """
        CREATE POLICY workspace_isolation ON tax_filing_submission
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
        raise RuntimeError("0011 requires -x tenant=<schema>")

    op.execute("DROP POLICY IF EXISTS workspace_isolation ON tax_filing_submission")
    op.execute("DROP TRIGGER IF EXISTS tfs_no_truncate ON tax_filing_submission")
    op.execute("DROP TRIGGER IF EXISTS tfs_updated_at_trigger ON tax_filing_submission")
    op.execute("DROP FUNCTION IF EXISTS tfs_set_updated_at()")
    op.execute(f'DROP INDEX IF EXISTS "{schema}".tfs_tracking_number')
    op.execute(f'DROP INDEX IF EXISTS "{schema}".tfs_status')
    op.execute(f'DROP INDEX IF EXISTS "{schema}".tfs_entity_year')

    if _table_exists(schema, "tax_filing_submission"):
        op.drop_table("tax_filing_submission")
