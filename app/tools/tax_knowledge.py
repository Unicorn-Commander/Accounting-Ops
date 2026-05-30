"""MCP read tools for the Tax Knowledge browser.

These tools wrap the public ``tax_knowledge_document`` index (curated weekly).
They are non-tenant-scoped — the dataset is publicly-sourced IRS / state
authoritative text — but still require an authenticated MCP caller (the
MCPAuthMiddleware enforces that at the transport layer).

Tools:
  - search_tax_knowledge      — faceted + semantic search over documents
  - list_tax_knowledge_topics — faceted summary for browse UIs
  - get_tax_knowledge_doc     — single document with full content

The complementary ``get_current_rule`` tool (already registered) is the
"specialist-focused" semantic-lookup tool for (jurisdiction, topic, year);
``search_tax_knowledge`` here is the "exploration" variant matching the
browser UI.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import current_auth


def _require_caller() -> None:
    """Make sure an MCP caller is bound. Mirrors the HTTP router dependency."""
    if current_auth() is None:
        raise PermissionError("No authenticated caller for this MCP request")


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
    keys = row.keys() if hasattr(row, "keys") else []
    if "similarity" in keys:
        sim = row["similarity"]
        doc["similarity"] = float(sim) if sim is not None else None
    return doc


async def list_tax_knowledge_topics() -> dict[str, Any]:
    """Faceted summary of available jurisdictions, topics, and tax years.

    Mirrors GET /api/tax-knowledge/topics. Useful for a browser-style agent
    that needs to populate filter facets before issuing a search.

    Returns:
        {
          "jurisdictions": [{"jurisdiction": "US", "doc_count": int}, ...],
          "topics":         [{"jurisdiction": ..., "topic": ..., "doc_count": int}, ...],
          "tax_years":      [2025, 2024, ...],
          "total_documents": int,
        }

    Role floor: any authenticated caller (the dataset is shared).
    """
    _require_caller()
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
                        "SELECT count(*) FROM public.tax_knowledge_document "
                        "WHERE is_current = TRUE"
                    )
                )
            ).scalar_one()
        except Exception:
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


async def search_tax_knowledge(
    jurisdiction: str | None = None,
    topic: str | None = None,
    tax_year: int | None = None,
    query: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Search the curated tax knowledge index with optional facets and a query.

    Mirrors GET /api/tax-knowledge/documents. When a ``query`` is provided and
    embeddings are available, results are ordered by cosine similarity;
    otherwise ordered by version DESC, ingested_at DESC. Plain ILIKE keyword
    search is the fallback when embeddings are missing.

    Args:
        jurisdiction: 'US' | 'SC' | etc. (optional facet).
        topic: '174' | '163j' | '1099_NEC' | ... (optional facet).
        tax_year: Specific tax year (optional facet).
        query: Optional natural-language query (semantic if embeddings present).
        limit: Page size (1-100). Default 20.
        offset: Page offset.

    Returns:
        {"items": [...], "total": int, "search_mode": "semantic"|"keyword"|"browse",
         "query": str|null}

    Role floor: any authenticated caller.
    """
    _require_caller()

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    async with SessionLocal() as session:
        # Try semantic first if a query is provided.
        if query and query.strip():
            try:
                from app.exports.tax_knowledge_embed import embed_text
                vec = await embed_text(query.strip())
            except Exception:
                vec = None

            if vec is not None:
                where = ["is_current = TRUE"]
                params: dict[str, Any] = {
                    "limit": limit, "offset": offset, "qvec": str(vec),
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
                                    id, jurisdiction, topic, tax_year, title,
                                    source_url, source_authority, version,
                                    is_current, ingested_at,
                                    1 - (embedding <=> :qvec::vector) AS similarity
                                FROM public.tax_knowledge_document
                                WHERE {where_sql} AND embedding IS NOT NULL
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
                                SELECT count(*) FROM public.tax_knowledge_document
                                WHERE {where_sql} AND embedding IS NOT NULL
                                """
                            ),
                            {k: v for k, v in params.items()
                             if k not in ("limit", "offset", "qvec")},
                        )
                    ).scalar_one()
                    return {
                        "items": [_format_row(r) for r in rows],
                        "total": int(total),
                        "search_mode": "semantic",
                        "query": query,
                    }
                except Exception:
                    pass  # fall through to keyword

        # Plain faceted / keyword fallback.
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
            where.append("(title ILIKE :q OR content ILIKE :q OR topic ILIKE :q)")
            params["q"] = f"%{query.strip()}%"
        where_sql = " AND ".join(where)

        try:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT id, jurisdiction, topic, tax_year, title,
                               source_url, source_authority, version,
                               is_current, ingested_at
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
                        f"SELECT count(*) FROM public.tax_knowledge_document "
                        f"WHERE {where_sql}"
                    ),
                    {k: v for k, v in params.items() if k not in ("limit", "offset")},
                )
            ).scalar_one()
        except Exception:
            return {
                "items": [], "total": 0,
                "search_mode": "unavailable",
                "query": query, "error": "knowledge_index_unavailable",
            }

    return {
        "items": [_format_row(r) for r in rows],
        "total": int(total),
        "search_mode": "keyword" if (query and query.strip()) else "browse",
        "query": query,
    }


async def get_tax_knowledge_doc(
    document_id: UUID,
) -> dict[str, Any]:
    """Fetch a single tax-knowledge document by id, with full content.

    Mirrors GET /api/tax-knowledge/documents/{id}.

    Args:
        document_id: tax_knowledge_document UUID.

    Returns the full document including ``content``.

    Role floor: any authenticated caller.
    """
    _require_caller()
    async with SessionLocal() as session:
        try:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT id, jurisdiction, topic, tax_year, title,
                               content, source_url, source_authority,
                               version, is_current, ingested_at
                        FROM public.tax_knowledge_document
                        WHERE id = :id
                        """
                    ),
                    {"id": str(document_id)},
                )
            ).mappings().first()
        except Exception as exc:
            raise RuntimeError("knowledge_index_unavailable") from exc

    if row is None:
        raise LookupError(f"Document {document_id} not found")
    return _format_row(row, include_content=True)
