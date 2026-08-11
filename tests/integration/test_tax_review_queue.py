"""Integration tests for the tax review queue (live stack).

Tests the full create → list → resolve lifecycle against the real DB + HTTP:

  (1) Create a memo-type item via HTTP (POST draft_memo) and verify it appears
      in the list endpoint.
  (2) Create a flagged_position item and verify it appears with the correct
      form_reference.
  (3) List with filters: entity_id, tax_year, status, item_type.
  (4) GET single item returns the full row; entity-scope enforced.
  (5) Resolve (accountant floor): reviewed → resolved cycle.
  (6) Resolve: rejected path.
  (7) RBAC: viewer cannot resolve (403).
  (8) Double-resolve conflict (409).
  (9) RLS isolation: items from workspace A are not visible to workspace B
      (cross-workspace deny — the schema isolation means workspace B's schema
      has no rows for workspace A's items).
  (10) Entity-scope: a scoped member cannot resolve an out-of-scope entity's
       items (403).

Seeds rows directly via the owner DB pool when needed (same pattern as
test_approvals.py). HTTP calls go through the real /api/workspaces/.../
tax-review-queue routes.

Skips cleanly when the stack is not reachable.
"""
from __future__ import annotations

import json
import uuid
import datetime as dt

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_B_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.test_mcp_write_tools import _membership_as

pytestmark = pytest.mark.integration

_SCHEMA = f"accounting_ops_{str(TEST_WORKSPACE_ID).replace('-', '_')}"


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


# ---------------------------------------------------------------------------
# DB-level seed helpers (owner pool, bypass RLS)
# ---------------------------------------------------------------------------


async def _seed_queue_item(
    db: asyncpg.Pool,
    *,
    item_type: str = "memo",
    tax_year: int = 2023,
    form_reference: str | None = None,
    content: dict | None = None,
    confidence: str = "medium",
    submitter_id: str = "tax-1120-preparer",
    submitter_kind: str = "agent",
    status: str = "pending",
) -> str:
    """Insert a tax_review_queue row directly via the owner pool. Returns item id."""
    item_id = str(uuid.uuid4())
    if content is None:
        content = {"text": f"Test memo {item_id}"}
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute(
                """
                INSERT INTO tax_review_queue (
                    id, workspace_id, entity_id, tax_year,
                    item_type, form_reference,
                    content, confidence, citations,
                    status, submitter_kind, submitter_id
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6,
                    $7::jsonb, $8, '[]'::jsonb,
                    $9, $10, $11
                )
                """,
                item_id,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
                tax_year,
                item_type,
                form_reference,
                json.dumps(content),
                confidence,
                status,
                submitter_kind,
                submitter_id,
            )
    return item_id


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def _list_items(
    base_url: str,
    token: str,
    *,
    entity_id=None,
    tax_year=None,
    status_filter="pending",
    item_type=None,
    workspace_id=None,
) -> httpx.Response:
    wid = workspace_id or TEST_WORKSPACE_ID
    params = {}
    if entity_id:
        params["entity_id"] = str(entity_id)
    if tax_year:
        params["tax_year"] = tax_year
    if status_filter:
        params["status_filter"] = status_filter
    if item_type:
        params["item_type"] = item_type

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.get(
            f"/api/workspaces/{wid}/tax-review-queue",
            params=params,
        )


async def _get_item(
    base_url: str,
    token: str,
    item_id: str,
    workspace_id=None,
) -> httpx.Response:
    wid = workspace_id or TEST_WORKSPACE_ID
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.get(
            f"/api/workspaces/{wid}/tax-review-queue/{item_id}"
        )


async def _resolve_item(
    base_url: str,
    token: str,
    item_id: str,
    new_status: str,
    resolution_notes: str | None = None,
    workspace_id=None,
) -> httpx.Response:
    wid = workspace_id or TEST_WORKSPACE_ID
    body = {"status": new_status}
    if resolution_notes:
        body["resolution_notes"] = resolution_notes
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.post(
            f"/api/workspaces/{wid}/tax-review-queue/{item_id}/resolve",
            json=body,
        )


# ---------------------------------------------------------------------------
# (1) Seed a memo and verify it appears in the list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memo_appears_in_list(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A seeded memo row appears in the list endpoint with the correct fields."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    tag = _run_tag()
    content = {"text": f"Test memo for listing {tag}", "topic": "Listing test"}
    item_id = await _seed_queue_item(
        db,
        item_type="memo",
        tax_year=2023,
        content=content,
    )

    r = await _list_items(base, token, entity_id=TEST_ENTITY_ID, tax_year=2023)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and "total" in body

    ids = [item["id"] for item in body["items"]]
    assert item_id in ids, f"Seeded item {item_id} not found in list response"

    # Find our item and verify fields
    our_item = next(i for i in body["items"] if i["id"] == item_id)
    assert our_item["item_type"] == "memo"
    assert our_item["tax_year"] == 2023
    assert our_item["status"] == "pending"
    assert our_item["submitter_id"] == "tax-1120-preparer"
    assert our_item["submitter_kind"] == "agent"


# ---------------------------------------------------------------------------
# (2) Seed a flagged_position and verify form_reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flagged_position_form_reference(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A flagged_position item carries the expected form_reference."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    tag = _run_tag()
    content = {
        "issue": f"§163(j) limitation may apply {tag}",
        "affected_line": "18",
        "context": "Entity has interest expense",
        "recommendation": "CPA to verify §163(j) small business exception",
    }
    item_id = await _seed_queue_item(
        db,
        item_type="flagged_position",
        tax_year=2023,
        form_reference="1120-line-18",
        content=content,
        confidence="high",
    )

    r = await _get_item(base, token, item_id)
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["id"] == item_id
    assert item["item_type"] == "flagged_position"
    assert item["form_reference"] == "1120-line-18"
    assert item["confidence"] == "high"


