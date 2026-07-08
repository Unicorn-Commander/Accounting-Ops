"""MCP tool: get_current_rule — retrieve current authoritative tax rule from RAG index.

Used by all tax specialist agents to look up the latest authoritative text for a
given (jurisdiction, topic, tax_year) tuple.  The tool queries the
``tax_knowledge_document`` table in the public schema and, when a natural-language
``query`` is provided, performs a pgvector cosine-similarity search over the stored
embeddings to surface the most relevant document(s).

Read-only tool — no authentication required beyond an active session.  The table
grants SELECT to the ``accounting_ops_app`` runtime role (migration 0005).

Typical specialist usage:
    result = await get_current_rule(
        jurisdiction="US",
        topic="174",
        tax_year=2024,
        query="capitalization requirement for R&D expenditures under TCJA",
    )
    for doc in result["documents"]:
        print(doc["title"], doc["source_url"])
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import SessionLocal

logger = logging.getLogger(__name__)

# Maximum documents returned from a semantic search pass.
_DEFAULT_LIMIT = 3
# Cosine similarity threshold below which we discard results as noise.
_SIM_THRESHOLD = 0.25


async def get_current_rule(
    jurisdiction: str,
    topic: str,
    tax_year: int | None = None,
    query: str | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Retrieve the current authoritative tax rule for a (jurisdiction, topic, year).

    Searches the tax_knowledge_document table which is populated and kept current
    by the Tax Knowledge Curator agent (run weekly via ``make knowledge-refresh``).

    Args:
        jurisdiction: Two-letter jurisdiction code: 'US' for federal, 'SC', 'GA', etc.
                      for states, 'CFR' for Code of Federal Regulations, etc.
        topic:        Topic slug, e.g. '174', '163j', '1099_NEC', 'form_1120',
                      'sc1120', 'bonus_depreciation', 'tcja_obbba_tracker'.
        tax_year:     Tax year integer (e.g. 2024).  Pass None for evergreen rules
                      (e.g. foundational IRC section language that does not vary by year).
        query:        Optional natural-language query string.  When provided, performs
                      a pgvector cosine-similarity search within documents matching
                      the (jurisdiction, topic, tax_year) filter.  When omitted, returns
                      the most recently ingested current document(s) for the tuple.
        limit:        Maximum number of documents to return (default 3, max 10).

    Returns:
        {
          "jurisdiction": str,
          "topic": str,
          "tax_year": int | None,
          "query": str | None,
          "documents": [
            {
              "id": str,
              "title": str,
              "content": str,
              "source_url": str,
              "source_authority": str,
              "ingested_at": str (ISO 8601),
              "version": int,
              "similarity": float | None,   # present only when query was provided
            },
            ...
          ],
          "knowledge_gap": bool,   # True when no documents were found
          "gap_note": str | None,  # advice for the specialist when knowledge_gap=True
        }
    """
    limit = max(1, min(limit, 10))

    async with SessionLocal() as session:
        # ------------------------------------------------------------------
        # Branch A: semantic search with embedding
        # ------------------------------------------------------------------
        if query:
            embedding = await _get_embedding(query)
            if embedding is not None:
                vec_literal = "[" + ",".join(str(v) for v in embedding) + "]"
                if tax_year is not None:
                    sql = text(
                        """
                        SELECT
                            CAST(id AS text) AS id,
                            title,
                            content,
                            source_url,
                            source_authority,
                            ingested_at,
                            version,
                            1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                        FROM public.tax_knowledge_document
                        WHERE is_current = TRUE
                          AND jurisdiction = :jurisdiction
                          AND topic = :topic
                          AND (tax_year = :tax_year OR tax_year IS NULL)
                          AND embedding IS NOT NULL
                          AND 1 - (embedding <=> CAST(:vec AS vector)) > :threshold
                        ORDER BY embedding <=> CAST(:vec AS vector)
                        LIMIT :limit
                        """
                    )
                    params = {
                        "vec": vec_literal,
                        "jurisdiction": jurisdiction,
                        "topic": topic,
                        "tax_year": tax_year,
                        "threshold": _SIM_THRESHOLD,
                        "limit": limit,
                    }
                else:
                    sql = text(
                        """
                        SELECT
                            CAST(id AS text) AS id,
                            title,
                            content,
                            source_url,
                            source_authority,
                            ingested_at,
                            version,
                            1 - (embedding <=> CAST(:vec AS vector)) AS similarity
                        FROM public.tax_knowledge_document
                        WHERE is_current = TRUE
                          AND jurisdiction = :jurisdiction
                          AND topic = :topic
                          AND embedding IS NOT NULL
                          AND 1 - (embedding <=> CAST(:vec AS vector)) > :threshold
                        ORDER BY embedding <=> CAST(:vec AS vector)
                        LIMIT :limit
                        """
                    )
                    params = {
                        "vec": vec_literal,
                        "jurisdiction": jurisdiction,
                        "topic": topic,
                        "threshold": _SIM_THRESHOLD,
                        "limit": limit,
                    }

                rows = (await session.execute(sql, params)).mappings().all()
                documents = [_format_row(row, include_similarity=True) for row in rows]

                # Fall back to exact-match if semantic returned nothing
                if not documents:
                    documents = await _exact_match(
                        session, jurisdiction, topic, tax_year, limit
                    )
            else:
                # Embedding unavailable (no API key or error) — fall through to exact
                logger.warning(
                    "get_current_rule: embedding unavailable for query=%r — "
                    "falling back to exact-match",
                    query[:80],
                )
                documents = await _exact_match(
                    session, jurisdiction, topic, tax_year, limit
                )

        # ------------------------------------------------------------------
        # Branch B: exact-match lookup (no query)
        # ------------------------------------------------------------------
        else:
            documents = await _exact_match(
                session, jurisdiction, topic, tax_year, limit
            )

    knowledge_gap = len(documents) == 0
    gap_note: str | None = None
    if knowledge_gap:
        gap_note = (
            f"No current documents found for jurisdiction={jurisdiction!r}, "
            f"topic={topic!r}, tax_year={tax_year!r}. "
            "The Tax Knowledge Curator has not yet ingested this rule, or the "
            "topic slug may not match the ingestion_sources.yaml definition. "
            "Flag for CPA review using flag_for_cpa_review with a "
            "'knowledge gap' note so the CPA can provide the authoritative source."
        )

    return {
        "jurisdiction": jurisdiction,
        "topic": topic,
        "tax_year": tax_year,
        "query": query,
        "documents": documents,
        "knowledge_gap": knowledge_gap,
        "gap_note": gap_note,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _exact_match(
    session: Any,
    jurisdiction: str,
    topic: str,
    tax_year: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    if tax_year is not None:
        sql = text(
            """
            SELECT
                CAST(id AS text) AS id,
                title,
                content,
                source_url,
                source_authority,
                ingested_at,
                version
            FROM public.tax_knowledge_document
            WHERE is_current = TRUE
              AND jurisdiction = :jurisdiction
              AND topic = :topic
              AND (tax_year = :tax_year OR tax_year IS NULL)
            ORDER BY version DESC, ingested_at DESC
            LIMIT :limit
            """
        )
        params = {
            "jurisdiction": jurisdiction,
            "topic": topic,
            "tax_year": tax_year,
            "limit": limit,
        }
    else:
        sql = text(
            """
            SELECT
                CAST(id AS text) AS id,
                title,
                content,
                source_url,
                source_authority,
                ingested_at,
                version
            FROM public.tax_knowledge_document
            WHERE is_current = TRUE
              AND jurisdiction = :jurisdiction
              AND topic = :topic
            ORDER BY version DESC, ingested_at DESC
            LIMIT :limit
            """
        )
        params = {
            "jurisdiction": jurisdiction,
            "topic": topic,
            "limit": limit,
        }

    rows = (await session.execute(sql, params)).mappings().all()
    return [_format_row(row, include_similarity=False) for row in rows]


def _format_row(row: Any, *, include_similarity: bool) -> dict[str, Any]:
    ingested_at = row["ingested_at"]
    if isinstance(ingested_at, datetime):
        ingested_at_str = ingested_at.isoformat()
    else:
        ingested_at_str = str(ingested_at) if ingested_at else None

    doc: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "content": row["content"],
        "source_url": row["source_url"],
        "source_authority": row["source_authority"],
        "ingested_at": ingested_at_str,
        "version": row["version"],
    }
    if include_similarity:
        sim = row.get("similarity")
        doc["similarity"] = float(sim) if sim is not None else None
    return doc


async def _get_embedding(text_input: str) -> list[float] | None:
    """Get embedding for text; returns None if unavailable (key missing, API error)."""
    try:
        from app.exports.tax_knowledge_embed import embed_text
        return await embed_text(text_input)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_current_rule: embedding failed: %s", exc)
        return None
