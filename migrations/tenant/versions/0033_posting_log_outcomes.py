"""per-workspace schema: ck_posting_log_outcome += blocked_period_closed, human_rejected.

Revision ID: 0033_posting_log_outcomes
Revises: 0032_invoice_brand
Create Date: 2026-11-21

The CHECK created by 0003_journal_v2 lists seven outcomes, but the code has
since grown two more that ``_log_post_attempt`` writes:

  * ``blocked_period_closed`` — app/journal/posting.py:673,
    app/journal/posting.py:1095, app/connectors/stripe/mapper.py:745,
    app/connectors/ebay/mapper.py:630 (latent since the period-close guard).
  * ``human_rejected`` — app/connectors/ebay/mapper.py:585 (rejected eBay
    item replayed by the import worker).

On Postgres either write raises CheckViolation and aborts the transaction.
Recreate the constraint with the union of the original seven values and the
two new literals; no existing value is removed, so the change is purely
widening. Every other literal/derived outcome passed to ``_log_post_attempt``
(posted, reversed, queued_for_confirm, blocked_materiality, blocked_unmapped,
blocked_unknown_category, blocked_idempotent) is already allowed.

Downgrade restores the original seven-value list verbatim.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0033_posting_log_outcomes"
down_revision: str | None = "0032_invoice_brand"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGINAL_OUTCOMES = (
    "posted",
    "queued_for_confirm",
    "blocked_materiality",
    "blocked_unmapped",
    "blocked_unknown_category",
    "blocked_idempotent",
    "reversed",
)
_ADDED_OUTCOMES = ("blocked_period_closed", "human_rejected")


def _set_search_path() -> None:
    bind = op.get_bind()
    schema = context.get_x_argument(as_dictionary=True).get("tenant")
    if schema:
        bind.execute(sa.text(f'SET LOCAL search_path = "{schema}", public'))


def _recreate_check(values: tuple[str, ...]) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "ALTER TABLE posting_log "
            "DROP CONSTRAINT IF EXISTS ck_posting_log_outcome"
        )
    )
    value_list = ", ".join(f"'{v}'" for v in values)
    bind.execute(
        sa.text(
            "ALTER TABLE posting_log ADD CONSTRAINT ck_posting_log_outcome "
            f"CHECK (outcome IN ({value_list}))"
        )
    )


def upgrade() -> None:
    _set_search_path()
    _recreate_check(_ORIGINAL_OUTCOMES + _ADDED_OUTCOMES)


def downgrade() -> None:
    _set_search_path()
    _recreate_check(_ORIGINAL_OUTCOMES)
