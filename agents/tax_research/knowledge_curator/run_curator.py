#!/usr/bin/env python3
"""Tax Knowledge Curator — CLI ingestion runner.

Reads ingestion_sources.yaml, fetches each source, diffs against the current
row in tax_knowledge_document, inserts new versions where content has changed,
generates embeddings, and emits a Markdown digest report.

Usage:
    # From inside the running app container:
    python /app/agents/tax_research/knowledge_curator/run_curator.py

    # With output to a file:
    python /app/agents/tax_research/knowledge_curator/run_curator.py \\
        --output /drafts/curator-digests/

    # Process one specific source by name:
    python /app/agents/tax_research/knowledge_curator/run_curator.py \\
        --only "IRS Form 1120 instructions"

    # Dry-run (fetch + diff but don't write to DB):
    python /app/agents/tax_research/knowledge_curator/run_curator.py --dry-run

Environment variables required:
    DATABASE_URL or ADMIN_DATABASE_URL — must be the OWNER role (accounting_ops)
        because we UPDATE (supersede) existing rows.  The runtime
        accounting_ops_app role has SELECT+INSERT only (see migration 0005).
    OPENROUTER_API_KEY — for embedding generation.

Idempotency:
    Re-running on unchanged sources produces zero new rows.
    The SHA-256 digest of each fetched document is compared against the stored
    `digest_sha256`.  If identical, the source is logged as 'skipped'.

Append-only semantics:
    We never DELETE rows.  To supersede a document we:
      1. UPDATE the old row: is_current=FALSE, superseded_at=now()
      2. INSERT a new row: version=old+1, is_current=TRUE

    The UPDATE requires the OWNER role (ADMIN_DATABASE_URL).  The INSERT is
    permitted to the app role but we use the owner connection here for the
    paired operation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure /app (or equivalent project root) is on sys.path so `from app.exports
# import embed_text` resolves regardless of cwd.  Without this, running the
# curator from any directory other than /app silently degrades to embedding=None
# (the URL-refresh sub-agent ran into this 2026-05-29 and lost embeddings on 7
# newly-curated docs until re-run with explicit PYTHONPATH=/app).
_CURATOR_FILE = Path(__file__).resolve()
# .../{root}/agents/tax_research/knowledge_curator/run_curator.py  → root is parents[3]
_PROJECT_ROOT = _CURATOR_FILE.parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import httpx
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tax-knowledge-curator")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_CURATOR_DIR = Path(__file__).parent
_SOURCES_FILE = _CURATOR_DIR / "ingestion_sources.yaml"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_INGESTED_BY = "tax-knowledge-curator"
_USER_AGENT = (
    "Accounting-Ops-TaxCurator/1.0 "
    "(aaron@magicunicorn.tech; non-commercial research)"
)
_REQUEST_DELAY_S = 2.0   # polite delay between fetches
_FETCH_TIMEOUT_S = 30.0

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def _fetch_url(url: str, client: httpx.AsyncClient) -> tuple[str | None, str | None]:
    """Fetch a URL and return (text_content, error_message).

    For HTML pages: returns the raw HTML (caller extracts text).
    For PDF URLs: returns raw bytes decoded as latin-1 (for hashing purposes;
    embedding will use extracted text from pypdf if available).
    """
    headers = {"User-Agent": _USER_AGENT}
    try:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            return _extract_pdf_text(resp.content), None
        return resp.text, None
    except httpx.HTTPStatusError as exc:
        return None, f"HTTP {exc.response.status_code}"
    except httpx.TimeoutException:
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)[:120]


def _extract_pdf_text(raw: bytes) -> str:
    """Extract text from PDF bytes using pypdf (if available) or fall back to raw."""
    try:
        import io
        import pypdf  # optional dependency

        reader = pypdf.PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                parts.append(text.strip())
        return "\n\n".join(parts)
    except ImportError:
        logger.warning(
            "pypdf not installed — PDF text extraction unavailable. "
            "Install pypdf for better PDF handling: pip install pypdf"
        )
        # Fall back: decode raw bytes (mostly binary noise, but gives a stable hash)
        return raw.decode("latin-1", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF extraction error: %s", exc)
        return raw.decode("latin-1", errors="replace")


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text using html2text or trafilatura (if available)."""
    try:
        import trafilatura
        text = trafilatura.extract(html, include_links=False, include_images=False)
        if text and len(text.strip()) > 100:
            return text.strip()
    except ImportError:
        pass

    try:
        import html2text as h2t
        h = h2t.HTML2Text()
        h.ignore_links = True
        h.ignore_images = True
        h.body_width = 0
        return h.handle(html).strip()
    except ImportError:
        pass

    # Minimal fallback: strip tags
    import re
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_admin_db_url() -> str:
    """Get the OWNER-role database URL for curator writes."""
    url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "Neither ADMIN_DATABASE_URL nor DATABASE_URL is set. "
            "The curator requires the OWNER role DB URL to supersede documents."
        )
    # Convert asyncpg URL to sync psycopg2 for simplicity in the CLI script
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _get_current_doc(
    conn: Any, jurisdiction: str, topic: str, tax_year: int | None
) -> dict | None:
    """Return the current row for (jurisdiction, topic, tax_year), or None."""
    from sqlalchemy import text

    if tax_year is not None:
        result = conn.execute(
            text(
                """
                SELECT CAST(id AS text) AS id, version, digest_sha256
                FROM public.tax_knowledge_document
                WHERE jurisdiction = :j AND topic = :t AND tax_year = :y
                  AND is_current = TRUE
                LIMIT 1
                """
            ),
            {"j": jurisdiction, "t": topic, "y": tax_year},
        ).mappings().first()
    else:
        result = conn.execute(
            text(
                """
                SELECT CAST(id AS text) AS id, version, digest_sha256
                FROM public.tax_knowledge_document
                WHERE jurisdiction = :j AND topic = :t AND tax_year IS NULL
                  AND is_current = TRUE
                LIMIT 1
                """
            ),
            {"j": jurisdiction, "t": topic},
        ).mappings().first()

    return dict(result) if result else None


