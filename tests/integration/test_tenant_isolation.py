"""Tenant-isolation regression tests (audit findings C1 + C2 + H4).

These prove, against the LIVE stack, the object-level-authorization guarantees
(OWASP API1:2023 / BOLA) the fixes introduced:

  C1  A workspace with NO stored SimpleFIN credential returns its
      "not configured" / empty result — never another workspace's bank data,
      and never a 500. (The env-var fallback that caused the leak is also
      proven gone by tests/smoke/test_no_env_credential_fallback.py.)

  C2  A membership scoped to entity A (workspace_membership.entity_scope =
      [A]) is:
        * DENIED (403) on entity B's reports + verify-chain,
        * RESTRICTED (B's rows absent) on the consolidated journal listing,
          consolidated P&L, and posting-log,
        * 404 on a direct fetch of one of B's journal entries.
      A full-workspace member (entity_scope = None) still sees BOTH entities
      everywhere (the additive guarantee — owners/admins unaffected).

  H4  A 'viewer'-scoped membership can still READ (journal list, reports,
      cash-position, transactions) — read endpoints default to viewer-allowed;
      and the credential-metadata listing now requires accountant+.

Mechanism: the integration harness has one service token -> one uc_uid with an
owner membership. We exercise scope by TEMPORARILY rewriting that membership's
(role, entity_scope) in public.workspace_membership and restoring it in a
finally (mirrors the tamper test). Entity B + its COA/link are provisioned via
the real API + the asyncpg pool, idempotently.
"""
from __future__ import annotations

import datetime as dt

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_EXTERNAL_ACCOUNT_ID,
    TEST_SOURCE,
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

PERIOD_YEAR = 2026
EFFECTIVE_DATE = dt.date(PERIOD_YEAR, 2, 14).isoformat()


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


