"""Personal Access Token end-to-end integration coverage.

Verifies the full lifecycle through the LIVE stack:

  1. Direct DB seeding of a PAT row (so we can exercise the auth path without
     needing a Keycloak session in the test runner).
  2. PAT bearer reaches /api/me/workspaces → 200.
  3. Revoke the PAT (via the same direct-DB path) → next call fails 401.
  4. Expire the PAT (expires_at < now) → call fails 401.
  5. Tampered token (one char flipped) → 401, no plaintext fall-through.

The HTTP layer uses in-process ASGI (httpx + ASGITransport) so the test
hits the real auth chain + real DB pool without a network round-trip,
mirroring the H2 service-token scope tests.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

from app.security.pat import PAT_PREFIX, generate_secret, hash_secret, secret_prefix
from tests.conftest import StackConfig

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — seed / mutate PAT rows directly via asyncpg
# ---------------------------------------------------------------------------


async def _ensure_user(pool: asyncpg.Pool, uc_uid: str) -> uuid.UUID:
    """Insert public.user_account if not present, return id."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id FROM public.user_account WHERE uc_uid = $1", uc_uid
            )
            if row:
                return row["id"]
            row = await conn.fetchrow(
                """
                INSERT INTO public.user_account (id, uc_uid, is_active, created_at)
                VALUES (gen_random_uuid(), $1, true, now())
                RETURNING id
                """,
                uc_uid,
            )
            return row["id"]


async def _seed_pat(
    pool: asyncpg.Pool,
    *,
    owner_user_id: uuid.UUID,
    name: str,
    raw_secret: str,
    scopes: list[str] | None = None,
    workspace_scope: list[uuid.UUID] | None = None,
    expires_at: datetime | None = None,
    revoked: bool = False,
) -> uuid.UUID:
    """Insert a PAT row computed from ``raw_secret``."""
    digest = hash_secret(raw_secret)
    prefix = secret_prefix(raw_secret)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO public.personal_access_token (
                    id, owner_user_id, name, secret_hash, secret_prefix,
                    scopes, workspace_scope, expires_at, revoked_at, created_at
                ) VALUES (
                    gen_random_uuid(), $1, $2, $3, $4,
                    $5::text[],
                    $6::uuid[],
                    $7,
                    $8,
                    now()
                )
                RETURNING id
                """,
                owner_user_id,
                name,
                digest,
                prefix,
                scopes or [],
                [str(w) for w in workspace_scope] if workspace_scope else None,
                expires_at,
                datetime.now(timezone.utc) if revoked else None,
            )
            return row["id"]


async def _revoke(pool: asyncpg.Pool, pat_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.personal_access_token SET revoked_at = now() WHERE id = $1",
            pat_id,
        )


async def _delete(pool: asyncpg.Pool, pat_id: uuid.UUID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM public.personal_access_token WHERE id = $1", pat_id
        )


# ---------------------------------------------------------------------------
# In-process ASGI client (mirrors test_h2_service_token_scope's helper)
# ---------------------------------------------------------------------------


def _asgi_client(token: str) -> httpx.AsyncClient:
    from app.main import app as fastapi_app

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Fixture: ensure the test user exists, clean up at the end
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pat_user(
    db: asyncpg.Pool, live_stack: StackConfig
) -> AsyncIterator[uuid.UUID]:
    """Use the same uc_uid the service-token tests use, so a public.user_account
    row + workspace_membership already exist (seeded by the integration env)."""
    uc_uid = live_stack.uc_uid
    user_id = await _ensure_user(db, uc_uid)
    yield user_id
    # Cleanup: remove any PATs left behind by this test class.
    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM public.personal_access_token WHERE owner_user_id = $1",
            user_id,
        )


# ---------------------------------------------------------------------------
# Test 1 — valid PAT → 200 on /api/me/workspaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_pat_authenticates(
    db: asyncpg.Pool, pat_user: uuid.UUID, seeded_workspace: StackConfig
) -> None:
    """A live PAT bearer is accepted and resolves to the owner's identity."""
    raw = generate_secret()
    pat_id = await _seed_pat(
        db,
        owner_user_id=pat_user,
        name="itest-valid",
        raw_secret=raw,
        scopes=["accounting:read"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    async with _asgi_client(raw) as c:
        r = await c.get("/api/me/workspaces")
    assert r.status_code == 200, f"valid PAT denied: {r.status_code} {r.text}"
    body = r.json()
    assert isinstance(body, list)
    # The owner should be a member of TEST_WORKSPACE — seeded_workspace ensures it.
    assert any(w.get("role") for w in body), body


# ---------------------------------------------------------------------------
# Test 2 — revoked PAT → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoked_pat_rejected(
    db: asyncpg.Pool, pat_user: uuid.UUID, seeded_workspace: StackConfig
) -> None:
    raw = generate_secret()
    pat_id = await _seed_pat(
        db,
        owner_user_id=pat_user,
        name="itest-revoke",
        raw_secret=raw,
        scopes=["accounting:read"],
    )
    # First call works.
    async with _asgi_client(raw) as c:
        r0 = await c.get("/api/me/workspaces")
    assert r0.status_code == 200, f"pre-revoke should work: {r0.status_code}"

    await _revoke(db, pat_id)

    async with _asgi_client(raw) as c:
        r1 = await c.get("/api/me/workspaces")
    assert r1.status_code == 401, f"revoked PAT must 401: {r1.status_code} {r1.text}"


# ---------------------------------------------------------------------------
# Test 3 — expired PAT → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_pat_rejected(
    db: asyncpg.Pool, pat_user: uuid.UUID, seeded_workspace: StackConfig
) -> None:
    raw = generate_secret()
    await _seed_pat(
        db,
        owner_user_id=pat_user,
        name="itest-expired",
        raw_secret=raw,
        scopes=["accounting:read"],
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    async with _asgi_client(raw) as c:
        r = await c.get("/api/me/workspaces")
    assert r.status_code == 401, f"expired PAT must 401: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# Test 4 — tampered PAT (good prefix, bad body) → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tampered_pat_rejected(
    db: asyncpg.Pool, pat_user: uuid.UUID, seeded_workspace: StackConfig
) -> None:
    """A token whose prefix matches a row but whose hash does not must 401 —
    not fall through to JWT validation."""
    raw = generate_secret()
    await _seed_pat(
        db,
        owner_user_id=pat_user,
        name="itest-tamper",
        raw_secret=raw,
        scopes=["accounting:read"],
    )
    # Flip the last char to make the hash differ but keep the prefix intact.
    tampered = raw[:-1] + ("A" if raw[-1] != "A" else "B")
    assert secret_prefix(tampered) == secret_prefix(raw), "prefix should match"

    async with _asgi_client(tampered) as c:
        r = await c.get("/api/me/workspaces")
    assert r.status_code == 401, f"tampered PAT must 401: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# Test 5 — non-existent PAT (good prefix shape, no row) → 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_pat_rejected(seeded_workspace: StackConfig) -> None:
    raw = generate_secret()
    # No seed — the row doesn't exist.
    async with _asgi_client(raw) as c:
        r = await c.get("/api/me/workspaces")
    assert r.status_code == 401, f"unknown PAT must 401: {r.status_code} {r.text}"