async def _supersede_and_insert(
    conn: Any,
    source: dict[str, Any],
    content: str,
    digest: str,
    old_doc: dict | None,
    embedding: list[float] | None,
    dry_run: bool,
) -> int:
    """Supersede old row (if any) and insert new row. Returns new version number."""
    from sqlalchemy import text

    new_version = (old_doc["version"] + 1) if old_doc else 1
    now = datetime.now(timezone.utc)

    if dry_run:
        logger.info(
            "[DRY-RUN] Would ingest: %s v%d (old: %s)",
            source["name"],
            new_version,
            old_doc["id"] if old_doc else "none",
        )
        return new_version

    # Supersede old
    if old_doc:
        conn.execute(
            text(
                """
                UPDATE public.tax_knowledge_document
                SET is_current = FALSE, superseded_at = :now
                WHERE CAST(id AS text) = :id
                """
            ),
            {"id": old_doc["id"], "now": now},
        )

    # Embed vector string
    if embedding is not None:
        vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
    else:
        vec_str = None

    # Build title from source name + year if present
    tax_year = source.get("tax_year")
    title = source["name"]
    if tax_year:
        title = f"{title} (TY {tax_year})"

    conn.execute(
        text(
            """
            INSERT INTO public.tax_knowledge_document (
                jurisdiction, topic, tax_year, source_url, source_authority,
                title, content, version, is_current,
                ingested_at, ingested_by, digest_sha256, embedding, metadata
            ) VALUES (
                :jurisdiction, :topic, :tax_year, :source_url, :source_authority,
                :title, :content, :version, TRUE,
                :ingested_at, :ingested_by, :digest_sha256,
                CAST(:embedding AS vector),
                CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "jurisdiction": source["jurisdiction"],
            "topic": source["topic"],
            "tax_year": tax_year,
            "source_url": source["base_url"],
            "source_authority": _infer_authority(source["base_url"]),
            "title": title,
            "content": content,
            "version": new_version,
            "ingested_at": now,
            "ingested_by": _INGESTED_BY,
            "digest_sha256": digest,
            "embedding": vec_str,
            "metadata": '{"crawl_hint": ' + _json_escape(source.get("crawl_hint", "")) + "}",
        },
    )

    return new_version


def _infer_authority(url: str) -> str:
    if "irs.gov" in url:
        return "IRS"
    if "dor.sc.gov" in url or "scdor" in url.lower():
        return "SCDOR"
    if "congress.gov" in url:
        return "Congress"
    if "ecfr.gov" in url or "law.cornell.edu" in url:
        return "CFR"
    if "treasury.gov" in url:
        return "Treasury"
    return "Unknown"


def _json_escape(s: str) -> str:
    import json
    return json.dumps(s)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def run_curator(
    output_dir: str | None = None,
    only: str | None = None,
    dry_run: bool = False,
) -> str:
    """Main curator loop. Returns the Markdown digest as a string."""
    run_at = datetime.now(timezone.utc)

    # Load sources
    with open(_SOURCES_FILE, "r") as f:
        config = yaml.safe_load(f)
    sources: list[dict] = config.get("sources", [])

    if only:
        sources = [s for s in sources if s["name"] == only]
        if not sources:
            raise ValueError(f"No source named {only!r} found in ingestion_sources.yaml")

    # Set up sync SQLAlchemy connection (curator is a CLI, not an async app)
    from sqlalchemy import create_engine
    db_url = _get_admin_db_url()
    engine = create_engine(db_url, pool_pre_ping=True)

    log_rows: list[dict[str, str]] = []
    ingested_count = 0
    skipped_count = 0
    error_count = 0

    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
        with engine.begin() as conn:
            for source in sources:
                name = source["name"]
                jurisdiction = source["jurisdiction"]
                topic = source["topic"]
                tax_year = source.get("tax_year")
                url = source["base_url"]

                # Skip paywalled sources
                if source.get("paywalled"):
                    log_rows.append({
                        "source": name,
                        "jurisdiction": jurisdiction,
                        "topic": topic,
                        "status": "skipped-paywalled",
                        "notes": "Paywalled source — not ingested per policy",
                    })
                    skipped_count += 1
                    continue

                logger.info("Fetching: %s — %s", name, url)
                raw_text, fetch_error = await _fetch_url(url, client)

                if fetch_error or not raw_text:
                    logger.warning("Fetch error for %s: %s", name, fetch_error)
                    log_rows.append({
                        "source": name,
                        "jurisdiction": jurisdiction,
                        "topic": topic,
                        "status": f"fetch-error",
                        "notes": fetch_error or "empty response",
                    })
                    error_count += 1
                    await asyncio.sleep(_REQUEST_DELAY_S)
                    continue

                # HTML → text conversion (no-op for PDF which already extracted)
                if not url.lower().endswith(".pdf"):
                    content = _html_to_text(raw_text)
                else:
                    content = raw_text

                if not content or len(content.strip()) < 50:
                    log_rows.append({
                        "source": name,
                        "jurisdiction": jurisdiction,
                        "topic": topic,
                        "status": "fetch-error",
                        "notes": "extracted content too short (< 50 chars) — possible fetch failure",
                    })
                    error_count += 1
                    await asyncio.sleep(_REQUEST_DELAY_S)
                    continue

                # Dedup check
                new_digest = _digest(content)
                old_doc = await _get_current_doc(conn, jurisdiction, topic, tax_year)

                if old_doc and old_doc["digest_sha256"] == new_digest:
                    logger.info("Unchanged: %s — skipping", name)
                    log_rows.append({
                        "source": name,
                        "jurisdiction": jurisdiction,
                        "topic": topic,
                        "status": "skipped-unchanged",
                        "notes": f"digest unchanged (v{old_doc['version']})",
                    })
                    skipped_count += 1
                    await asyncio.sleep(_REQUEST_DELAY_S)
                    continue

                # Generate embedding.
                #
                # Infinity (BAAI/bge-large-en-v1.5) accepts up to ~122,880 chars
                # per input.  We embed up to 100K chars here (5K safety margin)
                # — the "intro" vector for the doc.  If the doc is longer, the
                # tail content stays unindexed at curator time; run
                # `chunk_oversized_docs.py` afterwards to split it into chunks
                # whose deep content is indexed via parent_document_id.
                embedding: list[float] | None = None
                try:
                    from app.exports.tax_knowledge_embed import embed_text
                    logger.info("Generating embedding for: %s", name)
                    _EMBED_MAX_CHARS = 100_000
                    if len(content) > _EMBED_MAX_CHARS:
                        logger.info(
                            "%s is %d chars — embedding first %d only "
                            "(run chunk_oversized_docs.py for full coverage)",
                            name, len(content), _EMBED_MAX_CHARS,
                        )
                    embed_text_input = content[:_EMBED_MAX_CHARS]
                    embedding = await embed_text(embed_text_input)
                    if embedding is None:
                        logger.warning("Embedding returned None for %s", name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Embedding failed for %s: %s", name, exc)

                new_version = await _supersede_and_insert(
                    conn, source, content, new_digest, old_doc, embedding, dry_run
                )

                old_note = f"prev v{old_doc['version']} superseded" if old_doc else "new"
                emb_note = "embedding stored" if embedding else "no embedding (key missing?)"
                log_rows.append({
                    "source": name,
                    "jurisdiction": jurisdiction,
                    "topic": topic,
                    "status": f"ingested v{new_version}",
                    "notes": f"{old_note}; {emb_note}",
                })
                ingested_count += 1
                logger.info("Ingested: %s v%d", name, new_version)

                await asyncio.sleep(_REQUEST_DELAY_S)

    # Table totals
    total_docs = 0
    current_docs = 0
    superseded_docs = 0
    if not dry_run:
        with engine.connect() as conn:
            from sqlalchemy import text
            row = conn.execute(text(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN is_current THEN 1 ELSE 0 END) AS current_count, "
                "SUM(CASE WHEN NOT is_current THEN 1 ELSE 0 END) AS superseded_count "
                "FROM public.tax_knowledge_document"
            )).mappings().first()
            if row:
                total_docs = row["total"] or 0
                current_docs = row["current_count"] or 0
                superseded_docs = row["superseded_count"] or 0

    engine.dispose()

    # Build digest
    digest_lines = [
        "# Tax Knowledge Curator — Ingestion Digest",
        "",
        f"Run at: {run_at.isoformat()}",
        f"Dry run: {'YES — no DB writes' if dry_run else 'no'}",
        f"Sources checked: {len(sources)}",
        f"New documents ingested: {ingested_count}",
        f"Sources skipped (unchanged or paywalled): {skipped_count}",
        f"Fetch errors: {error_count}",
        "",
        "## Ingestion log",
        "",
        "| Source | Jurisdiction | Topic | Status | Notes |",
        "|---|---|---|---|---|",
    ]
    for row in log_rows:
        digest_lines.append(
            f"| {row['source']} | {row['jurisdiction']} | {row['topic']} "
            f"| {row['status']} | {row['notes']} |"
        )

    digest_lines += [
        "",
        "## Table totals (post-run)",
        "",
        f"- Total documents: {total_docs}",
        f"- Current documents: {current_docs}",
        f"- Superseded documents: {superseded_docs}",
        "",
        "---",
        "_Generated by Tax Knowledge Curator (agents/tax_research/knowledge_curator/run_curator.py)_",
    ]
    digest_md = "\n".join(digest_lines)

    # Write to file if requested
    if output_dir and not dry_run:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        fname = f"curator-digest-{run_at.strftime('%Y%m%d-%H%M%S')}.md"
        (out_path / fname).write_text(digest_md, encoding="utf-8")
        logger.info("Digest written to: %s/%s", output_dir, fname)

    return digest_md


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tax Knowledge Curator — fetch, diff, and ingest tax rule documents"
    )
    parser.add_argument(
        "--output", "-o",
        metavar="DIR",
        help="Write Markdown digest to this directory (created if needed).",
    )
    parser.add_argument(
        "--only",
        metavar="SOURCE_NAME",
        help="Process only the source with this exact name (for debugging).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and diff but do NOT write to the database.",
    )
    args = parser.parse_args()

    digest = asyncio.run(
        run_curator(
            output_dir=args.output,
            only=args.only,
            dry_run=args.dry_run,
        )
    )
    print(digest)


if __name__ == "__main__":
    main()
