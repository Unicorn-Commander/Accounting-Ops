"""Integration tests for the document-upload path (live stack + Garage).

Proves the full PHASE-1 loop against the real app + DB + object store:

  (1) Upload an image → 200, a ``receipt`` row is created (workspace + entity
      scoped) AND a ``document_review`` approval_queue row is created 'pending'.
      NOTHING is posted to journal_entry (never auto-posted).
  (2) Re-uploading the SAME bytes → 200 duplicate=True, NO second receipt and NO
      second approval (content-hash dedup).
  (3) An unsupported mime type → 415.
  (4) Upload to an out-of-scope entity (scoped membership) → 403 (BOLA).

Skips cleanly when the stack is unreachable OR when Garage S3 credentials aren't
configured on the app (the upload then returns 503 and we skip rather than fail —
the object store is an external dependency the unit tests already cover in full).

All reads go through the owner DB pool. The upload goes through the REAL HTTP
route with the REAL token.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import struct
import zlib

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.test_mcp_write_tools import _membership_as

pytestmark = pytest.mark.integration

_SCHEMA = f"accounting_ops_{str(TEST_WORKSPACE_ID).replace('-', '_')}"


def _tiny_png(seed: int = 0) -> bytes:
    """Build a minimal valid 1x1 PNG. ``seed`` perturbs the pixel so different
    seeds produce different content hashes (for the dedup-vs-distinct tests)."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    raw = bytes([0, seed % 256, (seed >> 8) % 256, 0])  # filter byte + RGB pixel
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


async def _upload(
    client: httpx.AsyncClient, content: bytes, filename: str, mime: str
) -> httpx.Response:
    return await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/entities/{TEST_ENTITY_ID}/documents/upload",
        files={"file": (filename, content, mime)},
    )


async def _count_receipt(pool: asyncpg.Pool, content_hash: str) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            return await conn.fetchval(
                "SELECT count(*) FROM receipt WHERE content_hash = $1", content_hash
            )


async def _count_doc_approvals(pool: asyncpg.Pool, receipt_id: str) -> int:
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            return await conn.fetchval(
                """
                SELECT count(*) FROM approval_queue
                WHERE subject_type = 'document_review' AND subject_id::text = $1
                """,
                receipt_id,
            )


@pytest.mark.asyncio
async def test_upload_creates_receipt_and_approval_never_posts(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
):
    png = _tiny_png(seed=dt.datetime.now().microsecond)
    content_hash = hashlib.sha256(png).hexdigest()

    res = await _upload(client, png, "receipt.png", "image/png")
    if res.status_code == 503:
        pytest.skip("Garage S3 not configured on the app — upload path unavailable.")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["duplicate"] is False
    receipt_id = body["receipt_id"]
    assert body["approval_id"]

    # A receipt row + exactly one document_review approval exist.
    assert await _count_receipt(db, content_hash) == 1
    assert await _count_doc_approvals(db, receipt_id) == 1

    # Crucially: NOTHING was posted to the ledger for this receipt.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            posted = await conn.fetchval(
                "SELECT count(*) FROM journal_entry WHERE source_document_id::text = $1",
                receipt_id,
            )
            status_val = await conn.fetchval(
                """
                SELECT status::text FROM approval_queue
                WHERE subject_id::text = $1 AND subject_type = 'document_review'
                """,
                receipt_id,
            )
    assert posted == 0
    assert status_val == "pending"


@pytest.mark.asyncio
async def test_reupload_same_bytes_dedups(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
):
    png = _tiny_png(seed=42424)
    content_hash = hashlib.sha256(png).hexdigest()

    first = await _upload(client, png, "dup.png", "image/png")
    if first.status_code == 503:
        pytest.skip("Garage S3 not configured.")
    assert first.status_code == 200, first.text

    second = await _upload(client, png, "dup-again.png", "image/png")
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True

    # Still exactly one receipt + one approval despite two uploads.
    assert await _count_receipt(db, content_hash) == 1
    assert await _count_doc_approvals(db, first.json()["receipt_id"]) == 1


@pytest.mark.asyncio
async def test_unsupported_type_is_415(
    client: httpx.AsyncClient, seeded_workspace: StackConfig
):
    res = await _upload(
        client, b"#!/bin/sh\necho hi\n", "evil.sh", "application/x-sh"
    )
    # 415 is enforced before any storage attempt, so this never reaches Garage.
    assert res.status_code == 415, res.text


@pytest.mark.asyncio
async def test_out_of_scope_entity_is_403(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig,
    live_stack: StackConfig,
):
    """A membership scoped to a DIFFERENT entity cannot upload to TEST_ENTITY_ID."""
    import uuid as _uuid

    other_entity = _uuid.uuid5(_uuid.NAMESPACE_DNS, f"{TEST_WORKSPACE_ID}:scope-other")
    # Flip the token's membership to be scoped to `other_entity` only.
    async with _membership_as(db, live_stack.uc_uid, "accountant", entity_scope=[str(other_entity)]):
        png = _tiny_png(seed=999)
        res = await _upload(client, png, "scoped.png", "image/png")
        assert res.status_code == 403, res.text