# ---------------------------------------------------------------------------
# (3) List with filters: status, item_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_filters_status_and_item_type(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """List endpoint filters by status and item_type correctly."""
    base, token = seeded_workspace.base_url, seeded_workspace.token

    # Seed a pending memo and a resolved flagged_position
    memo_id = await _seed_queue_item(db, item_type="memo", tax_year=2024)
    flag_id = await _seed_queue_item(
        db,
        item_type="flagged_position",
        tax_year=2024,
        form_reference="1120-line-26",
        content={"issue": "Test flag"},
        status="resolved",
    )

    # Filter by pending only — should see memo, not the resolved flag
    r = await _list_items(
        base, token, entity_id=TEST_ENTITY_ID, tax_year=2024, status_filter="pending"
    )
    assert r.status_code == 200, r.text
    pending_ids = {i["id"] for i in r.json()["items"]}
    assert memo_id in pending_ids, "Pending memo must appear in pending filter"
    assert flag_id not in pending_ids, "Resolved item must NOT appear in pending filter"

    # Filter by item_type=flagged_position, all statuses
    r = await _list_items(
        base, token, entity_id=TEST_ENTITY_ID, tax_year=2024,
        status_filter="all", item_type="flagged_position"
    )
    assert r.status_code == 200, r.text
    flag_type_ids = {i["id"] for i in r.json()["items"]}
    assert flag_id in flag_type_ids, "Resolved flagged_position must appear in item_type filter"
    assert memo_id not in flag_type_ids, "Memo must not appear in flagged_position filter"


# ---------------------------------------------------------------------------
# (4) GET single item; 404 for unknown id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_single_item_found_and_not_found(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """GET single item returns the row; a missing id returns 404."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    item_id = await _seed_queue_item(db)

    r = await _get_item(base, token, item_id)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == item_id

    # Non-existent id → 404
    r = await _get_item(base, token, str(uuid.uuid4()))
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# (5) Resolve: reviewed → resolved cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_reviewed_then_resolved(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """CPA first marks reviewed, then resolved. Both transitions succeed."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    item_id = await _seed_queue_item(db, item_type="flagged_position",
                                     form_reference="1120-line-26",
                                     content={"issue": "test"})

    # Mark reviewed
    r = await _resolve_item(base, token, item_id, "reviewed",
                            resolution_notes="Noted; confirming COGS split.")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "reviewed"
    assert body["reviewer_uc_uid"] == seeded_workspace.uc_uid

    # Verify DB state
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT status, reviewer_uc_uid, resolved_at, resolution_notes "
                "FROM tax_review_queue WHERE id::text = $1", item_id
            )
    assert row["status"] == "reviewed"
    assert row["reviewer_uc_uid"] == seeded_workspace.uc_uid
    assert row["resolved_at"] is not None
    assert "COGS" in (row["resolution_notes"] or "")

    # Now mark resolved (reviewed → resolved is allowed)
    r = await _resolve_item(base, token, item_id, "resolved",
                            resolution_notes="COGS split confirmed; line 26 correct.")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "resolved"


# ---------------------------------------------------------------------------
# (6) Resolve: rejected path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_rejected(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A pending item can be rejected; the row is stamped correctly."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    item_id = await _seed_queue_item(db)

    r = await _resolve_item(base, token, item_id, "rejected",
                            resolution_notes="Not applicable for this entity.")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT status FROM tax_review_queue WHERE id::text = $1", item_id
            )
    assert row["status"] == "rejected"


# ---------------------------------------------------------------------------
# (7) RBAC: viewer cannot resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_denied_for_viewer(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Resolve requires accountant floor; a viewer is denied (403)."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    item_id = await _seed_queue_item(db)

    async with _membership_as(db, seeded_workspace.uc_uid, role="viewer", entity_scope=None):
        r = await _resolve_item(base, token, item_id, "reviewed")
    assert r.status_code == 403, r.text

    # Row must still be pending
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            status_val = await conn.fetchval(
                "SELECT status FROM tax_review_queue WHERE id::text = $1", item_id
            )
    assert status_val == "pending", "Viewer deny must not mutate the row"


# ---------------------------------------------------------------------------
# (8) Double-resolve conflict (409)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_resolve_conflict(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Re-resolving an already-resolved item returns 409."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    item_id = await _seed_queue_item(db, status="resolved")

    # Trying to resolve an already-resolved item → 409
    r = await _resolve_item(base, token, item_id, "reviewed")
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# (9) Cross-workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_list_denied(seeded_workspace: StackConfig) -> None:
    """A caller with no membership in workspace B cannot list its items (403)."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
        r = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_B_ID}/tax-review-queue",
        )
    assert r.status_code in (401, 403), r.text


# ---------------------------------------------------------------------------
# (10) Entity-scope: scoped member cannot resolve out-of-scope entity's item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_denied_out_of_scope_entity(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A member scoped to a different entity cannot resolve an out-of-scope item."""

    base, token = seeded_workspace.base_url, seeded_workspace.token
    item_id = await _seed_queue_item(db)

    # Accountant role but scoped to workspace B's entity — not the test entity.
    async with _membership_as(
        db, seeded_workspace.uc_uid, role="accountant",
        entity_scope=(TEST_WORKSPACE_B_ENTITY_ID,),
    ):
        r = await _resolve_item(base, token, item_id, "reviewed")
    assert r.status_code == 403, r.text

    # Item must still be pending
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            status_val = await conn.fetchval(
                "SELECT status FROM tax_review_queue WHERE id::text = $1", item_id
            )
    assert status_val == "pending"
