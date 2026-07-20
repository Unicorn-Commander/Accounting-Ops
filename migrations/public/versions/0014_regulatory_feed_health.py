"""public schema: regulatory feed health (dead-feed detection)

Revision ID: 0014_regulatory_feed_health
Revises: 0013_workspace_uc_registry_id
Create Date: 2026-07-20

Why
===
The IRS Newsroom feed URL (``https://www.irs.gov/newsroom/news-releases-rss``)
began returning 404 at some unknown point and NOBODY NOTICED. The poller caught
the exception, logged one warning line, and returned an empty list:

    IRS Newsroom fetch failed: Client error '404 Not Found' for url ...
    regulatory_feeds: irs_newsroom_rss — 0 new / 0 seen

The Sentinel ``regulatory_updates`` check depends on that feed, so it was
silently inert for weeks — every cycle reporting a healthy "0 new" while
actually being blind. A green pipeline hiding a dead dependency: the failure
mode here is the ABSENCE of a health signal, not the stale URL. Fixing only the
URL would leave the next feed rot equally invisible.

Table
=====
  regulatory_feed_health   One row per feed source (PK ``source``), upserted on
                           every poll attempt — success or failure.

    last_attempt_at        Every attempt stamps this. If it stops advancing,
                           the POLLER is dead (not the feed).
    last_success_at        Only a successful fetch advances this. NULL means the
                           feed has never once succeeded.
    consecutive_failures   Reset to 0 on success; incremented on failure. The
                           primary "this feed is dead" signal.
    last_error             Truncated error text from the most recent failure.
    last_entry_count       Entries returned by the last SUCCESSFUL fetch. A
                           feed that returns 200 with zero items forever is
                           also broken — reachable but useless.

Lives in PUBLIC alongside ``regulatory_feed_entry`` (0009): the feeds are
global, so their health is global too. Per-workspace SURFACING of that health
is a tenant concern — the ``regulatory_updates`` check reads this table and
raises a ``sentinel_finding`` in each workspace, so a dead feed becomes a
visible finding instead of a log line nobody reads.

Grants
======
  SELECT, INSERT, UPDATE to accounting_ops_app (upsert on every poll).
  DELETE/TRUNCATE revoked — health history is operational evidence.

No RLS: this table is deliberately global (same posture as
regulatory_feed_entry in 0009 — it carries no workspace_id and every tenant
reads the same rows).

Idempotent + re-runnable.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_regulatory_feed_health"
down_revision: str | None = "0013_workspace_uc_registry_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "accounting_ops_app"
FEED_HEALTH = "regulatory_feed_health"


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = :table
                """
            ),
            {"table": table},
        ).first()
    )


def upgrade() -> None:
    if not _table_exists(FEED_HEALTH):
        op.create_table(
            FEED_HEALTH,
            sa.Column(
                "source", sa.String(length=64), nullable=False,
                comment="Feed slug, e.g. 'irs_federal_register', 'scdor_notices'.",
            ),
            sa.Column(
                "last_attempt_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
                comment="Every poll attempt stamps this, success or failure.",
            ),
            sa.Column(
                "last_success_at", sa.TIMESTAMP(timezone=True), nullable=True,
                comment="Only a successful fetch advances this. NULL = never succeeded.",
            ),
            sa.Column(
                "consecutive_failures", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
                comment="Reset to 0 on success; incremented on failure.",
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "last_entry_count", sa.Integer(), nullable=False,
                server_default=sa.text("0"),
                comment="Entries returned by the last SUCCESSFUL fetch.",
            ),
            sa.Column(
                "updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.PrimaryKeyConstraint("source"),
            schema="public",
            comment=(
                "Per-source health of the regulatory feed poller. Exists so a "
                "permanently-failing feed surfaces as a Sentinel finding "
                "instead of a log line nobody reads."
            ),
        )

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON public.{FEED_HEALTH} TO {RUNTIME_ROLE}"
    )
    op.execute(
        f"REVOKE DELETE, TRUNCATE ON public.{FEED_HEALTH} FROM {RUNTIME_ROLE}"
    )


def downgrade() -> None:
    if _table_exists(FEED_HEALTH):
        op.drop_table(FEED_HEALTH, schema="public")
