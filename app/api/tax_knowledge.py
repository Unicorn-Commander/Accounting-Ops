"""Tax knowledge browser: read-only HTTP surface over public.tax_knowledge_document.

The data is populated by the Tax Knowledge Curator (run weekly via
`make knowledge-refresh`). These routes let the WebGUI's Knowledge Browser
search and read the same authoritative documents the MCP tool
`tax_get_current_rule` uses internally.

Routes:
    GET /api/tax-knowledge/topics
        - Faceted distinct list of jurisdictions, topics, and tax years.
          Used by the browser to populate filter sidebars.
          Public (any authenticated user).

    GET /api/tax-knowledge/documents
        ?jurisdiction=US&topic=174&tax_year=2024&query=...&limit=20&offset=0
        - List documents matching the filters. If `query` is provided and
          embeddings are available, results are similarity-ordered; otherwise
          ordered by version DESC, ingested_at DESC.
          Public (any authenticated user).

    GET /api/tax-knowledge/documents/{id}
        - Single document by UUID, full content.
          Public (any authenticated user).

Role floor: any authenticated user (the dataset is non-customer-specific —
publicly-sourced IRS / state authoritative text). No workspace scoping; this
is shared knowledge.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.db import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/tax-knowledge",
    tags=["tax-knowledge"],
    dependencies=[Depends(validate_bearer_token)],
)


def _format_row(row: Any, *, include_content: bool = False) -> dict[str, Any]:
    ingested_at = row["ingested_at"]
    if isinstance(ingested_at, datetime):
        ingested_at_str = ingested_at.isoformat()
    else:
        ingested_at_str = str(ingested_at) if ingested_at else None
    doc: dict[str, Any] = {
        "id": str(row["id"]),
        "jurisdiction": row["jurisdiction"],
        "topic": row["topic"],
        "tax_year": row["tax_year"],
        "title": row["title"],
        "source_url": row["source_url"],
        "source_authority": row["source_authority"],
        "version": row["version"],
        "is_current": row.get("is_current", True),
        "ingested_at": ingested_at_str,
    }
    if include_content:
        doc["content"] = row.get("content")
    if "similarity" in row.keys():
        sim = row["similarity"]
        doc["similarity"] = float(sim) if sim is not None else None
    return doc


@router.get("/topics")
async def list_topics(
    _caller: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Faceted summary of available jurisdictions, topics, and tax years."""
    async with SessionLocal() as session:
        try:
            jurisdictions = (
                await session.execute(
                    text(
                        """
                        SELECT jurisdiction, count(*) AS doc_count
                        FROM public.tax_knowledge_document
                        WHERE is_current = TRUE
                        GROUP BY jurisdiction
                        ORDER BY jurisdiction
                        """
                    )
                )
            ).mappings().all()

            topics = (
                await session.execute(
                    text(
                        """
                        SELECT jurisdiction, topic, count(*) AS doc_count
                        FROM public.tax_knowledge_document
                        WHERE is_current = TRUE
                        GROUP BY jurisdiction, topic
                        ORDER BY jurisdiction, topic
                        """
                    )
                )
            ).mappings().all()

            tax_years = (
                await session.execute(
                    text(
                        """
                        SELECT DISTINCT tax_year
                        FROM public.tax_knowledge_document
                        WHERE is_current = TRUE AND tax_year IS NOT NULL
                        ORDER BY tax_year DESC
                        """
                    )
                )
            ).all()

            total = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM public.tax_knowledge_document WHERE is_current = TRUE"
                    )
                )
            ).scalar_one()
        except Exception as exc:
            logger.exception("Failed to query tax knowledge topics: %s", exc)
            # Soft-fail with empty facets rather than 500ing — page can still render.
            return {
                "jurisdictions": [],
                "topics": [],
                "tax_years": [],
                "total_documents": 0,
                "error": "knowledge_index_unavailable",
            }

    return {
        "jurisdictions": [dict(r) for r in jurisdictions],
        "topics": [dict(r) for r in topics],
        "tax_years": [r[0] for r in tax_years],
        "total_documents": int(total),
    }


