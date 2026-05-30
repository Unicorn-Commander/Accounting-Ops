"""public schema: swap tax_knowledge_document embedding column to vector(1024)

Revision ID: 0006_tax_knowledge_embedding_dim_swap
Revises: 0005_tax_knowledge_base
Create Date: 2026-05-29

WHY
---
Migration 0005 created the embedding column as vector(1536) to match the
text-embedding-3-small model served via OpenRouter.  Switching to self-hosted
Infinity with BAAI/bge-large-en-v1.5 (or mixedbread-ai/mxbai-embed-large-v1)
yields 1024-dimensional vectors — pgvector cannot store mismatched dimensions
in the same column, so we must ALTER the type.

pgvector does NOT support ALTER COLUMN … TYPE on a vector column that contains
non-NULL data.  The safest approach is:
  1. NULL out all embedding values (we will re-embed via the curator anyway).
  2. DROP the IVFFlat index (it is tied to the old dimension).
  3. ALTER COLUMN embedding TYPE vector(1024).
  4. Recreate the IVFFlat index on the new dimension.

Re-embedding:
  After running this migration, start the Infinity service and run the curator:

    docker compose --profile embeddings up -d infinity
    docker compose run --rm -e DATABASE_URL="$ADMIN_DATABASE_URL" app \\
        alembic -c alembic-public.ini upgrade head
    docker exec -e EMBED_BASE_URL=http://infinity:7997 accounting_ops-app-1 \\
        python /app/agents/tax_research/knowledge_curator/run_curator.py

  The curator will re-embed all is_current=TRUE documents via Infinity and
  store 1024-dim vectors in the column.

Downgrade:
  Reverting restores vector(1536) and NULLs embeddings again.  Re-embed via
  OpenRouter after downgrade:
    OPENROUTER_API_KEY=<key> EMBED_BASE_URL=https://openrouter.ai/api/v1 \\
    docker exec accounting_ops-app-1 python /app/agents/...run_curator.py
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_dim_swap"
down_revision: str | None = "0005_tax_knowledge_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. NULL out all embeddings — pgvector cannot retype a column with data
    bind.execute(sa.text(
        "UPDATE public.tax_knowledge_document SET embedding = NULL"
    ))

    # 2. Drop the IVFFlat index — it is dimension-specific (vector_cosine_ops
    #    compiles the dimension into the index metadata)
    bind.execute(sa.text(
        "DROP INDEX IF EXISTS public.tax_knowledge_embedding_ivfflat"
    ))

    # 3. Alter column to vector(1024)
    #    We go through TEXT as an intermediate to satisfy pgvector's ALTER rules:
    #    NULL → TEXT → vector(1024) is safe because all values are NULL.
    bind.execute(sa.text(
        "ALTER TABLE public.tax_knowledge_document "
        "ALTER COLUMN embedding TYPE text "
        "USING NULL::text"
    ))
    bind.execute(sa.text(
        "ALTER TABLE public.tax_knowledge_document "
        "ALTER COLUMN embedding TYPE vector(1024) "
        "USING NULL::vector(1024)"
    ))

    # 4. Recreate IVFFlat index on vector(1024)
    bind.execute(sa.text(
        "CREATE INDEX tax_knowledge_embedding_ivfflat "
        "ON public.tax_knowledge_document "
        "USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    ))


def downgrade() -> None:
    bind = op.get_bind()

    # NULL embeddings (1024-dim vectors can't cast to 1536)
    bind.execute(sa.text(
        "UPDATE public.tax_knowledge_document SET embedding = NULL"
    ))

    bind.execute(sa.text(
        "DROP INDEX IF EXISTS public.tax_knowledge_embedding_ivfflat"
    ))

    # Restore to 1536-dim for OpenRouter text-embedding-3-small
    bind.execute(sa.text(
        "ALTER TABLE public.tax_knowledge_document "
        "ALTER COLUMN embedding TYPE text "
        "USING NULL::text"
    ))
    bind.execute(sa.text(
        "ALTER TABLE public.tax_knowledge_document "
        "ALTER COLUMN embedding TYPE vector(1536) "
        "USING NULL::vector(1536)"
    ))

    bind.execute(sa.text(
        "CREATE INDEX tax_knowledge_embedding_ivfflat "
        "ON public.tax_knowledge_document "
        "USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    ))
