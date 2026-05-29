"""Chunk + embed oversized tax_knowledge_document rows.

PROBLEM
-------
The Tax Knowledge Curator (``agents/tax_research/knowledge_curator/run_curator.py``)
historically embedded only the first ~8000 chars of each scraped document
because the Infinity service used to bound itself at the model's token limit.
After we switched the backend to Infinity with BAAI/bge-large-en-v1.5
(migration 0006), the practical per-request char ceiling moved up to ~122,880
chars — but documents like IRS Pub 946 (~498K) and the 2024 Form 1120
instructions (~221K) still blow through it in one shot.

Net effect: the long-tail content of large documents was invisible to the
semantic-search path in ``app.tools.tax_get_current_rule.get_current_rule``.

SOLUTION
--------
For each row with ``length(content) > 100_000`` chars:

  1. Split the body into chunks of <100_000 chars each, with ~5_000 chars
     of overlap between consecutive chunks. The splitter prefers paragraph
     boundaries (double-newline), falls back to sentence boundaries, then to
     hard char-count splits as a last resort.
  2. Embed each chunk via ``app.exports.tax_knowledge_embed.embed_batch``
     (which hits the running Infinity service on midboy2).
  3. Insert each chunk as a child row in ``public.tax_knowledge_document``
     with ``parent_document_id`` pointing at the original and
     ``chunk_index`` set to its ordinal position (0-based).

The chunk rows inherit jurisdiction / topic / tax_year / source_url /
source_authority / version / is_current from the parent so the existing
retrieval queries continue to surface them under the same filter tuple.

ORIGINAL ROWS
-------------
We DO NOT null out the originals' embeddings. The original's embedding (which
was generated against the first ~8000 chars of content) is still a reasonable
"abstract" / "intro" vector — it will continue to rank well for queries that
genuinely match the opening sections of the document. The chunks add deep
coverage on top of that. Keeping the original avoids losing recall for
queries that already worked.

If you would prefer pure-chunk-only retrieval (no parent vector), pass
``--null-parent-embeddings`` on the command line.

IDEMPOTENCY
-----------
Safe to re-run. Documents that already have child chunks (any row with
parent_document_id matching the candidate's id) are skipped.

RUN
---
    docker exec -it accounting_ops-app-1 \\
        python -m app.exports.chunk_oversized_docs

    docker exec -it accounting_ops-app-1 \\
        python -m app.exports.chunk_oversized_docs --dry-run

    docker exec -it accounting_ops-app-1 \\
        python -m app.exports.chunk_oversized_docs --null-parent-embeddings

Connects via ``ADMIN_DATABASE_URL`` (admin role bypasses RLS — required to
INSERT chunk rows that reference parents owned by the same admin role).

Embedding backend honored from env (``EMBED_BASE_URL`` /
``INFINITY_BASE_URL``). On midboy1, the docker-compose stack exposes the
Infinity service at ``http://100.66.42.57:7997``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
logger = logging.getLogger("chunker")

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

# Hard upper bound on any single chunk passed to Infinity. Infinity's
# documented limit is 122_880 chars; we leave generous headroom.
MAX_CHUNK_CHARS = 100_000

# Overlap (in chars) between consecutive chunks. Roughly 5K chars covers a
# few paragraphs of context, which is enough for cosine-similarity to pull a
# chunk back even if the matching phrase straddles the boundary.
OVERLAP_CHARS = 5_000

# Threshold at which we start chunking. Anything <= this stays as a single
# row. Set equal to MAX_CHUNK_CHARS so that documents at exactly the boundary
# (very rare) don't get chunked unnecessarily.
CHUNK_TRIGGER_CHARS = MAX_CHUNK_CHARS

# Embedding batch size when calling Infinity (the embed_batch helper already
# sub-batches internally — this is just the outer working set).
EMBED_BATCH = 16


# ---------------------------------------------------------------------------
# Paragraph-aware splitter
# ---------------------------------------------------------------------------

_PARAGRAPH_RE = re.compile(r"\n\s*\n")
# Sentence boundary: ., ?, ! followed by whitespace then an uppercase letter
# (loose but good enough for IRS / SCDOR prose).
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(])")


def _split_paragraphs(body: str) -> list[str]:
    """Split into paragraphs on blank lines. Drops empties."""
    return [p for p in _PARAGRAPH_RE.split(body) if p.strip()]


def _split_oversized_paragraph(para: str, max_chars: int) -> list[str]:
    """If a single paragraph itself is over the limit, split on sentences.

    If even sentences are too long, fall back to a hard char-count slice.
    """
    if len(para) <= max_chars:
        return [para]

    pieces: list[str] = []
    sentences = _SENTENCE_RE.split(para)

    buf = ""
    for s in sentences:
        candidate = (buf + " " + s).strip() if buf else s
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                pieces.append(buf)
            # Sentence itself may still be huge (e.g. a wide IRS table glued
            # into one line by PDF extraction) — last-resort hard slice.
            if len(s) > max_chars:
                for i in range(0, len(s), max_chars):
                    pieces.append(s[i: i + max_chars])
                buf = ""
            else:
                buf = s

    if buf:
        pieces.append(buf)

    return pieces


def chunk_content(
    body: str,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
) -> list[str]:
    """Paragraph-aware chunker with rolling overlap.

    Algorithm:
      1. Walk paragraphs in order. Accumulate them in a buffer.
      2. When the buffer would exceed max_chunk_chars after adding the next
         paragraph, emit the current buffer as a chunk and start a fresh
         buffer pre-seeded with the trailing ``overlap_chars`` of the chunk
         we just emitted (snapped to a paragraph boundary if possible).
      3. Paragraphs that are themselves too long get pre-split via
         ``_split_oversized_paragraph``.

    Guarantees:
      - Every returned chunk has len(chunk) <= max_chunk_chars (modulo
        last-resort hard-slice paths).
      - Returns [] if body is empty/whitespace.
    """
    body = body.strip()
    if not body:
        return []

    if len(body) <= max_chunk_chars:
        return [body]

    # Pre-split paragraphs, expanding any oversized ones.
    raw_paras = _split_paragraphs(body)
    paras: list[str] = []
    for p in raw_paras:
        if len(p) > max_chunk_chars:
            paras.extend(_split_oversized_paragraph(p, max_chunk_chars))
        else:
            paras.append(p)

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        chunk_text = "\n\n".join(buf).strip()
        if chunk_text:
            chunks.append(chunk_text)

        # Seed next buffer with the tail of the chunk we just emitted to
        # provide overlap. We snap to paragraph boundaries from the end.
        tail: list[str] = []
        tail_len = 0
        for p in reversed(buf):
            sep = 2 if tail else 0
            if tail_len + len(p) + sep > overlap_chars and tail:
                break
            tail.insert(0, p)
            tail_len += len(p) + sep
        buf = tail
        buf_len = tail_len

    for p in paras:
        sep = 2 if buf else 0  # account for the "\n\n" we will join with
        if buf_len + len(p) + sep > max_chunk_chars:
            flush()
            # After flush, buf may still be non-empty (overlap tail) — recompute
            sep = 2 if buf else 0
            if buf_len + len(p) + sep > max_chunk_chars:
                # Overlap tail + this paragraph still wouldn't fit. Force-flush
                # the overlap (chunk would be tiny but correctness > prettiness),
                # then start fresh with just this paragraph.
                chunks.append("\n\n".join(buf).strip()) if buf else None
                buf = []
                buf_len = 0
        buf.append(p)
        buf_len += len(p) + sep

    if buf:
        chunks.append("\n\n".join(buf).strip())

    # Defensive: drop any empty results
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# DB plumbing
# ---------------------------------------------------------------------------

def _admin_url() -> str:
    url = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "ADMIN_DATABASE_URL (preferred) or DATABASE_URL must be set so we "
            "can connect as a role that owns public.tax_knowledge_document."
        )
    # SQLAlchemy needs the asyncpg driver scheme
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


_SELECT_OVERSIZED = text(
    """
    SELECT id,
           jurisdiction,
           topic,
           tax_year,
           source_url,
           source_authority,
           title,
           content,
           version,
           is_current,
           ingested_by,
           digest_sha256,
           metadata
    FROM public.tax_knowledge_document
    WHERE length(content) > :threshold
      AND parent_document_id IS NULL
      AND is_current = TRUE
    ORDER BY length(content) DESC
    """
)

_COUNT_EXISTING_CHUNKS = text(
    """
    SELECT count(*)::int AS n
    FROM public.tax_knowledge_document
    WHERE parent_document_id = :parent_id
    """
)

_INSERT_CHUNK = text(
    """
    INSERT INTO public.tax_knowledge_document (
        jurisdiction, topic, tax_year, source_url, source_authority,
        title, content, version, is_current, ingested_by, digest_sha256,
        embedding, metadata, parent_document_id, chunk_index
    ) VALUES (
        :jurisdiction, :topic, :tax_year, :source_url, :source_authority,
        :title, :content, :version, :is_current, :ingested_by, :digest_sha256,
        CAST(:embedding AS vector), CAST(:metadata AS jsonb),
        :parent_document_id, :chunk_index
    )
    RETURNING id
    """
)

_NULL_PARENT_EMBEDDING = text(
    """
    UPDATE public.tax_knowledge_document
       SET embedding = NULL
     WHERE id = :parent_id
    """
)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _process_parent(
    conn: Any,
    parent: dict[str, Any],
    *,
    dry_run: bool,
    null_parent_embedding: bool,
) -> dict[str, Any]:
    parent_id: UUID = parent["id"]
    body: str = parent["content"]
    name = f"{parent['jurisdiction']}/{parent['topic']}/v{parent['version']}"

    # Idempotency check
    existing = (
        await conn.execute(_COUNT_EXISTING_CHUNKS, {"parent_id": parent_id})
    ).scalar()
    if existing and existing > 0:
        logger.info(
            "Skip %s — already has %d chunk row(s)", name, existing
        )
        return {
            "name": name,
            "parent_id": str(parent_id),
            "source_url": parent["source_url"],
            "skipped": True,
            "reason": "already_chunked",
            "chunk_count": existing,
        }

    chunks = chunk_content(body)
    logger.info(
        "Parent %s len=%d → %d chunks (sizes=%s)",
        name,
        len(body),
        len(chunks),
        [len(c) for c in chunks[:8]] + (["..."] if len(chunks) > 8 else []),
    )

    if dry_run:
        return {
            "name": name,
            "parent_id": str(parent_id),
            "source_url": parent["source_url"],
            "skipped": False,
            "chunk_count": len(chunks),
            "chunk_sizes": [len(c) for c in chunks],
            "dry_run": True,
        }

    # Embed all chunks via Infinity
    from app.exports.tax_knowledge_embed import embed_batch  # local import

    vectors: list[list[float] | None] = []
    for batch_start in range(0, len(chunks), EMBED_BATCH):
        batch = chunks[batch_start: batch_start + EMBED_BATCH]
        v = await embed_batch(batch)
        vectors.extend(v)

    n_embedded = sum(1 for v in vectors if v is not None)
    logger.info("  embedded %d / %d chunks for %s", n_embedded, len(chunks), name)

    # Insert chunk rows
    inserted_ids: list[str] = []
    for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
        emb_literal = (
            "[" + ",".join(repr(float(x)) for x in vec) + "]"
            if vec is not None
            else None
        )
        params = {
            "jurisdiction": parent["jurisdiction"],
            "topic": parent["topic"],
            "tax_year": parent["tax_year"],
            "source_url": parent["source_url"],
            "source_authority": parent["source_authority"],
            "title": parent["title"] + f" — chunk {idx + 1}/{len(chunks)}",
            "content": chunk,
            "version": parent["version"],
            "is_current": parent["is_current"],
            "ingested_by": "chunk_oversized_docs",
            # digest_sha256 is NOT NULL — prefix with parent's digest + chunk idx
            "digest_sha256": f"{parent['digest_sha256']}#chunk{idx}",
            "embedding": emb_literal,
            "metadata": '{"chunk_of":"' + str(parent_id) + '","chunk_index":' + str(idx) + ',"chunk_count":' + str(len(chunks)) + ',"chunk_chars":' + str(len(chunk)) + '}',
            "parent_document_id": parent_id,
            "chunk_index": idx,
        }
        row = (await conn.execute(_INSERT_CHUNK, params)).mappings().first()
        inserted_ids.append(str(row["id"]))

    if null_parent_embedding:
        await conn.execute(_NULL_PARENT_EMBEDDING, {"parent_id": parent_id})
        logger.info("  nulled parent embedding for %s", name)

    return {
        "name": name,
        "parent_id": str(parent_id),
        "source_url": parent["source_url"],
        "skipped": False,
        "chunk_count": len(chunks),
        "embedded_count": n_embedded,
        "inserted_ids": inserted_ids[:3] + (["..."] if len(inserted_ids) > 3 else []),
    }


async def run(
    *,
    dry_run: bool = False,
    null_parent_embeddings: bool = False,
    threshold: int = CHUNK_TRIGGER_CHARS,
) -> int:
    engine = create_async_engine(_admin_url(), pool_pre_ping=True)
    summary: list[dict[str, Any]] = []
    try:
        async with engine.begin() as conn:
            rows = (
                await conn.execute(_SELECT_OVERSIZED, {"threshold": threshold})
            ).mappings().all()
            logger.info(
                "Found %d oversized parent document(s) with length > %d chars",
                len(rows), threshold,
            )

            if not rows:
                logger.info("Nothing to do.")
                return 0

            for row in rows:
                parent = dict(row)
                result = await _process_parent(
                    conn,
                    parent,
                    dry_run=dry_run,
                    null_parent_embedding=null_parent_embeddings,
                )
                summary.append(result)

            if dry_run:
                # Don't commit when dry-running
                raise _DryRunRollback()

    except _DryRunRollback:
        logger.info("Dry-run complete — rolled back; no rows inserted.")

    await engine.dispose()

    # Report
    print()
    print("=" * 72)
    print("CHUNKING SUMMARY")
    print("=" * 72)
    total_chunks = 0
    for s in summary:
        if s.get("skipped"):
            print(f"SKIP  {s['name']:40s}  ({s.get('reason')})")
            continue
        chunks = s.get("chunk_count", 0)
        total_chunks += chunks
        marker = "DRYRUN" if s.get("dry_run") else "OK"
        print(
            f"{marker:6s} {s['name']:40s}  chunks={chunks:3d}  "
            f"src={s['source_url']}"
        )
    print(f"\nTotal chunks {'would-be-inserted' if dry_run else 'inserted'}: {total_chunks}")
    return 0


class _DryRunRollback(Exception):
    pass


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Chunk + embed oversized tax_knowledge_document rows.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Compute chunks and report; do not insert.",
    )
    p.add_argument(
        "--null-parent-embeddings", action="store_true",
        help="After chunking, also NULL the embedding on each parent row.",
    )
    p.add_argument(
        "--threshold", type=int, default=CHUNK_TRIGGER_CHARS,
        help=f"length(content) threshold for chunking (default {CHUNK_TRIGGER_CHARS}).",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        rc = asyncio.run(run(
            dry_run=args.dry_run,
            null_parent_embeddings=args.null_parent_embeddings,
            threshold=args.threshold,
        ))
    except Exception as exc:
        logger.exception("Chunker crashed: %s", exc)
        sys.exit(2)
    sys.exit(rc)


if __name__ == "__main__":
    main()