@pytest.fixture
async def two_entities(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
) -> StackConfig:
    """Workspace with entity A (from seeded_workspace) AND entity B, both with a
    seeded COA, COA 1010 linked to their own external account, and one posted
    journal entry each so reads have something to (not) see.
    """
    # 1. Create entity B, then (re)seed the COA so B gets its chart too.
    await _ensure_entity_b(db)
    r = await client.post(f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/seed")
    assert r.status_code == 200, r.text

    # 2. Link COA 1010 for entity B to B's external account.
    r = await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/coa/link",
        json={
            "entity_id": str(ENTITY_B_ID),
            "account_number": "1010",
            "source": TEST_SOURCE,
            "external_account_id": ENTITY_B_EXTERNAL_ACCOUNT,
        },
    )
    assert r.status_code == 200, r.text
    await _ensure_entity_b_mapping(db)

    # 3. Post one confirmed expense to EACH entity so each chain is non-empty.
    #    user_confirmed=True forces auto_post regardless of entity_mapped.
    tag = _run_tag()
    for eid, ext in ((TEST_ENTITY_ID, TEST_EXTERNAL_ACCOUNT_ID), (ENTITY_B_ID, ENTITY_B_EXTERNAL_ACCOUNT)):
        r = await client.post(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
            json={
                "entity_id": str(eid),
                "source": TEST_SOURCE,
                "source_external_id": f"iso-{eid}-{tag}",
                "effective_date": EFFECTIVE_DATE,
                "amount_cents": -4200,
                "description": "Isolation seed expense",
                "payee": "Anthropic",
                "category": "SaaS · AI",
                "category_status": "auto",
                "sign_matched": True,
                "external_account_id": ext,
                "user_confirmed": True,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json().get("outcome") == "posted", r.json()
    return seeded_workspace


@pytest.fixture
async def scoped_to_entity_a(db: asyncpg.Pool, two_entities: StackConfig):
    """Temporarily scope the test membership to entity A only (role=accountant).

    Restores the original (role, entity_scope) afterwards no matter what — so a
    failing assertion never leaves the shared membership in a scoped state.
    """
    stack = two_entities
    prev_role, prev_scope = await _set_membership(
        db, stack.uc_uid, "accountant", [str(TEST_ENTITY_ID)]
    )
    try:
        yield stack
    finally:
        await _set_membership(db, stack.uc_uid, prev_role, prev_scope)


@pytest.fixture
async def viewer_role(db: asyncpg.Pool, seeded_workspace: StackConfig):
    """Temporarily downgrade the test membership to 'viewer' (unscoped)."""
    stack = seeded_workspace
    prev_role, prev_scope = await _set_membership(db, stack.uc_uid, "viewer", None)
    try:
        yield stack
    finally:
        await _set_membership(db, stack.uc_uid, prev_role, prev_scope)


# ==========================================================================
# C1 — bank-data cross-workspace leak
# ==========================================================================
@pytest.mark.asyncio
async def test_cash_position_without_credential_is_not_configured(
    client: httpx.AsyncClient, seeded_workspace: StackConfig
) -> None:
    """No stored SimpleFIN credential => configured:false, empty accounts, no leak."""
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/cash-position")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is False, body
    assert body["accounts"] == [], body
    assert body["total_usd"] == "0.00", body


@pytest.mark.asyncio
async def test_recent_transactions_without_credential_is_graceful(
    client: httpx.AsyncClient, seeded_workspace: StackConfig
) -> None:
    """No SimpleFIN credential => no simplefin source, no 500, empty/`none`."""
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/recent-transactions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "simplefin" not in body["source"], body
    assert body["total_count"] == 0 or all(
        t["account_id"] for t in body["transactions"]
    ), body


@pytest.mark.asyncio
async def test_account_mappings_without_credential_discovers_nothing(
    client: httpx.AsyncClient, seeded_workspace: StackConfig
) -> None:
    """No SimpleFIN credential => no discovered live accounts (no 500)."""
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/account-mappings")
    assert r.status_code == 200, r.text
    body = r.json()
    # Discovery is skipped entirely without a credential.
    assert body["live_accounts"] == [], body


# ==========================================================================
# C2 — entity-scope enforcement (BOLA)
# ==========================================================================
@pytest.mark.asyncio
async def test_scoped_member_denied_on_other_entity_report(
    client: httpx.AsyncClient, scoped_to_entity_a: StackConfig
) -> None:
    """A-scoped member asking for entity B's P&L / balance-sheet / cash-flow / 1099
    is forbidden."""
    for path, params in (
        ("reports/pl", {"period": str(PERIOD_YEAR), "entity_id": str(ENTITY_B_ID)}),
        ("reports/balance-sheet", {"entity_id": str(ENTITY_B_ID)}),
        ("reports/cash-flow", {"period": str(PERIOD_YEAR), "entity_id": str(ENTITY_B_ID)}),
        ("reports/1099-nec", {"year": str(PERIOD_YEAR), "entity_id": str(ENTITY_B_ID)}),
    ):
        r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/{path}", params=params)
        assert r.status_code == 403, f"{path}: expected 403, got {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_scoped_member_allowed_on_own_entity_report(
    client: httpx.AsyncClient, scoped_to_entity_a: StackConfig
) -> None:
    """A-scoped member CAN read entity A's own P&L."""
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
        params={"period": str(PERIOD_YEAR), "entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["entity_id"] == str(TEST_ENTITY_ID)


@pytest.mark.asyncio
async def test_scoped_member_consolidated_report_excludes_other_entity(
    client: httpx.AsyncClient, db: asyncpg.Pool, scoped_to_entity_a: StackConfig
) -> None:
    """A-scoped member's CONSOLIDATED P&L must be restricted to A — never include
    B's totals. We compare scoped-consolidated against the entity-A-only report;
    they must match (B contributes nothing)."""
    r_consolidated = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
        params={"period": str(PERIOD_YEAR), "entity_id": "consolidated"},
    )
    assert r_consolidated.status_code == 200, r_consolidated.text
    r_a_only = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
        params={"period": str(PERIOD_YEAR), "entity_id": str(TEST_ENTITY_ID)},
    )
    assert r_a_only.status_code == 200, r_a_only.text
    a_only_total = float(r_a_only.json()["expenses"]["total"])
    consolidated_total = float(r_consolidated.json()["expenses"]["total"])
    # Non-vacuous: A genuinely has expenses (the fixture posted one), so this
    # isn't a "0 == 0" pass.
    assert a_only_total >= 42.0, r_a_only.json()
    # Restricted consolidated == A-only: B is invisible to the scoped member.
    # (If scope were ignored, consolidated would also include B's expense and
    # exceed A-only.)
    assert consolidated_total == a_only_total, (
        r_consolidated.json(),
        r_a_only.json(),
    )


