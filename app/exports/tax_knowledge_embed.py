"""Embedding generator for the tax knowledge RAG index.

Generates text embeddings via OpenRouter using the ``text-embedding-3-small``
model (1536-dimensional vectors, same dimensionality as the
``tax_knowledge_document.embedding vector(1536)`` column).

Configuration (all from environment — never hardcoded):
  OPENROUTER_API_KEY   — required in production; set in .env / docker env
  EMBED_MODEL          — optional override; defaults to openai/text-embedding-3-small
  EMBED_BASE_URL       — optional override; defaults to https://openrouter.ai/api/v1

Cost note (2026-05-29):
  text-embedding-3-small via OpenRouter: ~$0.00002 / 1K tokens.
  Baseline ingestion of ~200 documents at ~500 tokens each ≈ $0.002 total.

Usage:
    from app.exports.tax_knowledge_embed import embed_text, embed_batch

    vector = await embed_text("IRC §174 research or experimental expenditures...")
    # Returns a list[float] of length 1536, or None on failure.

    vectors = await embed_batch(["text1", "text2", ...])
    # Returns list[list[float] | None] — one entry per input.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "openai/text-embedding-3-small"
_EXPECTED_DIM = 1536


def _api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. "
            "Add it to your .env file or docker environment. "
            "Embedding generation requires a valid OpenRouter API key."
        )
    return key


def _base_url() -> str:
    return os.getenv("EMBED_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _model() -> str:
    return os.getenv("EMBED_MODEL", _DEFAULT_MODEL)


async def embed_text(text: str) -> list[float] | None:
    """Generate a 1536-dim embedding for a single text string.

    Args:
        text: The text to embed.  Will be truncated at 8191 tokens by the
              model endpoint if longer; keep documents reasonably sized.

    Returns:
        A list of 1536 floats, or None if the API call fails after logging
        the error.  Callers should treat None as "embedding not available"
        and store NULL in the DB rather than crashing the ingestion run.
    """
    results = await embed_batch([text])
    return results[0]


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float] | None]:
    """Generate embeddings for a list of texts, batching to avoid request limits.

    Args:
        texts:      Input strings.
        batch_size: Maximum texts per API request (OpenRouter limit is ~96;
                    we default to 32 for safety and to keep request latency low).

    Returns:
        A list[list[float] | None] with one entry per input text, in order.
        None entries indicate embedding failure for that specific text.
    """
    results: list[list[float] | None] = [None] * len(texts)

    try:
        key = _api_key()
    except RuntimeError as exc:
        logger.warning("embed_batch: %s — returning all-None vectors", exc)
        return results

    base_url = _base_url()
    model = _model()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://accounting.magicunicorn.dev",
        "X-Title": "Accounting-Ops Tax Knowledge Curator",
    }

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
                    "embed_batch: HTTP %s from %s — batch %d-%d marked None. Body: %s",
                    exc.response.status_code,
                    base_url,
                    batch_start,
                    batch_start + len(batch),
                    exc.response.text[:400],
                )
                # Leave these entries as None; continue with next batch
                continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "embed_batch: unexpected error for batch %d-%d: %s",
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
                if vec and len(vec) == _EXPECTED_DIM:
                    results[absolute_idx] = vec
                else:
                    logger.warning(
                        "embed_batch: unexpected embedding dimension %d at index %d "
                        "(expected %d) — marking None",
                        len(vec) if vec else 0,
                        absolute_idx,
                        _EXPECTED_DIM,
                    )

    return results
