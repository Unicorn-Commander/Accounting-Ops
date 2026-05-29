"""public schema: tax knowledge base + pgvector RAG index

Revision ID: 0005_tax_knowledge_base
Revises: 0004_worker_runtime_grants
Create Date: 2026-05-29

ARCHITECTURE NOTE
-----------------
This table lives in the PUBLIC schema because it holds global authoritative
knowledge (federal + state tax law), NOT per-tenant data.  Every workspace
queries it at runtime via the `get_current_rule` MCP tool.

pgvector extension
~~~~~~~~~~~~~~~~~~
Requires the pgvector extension.  The postgres image in docker-compose.yml
uses ``postgres:16`` which does NOT ship with pgvector.  Before running this
migration you must switch to the pgvector-bundled image:

    image: pgvector/pgvector:pg16

Or install the extension manually on the running container:

    apt-get install -y postgresql-16-pgvector

The ``CREATE EXTENSION IF NOT EXISTS vector`` below is idempotent; if the
extension is already installed it is a no-op.

Append-only semantics
~~~~~~~~~~~~~~~~~~~~~
Documents are NEVER hard-deleted.  To supersede a document:
  UPDATE tax_knowledge_document
     SET is_current = FALSE, superseded_at = now()
   WHERE jurisdiction = $1 AND topic = $2 AND tax_year = $3 AND is_current = TRUE;

Then insert the new version with version = old_version + 1, is_current = TRUE.

The ``accounting_ops_app`` runtime role gets SELECT + INSERT only.  No UPDATE
or DELETE — the curator runs as the OWNER role (accounting_ops / admin DB URL).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_tax_knowledge_base"
down_revision: str | None = "0004_worker_runtime_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # pgvector extension — idempotent, required for the embedding column
    # ------------------------------------------------------------------
    bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))

    # ------------------------------------------------------------------
    # Main table
    # ------------------------------------------------------------------
    op.create_table(
        "tax_knowledge_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("jurisdiction", sa.Text(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_authority", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default="TRUE"),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("ingested_by", sa.Text(), nullable=False),
        sa.Column("digest_sha256", sa.Text(), nullable=False),
        # vector(1536) = text-embedding-3-small dimensionality
        sa.Column("embedding", sa.Text(), nullable=True),   # stored as text initially; see note below
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        schema="public",
    )

    # Alter the embedding column to use the actual vector type now that the
    # extension is installed.  We create it as TEXT first so SQLAlchemy's
    # DDL doesn't need a custom type object, then ALTER it to vector(1536).
    bind.execute(sa.text(
        "ALTER TABLE public.tax_knowledge_document "
        "ALTER COLUMN embedding TYPE vector(1536) "
        "USING NULL::vector(1536)"
    ))

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    # Lookup index for exact-match queries by (jurisdiction, topic, tax_year, is_current)
    op.create_index(
        "tax_knowledge_lookup",
        "tax_knowledge_document",
        ["jurisdiction", "topic", "tax_year", "is_current"],
        schema="public",
    )

    # IVFFlat index for ANN cosine similarity search.
    # lists=100 is appropriate for an initial corpus up to ~1M vectors.
    # NOTE: IVFFlat requires at least one row before building efficiently;
    # the index is created empty here and will be effective once the baseline
    # ingestion run has populated the table.
    bind.execute(sa.text(
        "CREATE INDEX tax_knowledge_embedding_ivfflat "
        "ON public.tax_knowledge_document "
        "USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    ))

    # ------------------------------------------------------------------
    # Runtime grants — SELECT + INSERT for the app role only.
    # The curator (run_curator.py) must connect as the OWNER role
    # (ADMIN_DATABASE_URL) to perform UPDATEs (supersede old documents).
    # ------------------------------------------------------------------
    bind.execute(sa.text(
        "GRANT SELECT, INSERT ON public.tax_knowledge_document "
        "TO accounting_ops_app"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "REVOKE SELECT, INSERT ON public.tax_knowledge_document "
        "FROM accounting_ops_app"
    ))
    op.drop_index("tax_knowledge_embedding_ivfflat",
                  table_name="tax_knowledge_document", schema="public")
    op.drop_index("tax_knowledge_lookup",
                  table_name="tax_knowledge_document", schema="public")
    op.drop_table("tax_knowledge_document", schema="public")
    # NOTE: We intentionally do NOT drop the vector extension here because
    # other tables may depend on it after this migration runs.
