"""per-workspace schema: realign Sentinel dedupe keys (dismiss period-stamped orphans)

Revision ID: 0021_sentinel_dedupe_realign
Revises: 0020_sentinel_finding_dedupe
Create Date: 2026-06-02

Follow-up to 0020. 0020 backfilled dedupe_key as ``{check_type}:{entity}`` for
most checks, but the runtime had briefly keyed findings on ``payload['topic']``,
which is period-stamped (``...-{month}``/``-{quarter}``/``-{today}``) for nearly
every check. That mismatch let a re-detected condition (recon_drift,
uncategorized_txns, tax_credit_scan, and any other topic-passing check that
fired) create a topic-keyed twin alongside the ``{check_type}:{entity}`` row —
the same incident under two keys.

The runtime now keys findings on ``{check_type}:{entity}`` (one open finding per
check per entity, refreshed in place), with an EXPLICIT per-subject key only for
the genuinely multi-per-entity loopers, whose keys are stable (no period):
  - filing_deadlines  -> sentinel-deadline-{form}-{due}
  - regulatory_updates-> sentinel-reg-{source}-{external_id}
  - approval_aging    -> sentinel-approval-aging-{id}
  - connector_health  -> sentinel-connector-{name}-{id8}
  - tax_credit_scan   -> tax_credit_scan:{entity}:{title}   (NOT a sentinel-* key)

So any OPEN finding whose key is ``sentinel-*`` OUTSIDE those four stable
prefixes is a now-orphaned, period-stamped duplicate the runtime will never
refresh again. Dismiss them (auditable; the underlying condition, if still true,
is re-detected next cycle under the stable ``{check_type}:{entity}`` key). The
matching ``{check_type}:{entity}`` row (when one exists) is the survivor.

Idempotent + safe on a fresh schema (no such rows -> no-op).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0021_sentinel_dedupe_realign"
down_revision: str | None = "0020_sentinel_finding_dedupe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# sentinel-* prefixes the runtime STILL writes (stable per-subject keys). Any
# other sentinel-* open key is a period-stamped orphan.
_KEEP_PREFIXES = (
    "sentinel-deadline-",
    "sentinel-reg-",
    "sentinel-approval-aging-",
    "sentinel-connector-",
)


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


def upgrade() -> None:
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0021 requires -x tenant=<schema>")
    if not _table_exists(schema, "sentinel_finding"):
        return

    keep = " AND ".join(
        f"dedupe_key NOT LIKE '{p}%'" for p in _KEEP_PREFIXES
    )
    op.execute(
        f"""
        UPDATE sentinel_finding
        SET status  = 'dismissed',
            details = COALESCE(details, '{{}}'::jsonb)
                      || jsonb_build_object('auto_deduped', true,
                                            'dedupe_key_realigned', true)
        WHERE status = 'open'
          AND dedupe_key LIKE 'sentinel-%'
          AND {keep}
        """
    )


def downgrade() -> None:
    # One-way data correction: dismissed period-stamped orphans are not
    # reopened (the condition, if still true, re-surfaces next Sentinel cycle).
    schema = _set_search_path()
    if not schema:
        raise RuntimeError("0021 requires -x tenant=<schema>")
