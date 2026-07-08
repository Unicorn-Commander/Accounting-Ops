"""A tiny in-process TTL cache for hot, rarely-changing per-workspace reads.

Motivation: the connector-status endpoints (eBay / Stripe) fire on EVERY
dashboard navigation, and each does a credential decrypt (a DB read + an
access-audit WRITE) plus an RLS-pinned session with two queries. When a user
clicks between screens, that work repeats for the same workspace every time.
Caching the (non-secret) status result for a few seconds lets inter-screen
navigation reuse it instead of re-hitting the DB.

Scope / safety:
  * Process-local, best-effort. The staleness ceiling is the TTL — so this is
    ONLY for values where being a few seconds stale is not a correctness risk
    (a status chip, never a balance or an authz decision).
  * Callers MUST ``invalidate`` on any event that changes the value (connect /
    disconnect), so the cache is never wrong after an actual change — only the
    passive-navigation reads are served stale (bounded by the TTL).
"""
from __future__ import annotations

import time
from typing import Any


class TTLCache:
    """A minimal monotonic-clock TTL cache. Not thread-safe by design — the
    asyncio event loop serializes access, and a rare lost update just costs one
    extra recompute (never a wrong-after-write, because writers invalidate)."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = float(ttl_seconds)
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        ts, val = hit
        if (time.monotonic() - ts) >= self._ttl:
            self._store.pop(key, None)
            return None
        return val

    def put(self, key: str, value: Any) -> Any:
        """Store ``value`` under ``key`` and RETURN it (so call sites can wrap a
        ``return`` in one expression: ``return cache.put(k, {...})``)."""
        self._store[key] = (time.monotonic(), value)
        return value

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)


__all__ = ("TTLCache",)
