"""public schema: add chunking columns to tax_knowledge_document

Revision ID: 0007_tax_chunking
Revises: 0006_dim_swap
Create Date: 2026-05-29

WHY
---
Several authoritative tax documents (notably IRS Pub 946 on depreciation at
~498K chars and the 2024 Form 1120 instructions at ~221K chars) exceed
Infinity's 122,880-char per-request input limit. The original ingest pipeline
embedded only the first ~8000 chars of each document via the curator
(``run_curator.py``), which meant the long-tail content was invisible to
semantic search.

This migration adds two columns to allow a parent-child chunk hierarchy:

  parent_document_id UUID  — points at the original (full-content) row when
                              this row is a derived chunk. NULL on originals.
  chunk_index INT          — ordinal index (0-based) within the parent's
                              chunk sequence. NULL on originals, 0..N on chunks.

Derived chunks INHERIT the parent's identifying metadata
(jurisdiction, topic, tax_year, source_url, source_authority, title, version,
is_current, ingested_by) so existing retrieval filters (the WHERE clauses in
``app.tools.tax_get_current_rule.get_current_rule``) continue to work without
modification — chunks simply look like additional documents under the same
(jurisdiction, topic, tax_year) tuple.

A self-FK on parent_document_id (ON DELETE CASCADE) ensures chunks vanish if
the parent is ever hard-deleted (the table is append-only by policy, but the
constraint is good hygiene).

An index on (parent_document_id, chunk_index) supports both the chunk-write
path (avoid duplicate chunks for the same parent) and any future UI that
wants to reconstruct the full document by stitching chunks in order.

Append-only semantics are preserved. The chunker in
``app.exports.chunk_oversized_docs`` is idempotent: it skips parents that
already have any chunk rows.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# IMPORTANT: alembic_version_public.version_num is varchar(32).
# This id is 17 chars — well under the limit.
revision: str = "0007_tax_chunking"
down_revision: str | None = "0006_dim_swap"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tax_knowledge_document",
        sa.Column(
            "parent_document_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        schema="public",
    )
    op.add_column(
        "tax_knowledge_document",
        sa.Column(
            "chunk_index",
            sa.Integer(),
            nullable=True,
        ),
        schema="public",
    )

    # Self-FK so chunks cascade-delete with their parent (defensive — the table
    # is append-only by policy, but the constraint is cheap insurance).
    op.create_foreign_key(
        "tax_knowledge_document_parent_fk",
        source_table="tax_knowledge_document",
        referent_table="tax_knowledge_document",
        local_cols=["parent_document_id"],
        remote_cols=["id"],
        ondelete="CASCADE",
        source_schema="public",
        referent_schema="public",
    )

    # Index supports (a) chunker idempotency lookups and (b) stitching chunks
    # back together in order. Partial index — only chunk rows have non-NULL
    # parent_document_id.
    op.create_index(
        "tax_knowledge_parent_chunk_idx",
        "tax_knowledge_document",
        ["parent_document_id", "chunk_index"],
        schema="public",
        postgresql_where=sa.text("parent_document_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "tax_knowledge_parent_chunk_idx",
        table_name="tax_knowledge_document",
        schema="public",
    )
    op.drop_constraint(
        "tax_knowledge_document_parent_fk",
        "tax_knowledge_document",
        schema="public",
        type_="foreignkey",
    )
    op.drop_column("tax_knowledge_document", "chunk_index", schema="public")
    op.drop_column("tax_knowledge_document", "parent_document_id", schema="public")