@pytest.mark.asyncio
async def test_scoped_member_journal_list_excludes_other_entity(
    client: httpx.AsyncClient, scoped_to_entity_a: StackConfig
) -> None:
    """A-scoped member's consolidated journal listing must contain ZERO entity-B
    rows; an explicit entity_id=B request is forbidden."""
    # Consolidated listing — restricted to A.
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries", params={"limit": 500})
    assert r.status_code == 200, r.text
    entity_ids = {e["entity_id"] for e in r.json()["entries"]}
    assert str(ENTITY_B_ID) not in entity_ids, entity_ids
    # Non-vacuous: A's own entries ARE present (the ANY(:scope) predicate
    # matches A, it doesn't just return empty), and B is the only thing missing.
    assert str(TEST_ENTITY_ID) in entity_ids, entity_ids
    assert entity_ids == {str(TEST_ENTITY_ID)}, entity_ids

    # Explicit out-of-scope entity_id => 403.
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
        params={"entity_id": str(ENTITY_B_ID), "limit": 500},
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_scoped_member_cannot_fetch_other_entity_entry_detail(
    client: httpx.AsyncClient, db: asyncpg.Pool, scoped_to_entity_a: StackConfig
) -> None:
    """Direct GET of one of entity B's entries by id => 404 for an A-scoped
    member (the route previously had NO entity filter)."""
    # Find a journal_entry id belonging to entity B.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT id::text AS id FROM journal_entry WHERE entity_id = $1 LIMIT 1",
                str(ENTITY_B_ID),
            )
    assert row is not None, "fixture should have posted an entity-B entry"
    b_entry_id = row["id"]

    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/{b_entry_id}"
    )
    assert r.status_code == 404, f"scoped member must not read B's entry: {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_scoped_member_can_fetch_own_entry_detail(
    client: httpx.AsyncClient, db: asyncpg.Pool, scoped_to_entity_a: StackConfig
) -> None:
    """The same route still returns entity A's own entry to the A-scoped member."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT id::text AS id FROM journal_entry WHERE entity_id = $1 LIMIT 1",
                str(TEST_ENTITY_ID),
            )
    assert row is not None
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/{row['id']}"
    )
    assert r.status_code == 200, r.text
    assert r.json()["entity_id"] == str(TEST_ENTITY_ID)


@pytest.mark.asyncio
async def test_scoped_member_denied_verify_chain_other_entity(
    client: httpx.AsyncClient, scoped_to_entity_a: StackConfig
) -> None:
    """verify-chain on entity B => 403 for an A-scoped member; A is allowed."""
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
        params={"entity_id": str(ENTITY_B_ID)},
    )
    assert r.status_code == 403, r.text

    r_ok = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r_ok.status_code == 200, r_ok.text


@pytest.mark.asyncio
async def test_scoped_member_posting_log_excludes_other_entity(
    client: httpx.AsyncClient, scoped_to_entity_a: StackConfig
) -> None:
    """The posting-log feed must not surface entity-B attempts to an A-scoped member."""
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/posting-log", params={"limit": 500})
    assert r.status_code == 200, r.text
    entity_ids = {e["entity_id"] for e in r.json()["entries"]}
    assert str(ENTITY_B_ID) not in entity_ids, entity_ids


# ==========================================================================
# C2 — the ADDITIVE guarantee: a full-workspace member still sees everything.
# ==========================================================================
@pytest.mark.asyncio
async def test_full_workspace_member_sees_both_entities(
    client: httpx.AsyncClient, two_entities: StackConfig
) -> None:
    """With entity_scope = None (the default owner membership), BOTH entities are
    visible — proving the fix is additive and does not break the consolidated
    path for unscoped owners/admins."""
    # Consolidated journal listing includes BOTH A and B.
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries", params={"limit": 500})
    assert r.status_code == 200, r.text
    entity_ids = {e["entity_id"] for e in r.json()["entries"]}
    assert {str(TEST_ENTITY_ID), str(ENTITY_B_ID)} <= entity_ids, entity_ids

    # And entity B's own report + verify-chain + entry detail are all reachable.
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
        params={"period": str(PERIOD_YEAR), "entity_id": str(ENTITY_B_ID)},
    )
    assert r.status_code == 200, r.text

    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
        params={"entity_id": str(ENTITY_B_ID)},
    )
    assert r.status_code == 200, r.text


# ==========================================================================
# H4 — explicit role checks on reads
# ==========================================================================
@pytest.mark.asyncio
async def test_viewer_can_read_journal_and_reports(
    client: httpx.AsyncClient, viewer_role: StackConfig
) -> None:
    """A viewer (lowest role) can still READ — read endpoints are viewer-allowed."""
    for path, params in (
        ("journal-entries", {"limit": 10}),
        ("reports/pl", {"period": str(PERIOD_YEAR)}),
        ("cash-position", {}),
        ("recent-transactions", {}),
    ):
        r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/{path}", params=params)
        assert r.status_code == 200, f"{path}: viewer read denied: {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_viewer_cannot_list_credential_metadata(
    client: httpx.AsyncClient, viewer_role: StackConfig
) -> None:
    """Credential metadata listing now requires accountant+ — a viewer is 403."""
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/credentials")
    assert r.status_code == 403, f"viewer should not list credentials: {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_accountant_can_list_credential_metadata(
    client: httpx.AsyncClient, scoped_to_entity_a: StackConfig
) -> None:
    """An accountant CAN list credential metadata (returns a list, maybe empty)."""
    # scoped_to_entity_a sets role=accountant; credentials listing isn't entity-scoped.
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/credentials")
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
