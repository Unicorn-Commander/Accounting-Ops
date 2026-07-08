"""public schema: regulatory feed cache.

Revision ID: 0009_regulatory_feed
Revises: 0008_rbac_expansion
Create Date: 2026-05-30

Caches polled regulatory feed entries (IRS Newsroom RSS, SCDOR notices sitemap
diff, Treasury Federal Register, etc.). One row per (source, external_id).

Lives in PUBLIC because the feed is global — every workspace sees the same
IRS notices. Per-workspace relevance classification lives in
sentinel_finding (per-tenant).

Grants:
  SELECT to accounting_ops_app   (Sentinel reads to diff)
  INSERT, UPDATE to accounting_ops_app  (Sentinel adds new entries + marks classified)

Idempotent + re-runnable.

Audit 2026-05-31 Bug 5: renumbered 0008 -> 0009 to resolve two-heads ambiguity
on ``alembic -c alembic-public.ini heads``. The other 0008 (rbac_expansion)
landed first chronologically, so this becomes 0009 with down_revision
"0008_rbac_expansion". the production database already applied this migration as
0009_regulatory_feed (it was renumbered there earlier by the live-deploy step);
fresh deploys + drift checks now match.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_regulatory_feed"
down_revision: str | None = "0008_rbac_expansion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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


def _index_exists(index: str) -> bool:
    bind = op.get_bind()
    return bool(
        bind.execute(
            sa.text(
                """
                SELECT 1 FROM pg_indexes
                WHERE schemaname = 'public' AND indexname = :index
                """
            ),
            {"index": index},
        ).first()
    )


def upgrade() -> None:
    if not _table_exists("regulatory_feed_entry"):
        op.create_table(
            "regulatory_feed_entry",
            sa.Column(
                "id",
                sa.UUID(),
                nullable=False,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "source",
                sa.Text(),
                nullable=False,
                comment=(
                    "Feed source slug: 'irs_newsroom_rss' | 'scdor_notices' | "
                    "'treasury_fed_register' | 'sba_grants'."
                ),
            ),
            sa.Column(
                "external_id",
                sa.Text(),
                nullable=False,
                comment="Stable id from the feed (URL or guid).",
            ),
            sa.Column(
                "title",
                sa.Text(),
                nullable=False,
            ),
            sa.Column(
                "summary",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "url",
                sa.Text(),
                nullable=True,
            ),
            sa.Column(
                "jurisdiction",
                sa.Text(),
                nullable=True,
                comment="'US' | 'SC' | 'GA' | etc.",
            ),
            sa.Column(
                "tags",
                postgresql.JSONB(),
                nullable=True,
                comment="['c_corp', 'estimated_tax', 'sec_174', …]",
            ),
            sa.Column(
                "published_at",
                sa.TIMESTAMP(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "first_seen_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "classified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
                comment=(
                    "True once a relevance classifier has assigned tags/"
                    "jurisdiction and decided whether to surface per-workspace."
                ),
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("source", "external_id", name="regulatory_feed_uq"),
            schema="public",
        )

    if not _index_exists("ix_regulatory_feed_unclassified"):
        op.execute(
            "CREATE INDEX ix_regulatory_feed_unclassified "
            "ON public.regulatory_feed_entry (classified, first_seen_at DESC) "
            "WHERE classified = false"
        )
    if not _index_exists("ix_regulatory_feed_source_published"):
        op.execute(
            "CREATE INDEX ix_regulatory_feed_source_published "
            "ON public.regulatory_feed_entry (source, published_at DESC NULLS LAST)"
        )

    # Grants — runtime role reads+inserts+updates, never deletes.
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON public.regulatory_feed_entry TO accounting_ops_app"
    )
    op.execute(
        "REVOKE DELETE, TRUNCATE ON public.regulatory_feed_entry FROM accounting_ops_app"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.ix_regulatory_feed_source_published")
    op.execute("DROP INDEX IF EXISTS public.ix_regulatory_feed_unclassified")
    if _table_exists("regulatory_feed_entry"):
        op.drop_table("regulatory_feed_entry", schema="public")
