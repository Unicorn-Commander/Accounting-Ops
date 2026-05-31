"""Integration tests: COA REST + MCP entity_scope enforcement.

Closes audit findings (`audits/code-audit-2026-05-31.md`):
  * Bug 1 — REST `/api/workspaces/{wid}/coa` endpoints (GET / POST / PATCH /
    DELETE) must enforce `entity_scope` on every operation. A scoped CPA in
    workspace A whose membership only includes entity A must get 403 on every
    attempt to read/create/update/deactivate accounts on entity B in the same
    workspace.
  * Bug 2 — MCP `update_coa_account` must load the account's `entity_id` and
    enforce `entity_scope` before mutation.

Pattern mirrors tests/integration/test_tenant_isolation.py: use the
``scoped_to_entity_a`` style of temporarily rewriting the test membership to
``(role='accountant', entity_scope=[A])`` and asserting B is invisible/inaccessible.

A control test confirms an UNSCOPED admin/owner can still touch BOTH entities'
COA — proving the fix is additive and doesn't break the legitimate path.
"""
from __future__ import annotations

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.conftest import (
    ENTITY_B_ID,
    ENTITY_B_EXTERNAL_ACCOUNT,
    _ensure_entity_b,
    _ensure_entity_b_mapping,
    _set_membership,
)

pytestmark = pytest.mark.integration

_SCHEMA = f"accounting_ops_{str(TEST_WORKSPACE_ID).replace('-', '_')}"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture
async def two_entities_with_coa(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
) -> StackConfig:
    """Both entities A + B exist and have COA seeded.

    seeded_workspace seeds A's COA via the seed endpoint. We add entity B, then
    re-run /coa/seed (idempotent) so B gets the default chart too, and link
    COA 1010 on B to B's external account so we have a known target for the
    update/delete tests.
    """
    await _ensure_entity_b(db)
    r = await client.post(f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/seed")
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/link",
        json={
            "entity_id": str(ENTITY_B_ID),
            "account_number": "1010",
            "source": "simplefin",
            "external_account_id": ENTITY_B_EXTERNAL_ACCOUNT,
        },
    )
    assert r.status_code == 200, r.text
    await _ensure_entity_b_mapping(db)
    return seeded_workspace


@pytest.fixture
async def scoped_accountant_to_a(
    db: asyncpg.Pool, two_entities_with_coa: StackConfig
):
    """Temporarily scope the test membership to (accountant, [entity A]).

    Restores prior (role, scope) on exit no matter what — so a failing assert
    never leaves the shared membership in a scoped state.
    """
    stack = two_entities_with_coa
    prev_role, prev_scope = await _set_membership(
        db, stack.uc_uid, "accountant", [str(TEST_ENTITY_ID)]
    )
    try:
        yield stack
    finally:
        await _set_membership(db, stack.uc_uid, prev_role, prev_scope)


