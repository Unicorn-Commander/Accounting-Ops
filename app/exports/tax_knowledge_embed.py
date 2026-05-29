"""Embedding generator for the tax knowledge RAG index.

Backend priority (evaluated at call time, no restart needed):
  1. EMBED_BASE_URL env — explicit override; defaults to http://infinity:7997
     (the self-hosted Infinity service on the docker network).
  2. INFINITY_BASE_URL env — secondary Infinity pointer (e.g. midboy1 direct).
  3. OPENROUTER_API_KEY env — cloud fallback via OpenRouter.
  4. RuntimeError — no embedding capability available.

Infinity (self-hosted, https://github.com/michaelfeil/infinity):
  Default model:   BAAI/bge-large-en-v1.5   (1024-dim, ~1.3 GB, runs on P40)
  Alt model:       mixedbread-ai/mxbai-embed-large-v1  (1024-dim)
  No auth header required — all requests hit localhost/docker-internal.

OpenRouter (cloud fallback):
  Model:           openai/text-embedding-3-small  (1536-dim)
  Requires:        OPENROUTER_API_KEY in environment.
  Cost note (2026-05-29): ~$0.00002 / 1K tokens.

Important: the column dimension must match the backend in use.  Migration
0006 resizes the embedding column to vector(1024) for Infinity.  If you
switch back to OpenRouter you must re-run the migration to change it to
vector(1536) and re-embed all documents.

Usage:
    from app.exports.tax_knowledge_embed import embed_text, embed_batch

    vector = await embed_text("IRC §174 research or experimental expenditures...")
    # Returns list[float] of length matching the active model, or None on failure.

    vectors = await embed_batch(["text1", "text2", ...])
    # Returns list[list[float] | None] — one entry per input.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend-specific defaults
# ---------------------------------------------------------------------------
_INFINITY_DEFAULT_BASE_URL = "http://infinity:7997"
_INFINITY_DEFAULT_MODEL = "BAAI/bge-large-en-v1.5"
_INFINITY_EXPECTED_DIM = 1024

_OPENROUTER_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_DEFAULT_MODEL = "openai/text-embedding-3-small"
_OPENROUTER_EXPECTED_DIM = 1536


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def _embedding_backend() -> Literal["infinity", "openrouter"]:
    """Detect which embedding backend is active based on environment variables.

    Priority:
      - EMBED_BASE_URL set (non-empty, non-OpenRouter) → infinity
      - INFINITY_BASE_URL set → infinity
      - OPENROUTER_API_KEY set → openrouter
      - EMBED_BASE_URL explicitly set to OpenRouter URL → openrouter

    Returns "infinity" or "openrouter".
    Raises RuntimeError if no backend is configured.
    """
    embed_base = os.getenv("EMBED_BASE_URL", "").strip()
    infinity_base = os.getenv("INFINITY_BASE_URL", "").strip()
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "").strip()

    # Explicit EMBED_BASE_URL pointing at Infinity (default or custom host)
    if embed_base and "openrouter.ai" not in embed_base:
        return "infinity"

    # Explicit INFINITY_BASE_URL always means Infinity
    if infinity_base:
        return "infinity"

    # EMBED_BASE_URL explicitly pointing at OpenRouter, OR just OpenRouter key
    if openrouter_key:
        return "openrouter"

    # Default: use Infinity (assumes docker service 'infinity' is up)
    return "infinity"


def _base_url() -> str:
    """Return the active embedding API base URL."""
    backend = _embedding_backend()
    if backend == "infinity":
        explicit = os.getenv("EMBED_BASE_URL", "").strip()
        if explicit and "openrouter.ai" not in explicit:
            return explicit.rstrip("/")
        infinity_url = os.getenv("INFINITY_BASE_URL", "").strip()
        if infinity_url:
            return infinity_url.rstrip("/")
        return _INFINITY_DEFAULT_BASE_URL
    else:
        return os.getenv("EMBED_BASE_URL", _OPENROUTER_DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    """Return the active embedding model name."""
    # Explicit override always wins
    override = os.getenv("EMBED_MODEL", "").strip()
    if override:
        return override

    backend = _embedding_backend()
    if backend == "infinity":
        return _INFINITY_DEFAULT_MODEL
    else:
        return _OPENROUTER_DEFAULT_MODEL


def _expected_dim() -> int:
    """Return expected embedding dimension for the active backend + model."""
    backend = _embedding_backend()
    model = _model()
    # Both bge-large-en-v1.5 and mxbai-embed-large-v1 are 1024-dim
    if backend == "infinity":
        return _INFINITY_EXPECTED_DIM
    # OpenRouter text-embedding-3-small is 1536-dim
    return _OPENROUTER_EXPECTED_DIM


def _build_headers() -> dict[str, str]:
    """Build request headers for the active backend.

    Infinity: no auth header required (self-hosted, no key).
    OpenRouter: Bearer token required.
    """
    headers: dict[str, str] = {"Content-Type": "application/json"}
    backend = _embedding_backend()

    if backend == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY", "")
        if not key:
            raise RuntimeError(
                "OPENROUTER_API_KEY is not set but openrouter backend was selected. "
                "Add it to your .env file or set EMBED_BASE_URL=http://infinity:7997 "
                "to use the self-hosted Infinity backend."
            )
        headers["Authorization"] = f"Bearer {key}"
        headers["HTTP-Referer"] = "https://accounting.magicunicorn.dev"
        headers["X-Title"] = "Accounting-Ops Tax Knowledge Curator"

    # Infinity: no Authorization header — self-hosted, no key needed
    return headers


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def embed_text(text: str) -> list[float] | None:
    """Generate an embedding for a single text string.

    Returns a list of floats (dimension depends on active backend/model),
    or None if the API call fails.  Callers should treat None as
    "embedding not available" and store NULL in the DB rather than crashing.

    Active backend is determined by _embedding_backend() at call time.
    """
    results = await embed_batch([text])
    return results[0]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float] | None]:
    """Generate embeddings for a list of texts, batching to avoid request limits.

    Args:
        texts:      Input strings.
        batch_size: Maximum texts per API request (Infinity default batch-size
                    is 32; OpenRouter limit ~96; we default to 32 for both).

    Returns:
        list[list[float] | None] — one entry per input text, in order.
        None entries indicate embedding failure for that specific text.
    """
    results: list[list[float] | None] = [None] * len(texts)

    try:
        headers = _build_headers()
    except RuntimeError as exc:
        logger.warning("embed_batch: %s — returning all-None vectors", exc)
        return results

    base_url = _base_url()
    model = _model()
    expected_dim = _expected_dim()
    backend = _embedding_backend()

    logger.debug(
        "embed_batch: backend=%s url=%s model=%s dim=%d texts=%d",
        backend, base_url, model, expected_dim, len(texts),
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        for batch_start in range(0, len(texts), batch_size):
            batch = texts[batch_start: batch_start + batch_size]
            payload: dict[str, Any] = {"model": model, "input": batch}

            try:
                resp = await client.post(
                    f"{base_url}/embeddings",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.error(
                    "embed_batch: HTTP %s from %s (%s backend) — "
                    "batch %d-%d marked None. Body: %s",
                    exc.response.status_code,
                    base_url,
                    backend,
                    batch_start,
                    batch_start + len(batch),
                    exc.response.text[:400],
                )
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "embed_batch: unexpected error (%s backend) for batch %d-%d: %s",
                    backend,
                    batch_start,
                    batch_start + len(batch),
                    exc,
                )
                continue

            for item in data.get("data", []):
                idx = item.get("index", -1)
                if idx < 0:
                    continue
                absolute_idx = batch_start + idx
                if absolute_idx >= len(texts):
                    continue
                vec = item.get("embedding")
                if vec and len(vec) == expected_dim:
                    results[absolute_idx] = vec
                else:
                    logger.warning(
                        "embed_batch: unexpected embedding dimension %d at index %d "
                        "(expected %d, backend=%s) — marking None",
                        len(vec) if vec else 0,
                        absolute_idx,
                        expected_dim,
                        backend,
                    )

    return results