# ---------------------------------------------------------------------------
# Test 6 — PAT cannot mint another PAT (chicken-and-egg defense)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pat_cannot_mint_pat(
    db: asyncpg.Pool, pat_user: uuid.UUID, seeded_workspace: StackConfig
) -> None:
    raw = generate_secret()
    await _seed_pat(
        db,
        owner_user_id=pat_user,
        name="itest-no-self-mint",
        raw_secret=raw,
        scopes=["accounting:read", "accounting:write", "accounting:admin"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
    )

    async with _asgi_client(raw) as c:
        r = await c.post(
            "/api/me/personal-access-tokens",
            json={"name": "second token from a PAT"},
        )
    assert r.status_code == 403, (
        f"PAT-to-PAT minting must be denied: {r.status_code} {r.text}"
    )
    assert "personal access tokens cannot" in r.json().get("detail", "").lower(), (
        r.json()
    )


# ---------------------------------------------------------------------------
# Test 7 — BACKWARD COMPAT: a legacy NULL-expiry, full-scope PAT still works
#
# The mint path no longer issues never-expiring tokens (app.security.pat
# .resolve_ttl refuses ttl_days 0/null). Verification is deliberately
# untouched, so the ones already in production must keep authenticating.
# A NOT NULL constraint, an expires_at backfill, or treating NULL as expired
# would all turn this test red — and would silently revoke live credentials.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_never_expiring_pat_still_authenticates(
    db: asyncpg.Pool, pat_user: uuid.UUID, seeded_workspace: StackConfig
) -> None:
    raw = generate_secret()
    await _seed_pat(
        db,
        owner_user_id=pat_user,
        name="itest-legacy-never-expires",
        raw_secret=raw,
        # Exactly what the pre-fix mint path produced: every scope the owner's
        # role allowed, no workspace restriction, and NO expiry at all.
        scopes=["accounting:read", "accounting:write", "accounting:admin"],
        workspace_scope=None,
        expires_at=None,
    )

    async with _asgi_client(raw) as c:
        r = await c.get("/api/me/workspaces")
    assert r.status_code == 200, (
        "a live NULL-expiry PAT must keep working — the mint-side fix must "
        f"not revoke already-issued credentials: {r.status_code} {r.text}"
    )