@router.get("/documents")
async def list_documents(
    jurisdiction: str | None = Query(None, max_length=8),
    topic: str | None = Query(None, max_length=128),
    tax_year: int | None = Query(None, ge=1900, le=2100),
    query: str | None = Query(None, max_length=512),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _caller: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """List documents with optional faceted filters and natural-language query."""
    async with SessionLocal() as session:
        # Try semantic search if a query string is provided AND embeddings work.
        if query and query.strip():
            try:
                from app.exports.tax_knowledge_embed import embed_text
                vec = await embed_text(query.strip())
            except Exception as exc:  # noqa: BLE001
                logger.info("knowledge search: embedding unavailable: %s", exc)
                vec = None

            if vec is not None:
                # Cosine similarity search with optional facet filters.
                where = ["is_current = TRUE"]
                params: dict[str, Any] = {
                    "limit": limit,
                    "offset": offset,
                    "qvec": str(vec),
                }
                if jurisdiction:
                    where.append("jurisdiction = :jurisdiction")
                    params["jurisdiction"] = jurisdiction
                if topic:
                    where.append("topic = :topic")
                    params["topic"] = topic
                if tax_year is not None:
                    where.append("(tax_year = :tax_year OR tax_year IS NULL)")
                    params["tax_year"] = tax_year
                where_sql = " AND ".join(where)
                try:
                    rows = (
                        await session.execute(
                            text(
                                f"""
                                SELECT
                                    id,
                                    jurisdiction,
                                    topic,
                                    tax_year,
                                    title,
                                    source_url,
                                    source_authority,
                                    version,
                                    is_current,
                                    ingested_at,
                                    1 - (embedding <=> :qvec::vector) AS similarity
                                FROM public.tax_knowledge_document
                                WHERE {where_sql}
                                  AND embedding IS NOT NULL
                                ORDER BY embedding <=> :qvec::vector
                                LIMIT :limit OFFSET :offset
                                """
                            ),
                            params,
                        )
                    ).mappings().all()
                    total = (
                        await session.execute(
                            text(
                                f"""
                                SELECT count(*)
                                FROM public.tax_knowledge_document
                                WHERE {where_sql} AND embedding IS NOT NULL
                                """
                            ),
                            {k: v for k, v in params.items() if k not in ("limit", "offset", "qvec")},
                        )
                    ).scalar_one()
                    return {
                        "items": [_format_row(r) for r in rows],
                        "total": int(total),
                        "search_mode": "semantic",
                        "query": query,
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.info(
                        "knowledge search: semantic search failed, falling back to keyword: %s",
                        exc,
                    )
                    # Fall through to keyword search below.

        # Plain faceted / keyword search (no embeddings or fallback).
        where = ["is_current = TRUE"]
        params = {"limit": limit, "offset": offset}
        if jurisdiction:
            where.append("jurisdiction = :jurisdiction")
            params["jurisdiction"] = jurisdiction
        if topic:
            where.append("topic = :topic")
            params["topic"] = topic
        if tax_year is not None:
            where.append("(tax_year = :tax_year OR tax_year IS NULL)")
            params["tax_year"] = tax_year
        if query and query.strip():
            where.append(
                "(title ILIKE :q OR content ILIKE :q OR topic ILIKE :q)"
            )
            params["q"] = f"%{query.strip()}%"
        where_sql = " AND ".join(where)

        try:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT
                            id,
                            jurisdiction,
                            topic,
                            tax_year,
                            title,
                            source_url,
                            source_authority,
                            version,
                            is_current,
                            ingested_at
                        FROM public.tax_knowledge_document
                        WHERE {where_sql}
                        ORDER BY version DESC, ingested_at DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            ).mappings().all()
            total = (
                await session.execute(
                    text(
                        f"SELECT count(*) FROM public.tax_knowledge_document WHERE {where_sql}"
                    ),
                    {k: v for k, v in params.items() if k not in ("limit", "offset")},
                )
            ).scalar_one()
        except Exception as exc:
            logger.exception("knowledge search: faceted query failed: %s", exc)
            return {
                "items": [],
                "total": 0,
                "search_mode": "unavailable",
                "query": query,
                "error": "knowledge_index_unavailable",
            }

    return {
        "items": [_format_row(r) for r in rows],
        "total": int(total),
        "search_mode": "keyword" if (query and query.strip()) else "browse",
        "query": query,
    }


@router.get("/documents/{document_id}")
async def get_document(
    document_id: UUID,
    _caller: AuthContext = Depends(validate_bearer_token),
) -> dict[str, Any]:
    """Single document by UUID, with full content."""
    async with SessionLocal() as session:
        try:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT
                            id,
                            jurisdiction,
                            topic,
                            tax_year,
                            title,
                            content,
                            source_url,
                            source_authority,
                            version,
                            is_current,
                            ingested_at
                        FROM public.tax_knowledge_document
                        WHERE id = :id
                        """
                    ),
                    {"id": str(document_id)},
                )
            ).mappings().first()
        except Exception as exc:
            logger.exception("knowledge document fetch failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge_index_unavailable",
            ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    return _format_row(row, include_content=True)