async def _b_account_id(db: asyncpg.Pool) -> str:
    """Return one of entity B's COA account UUIDs (the seeded 6210 SaaS · AI)."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                """
                SELECT id::text AS id
                FROM chart_of_account
                WHERE entity_id = $1 AND account_number = '6210'
                LIMIT 1
                """,
                str(ENTITY_B_ID),
            )
    assert row is not None, "entity B should have a seeded 6210 account"
    return row["id"]


async def _a_account_id(db: asyncpg.Pool) -> str:
    """Return one of entity A's COA account UUIDs (the seeded 6210)."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                """
                SELECT id::text AS id
                FROM chart_of_account
                WHERE entity_id = $1 AND account_number = '6210'
                LIMIT 1
                """,
                str(TEST_ENTITY_ID),
            )
    assert row is not None, "entity A should have a seeded 6210 account"
    return row["id"]


# ==========================================================================
# Bug 1 — REST list_coa: scoped CPA must be denied on out-of-scope entity
# ==========================================================================
@pytest.mark.asyncio
async def test_scoped_member_denied_listing_other_entity_coa(
    client: httpx.AsyncClient, scoped_accountant_to_a: StackConfig
) -> None:
    """A-scoped membership: GET /coa?entity_id=B -> 403 (Bug 1)."""
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa",
        params={"entity_id": str(ENTITY_B_ID)},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_scoped_member_allowed_listing_own_entity_coa(
    client: httpx.AsyncClient, scoped_accountant_to_a: StackConfig
) -> None:
    """A-scoped membership: GET /coa?entity_id=A still succeeds (no regression)."""
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["entity_id"] == str(TEST_ENTITY_ID)
    # Non-vacuous: A has a non-empty COA from the seed.
    assert len(r.json()["accounts"]) > 0


# ==========================================================================
# Bug 1 — REST create_coa_account: scoped CPA must be denied
# ==========================================================================
@pytest.mark.asyncio
async def test_scoped_member_denied_creating_account_on_other_entity(
    client: httpx.AsyncClient, scoped_accountant_to_a: StackConfig
) -> None:
    """A-scoped member POST /coa with body.entity_id=B -> 403 (Bug 1)."""
    r = await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa",
        json={
            "entity_id": str(ENTITY_B_ID),
            "account_number": "9990",
            "account_name": "Hostile · Create",
            "account_type": "expense",
        },
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"


# ==========================================================================
# Bug 1 — REST update_coa_account: scoped CPA must be denied
# ==========================================================================
@pytest.mark.asyncio
async def test_scoped_member_denied_updating_other_entity_account(
    client: httpx.AsyncClient, db: asyncpg.Pool,
    scoped_accountant_to_a: StackConfig,
) -> None:
    """A-scoped member PATCH /coa/{B-account-id} -> 403 (Bug 1).

    The pre-fix path joined chart_of_account -> entity by workspace_id only,
    so a known account UUID in another entity inside the same workspace would
    be mutated. After the fix the route loads the account's entity_id and
    enforces entity_scope on it.
    """
    b_acct_id = await _b_account_id(db)
    r = await client.patch(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/{b_acct_id}",
        json={"account_name": "Hostile · Renamed by scoped CPA"},
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"

    # Confirm the row was NOT mutated.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT account_name FROM chart_of_account WHERE id = $1",
                b_acct_id,
            )
    assert row is not None
    assert "Hostile" not in row["account_name"], (
        f"Update should have been blocked, but the row was renamed: {row['account_name']}"
    )


# ==========================================================================
# Bug 1 — REST deactivate_coa_account: scoped CPA must be denied
# ==========================================================================
@pytest.mark.asyncio
async def test_scoped_member_denied_deactivating_other_entity_account(
    client: httpx.AsyncClient, db: asyncpg.Pool,
    scoped_accountant_to_a: StackConfig,
) -> None:
    """A-scoped member DELETE /coa/{B-account-id} -> 403 (Bug 1)."""
    b_acct_id = await _b_account_id(db)
    r = await client.delete(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/{b_acct_id}"
    )
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"

    # Confirm the row is still active.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT is_active FROM chart_of_account WHERE id = $1",
                b_acct_id,
            )
    assert row is not None
    assert row["is_active"] is True, "delete should have been blocked"


# ==========================================================================
# Bug 1 control — scoped CPA can still touch their OWN entity's COA
# ==========================================================================
@pytest.mark.asyncio
async def test_scoped_member_can_still_update_own_entity_account(
    client: httpx.AsyncClient, db: asyncpg.Pool,
    scoped_accountant_to_a: StackConfig,
) -> None:
    """The fix must not regress legitimate in-scope access."""
    a_acct_id = await _a_account_id(db)
    r = await client.patch(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/{a_acct_id}",
        json={"account_name": "SaaS · AI (scope-test rename)"},
    )
    assert r.status_code == 200, f"in-scope update should succeed: {r.text}"
    assert r.json()["account_name"] == "SaaS · AI (scope-test rename)"


# ==========================================================================
# Bug 1 control — UNSCOPED owner can touch BOTH entities' COA
# ==========================================================================
@pytest.mark.asyncio
async def test_unscoped_owner_can_touch_any_entity_coa(
    client: httpx.AsyncClient, db: asyncpg.Pool,
    two_entities_with_coa: StackConfig,
) -> None:
    """Owner/admin (entity_scope=None) sees and mutates both entities. The
    seeded_workspace fixture leaves the test membership as ``owner`` with no
    entity_scope, so this asserts the additive guarantee: owners unaffected by
    the new entity_scope checks on the COA routes.
    """
    # List BOTH entities' COA.
    r_a = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r_a.status_code == 200, r_a.text
    r_b = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa",
        params={"entity_id": str(ENTITY_B_ID)},
    )
    assert r_b.status_code == 200, r_b.text
    assert len(r_a.json()["accounts"]) > 0
    assert len(r_b.json()["accounts"]) > 0

    # Update B's account (owner has access).
    b_acct_id = await _b_account_id(db)
    r_patch = await client.patch(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/{b_acct_id}",
        json={"account_name": "SaaS · AI (owner rename)"},
    )
    assert r_patch.status_code == 200, f"owner should update B: {r_patch.text}"


# ==========================================================================
# Bug 2 — MCP update_coa_account: scoped CPA must be denied
# ==========================================================================
@pytest.mark.asyncio
async def test_mcp_update_coa_account_scoped_member_denied(
    client: httpx.AsyncClient, db: asyncpg.Pool,
    scoped_accountant_to_a: StackConfig,
) -> None:
    """The MCP tool's pre-fix authorized the workspace with entity_id=None and
    then updated any account, letting a scoped CPA mutate B's COA by guessing a
    UUID. After the fix the tool loads entity_id and enforces entity_scope.

    Drive the tool through the mounted MCP HTTP surface so we exercise the
    real auth bridge (no in-process shortcut).
    """
    b_acct_id = await _b_account_id(db)
    # Streamable-HTTP POST to /mcp/v1/messages — the same surface FastMCP exposes.
    # We use the JSON-RPC envelope for the registered ``update_coa_account`` tool.
    rpc = {
        "jsonrpc": "2.0",
        "id": "test-bug2",
        "method": "tools/call",
        "params": {
            "name": "update_coa_account",
            "arguments": {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "account_id": b_acct_id,
                "account_name": "Hostile · MCP scoped CPA rename",
            },
        },
    }
    r = await client.post("/mcp/v1/messages", json=rpc)
    # The MCP tool raises PermissionError; FastMCP renders that as an error
    # result inside the JSON-RPC response (HTTP 200) or a 4xx — both are
    # acceptable denial signals so long as the row is not mutated.
    is_denied = r.status_code in (403, 200)
    assert is_denied, f"unexpected status {r.status_code}: {r.text}"
    if r.status_code == 200:
        body = r.json()
        # Either an explicit "error" envelope or a tool error inside result.
        error_text = (
            str(body.get("error", ""))
            + str(body.get("result", {}).get("isError", ""))
            + str(body.get("result", {}).get("content", ""))
        )
        # Be tolerant: any denial word is fine.
        assert any(
            kw in error_text.lower()
            for kw in ("scope", "permission", "forbid", "denied")
        ), f"expected denial in MCP error envelope, got: {body!r}"

    # The row MUST NOT have been mutated.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT account_name FROM chart_of_account WHERE id = $1",
                b_acct_id,
            )
    assert row is not None
    assert "Hostile" not in row["account_name"], (
        f"MCP update should have been blocked: {row['account_name']}"
    )
