"""DB-enforced tenant isolation + ledger immutability proofs (audit finding C3).

What the database now ENFORCES (vs. what the app previously only promised):

  ROLE SPLIT
    * The app connects as the least-privilege runtime role
      `accounting_ops_app` (it is the role behind the live stack the e2e +
      safety tests already drive — those passing under this role is itself a
      proof the app works end-to-end as the restricted role).
    * The owner role `accounting_ops` is used only by migrations + provisioning.

  THE RUNTIME ROLE CANNOT (proven here against a pool connected AS that role):
    * UPDATE or DELETE journal_entry            -> permission denied
    * TRUNCATE journal_entry / event_log        -> permission denied AND, even
                                                   for the owner, a BEFORE
                                                   TRUNCATE trigger RAISEs
    * ALTER TABLE journal_entry DISABLE TRIGGER -> must be owner (denied)

  RLS BACKSTOP (proven against the runtime-role pool):
    * With app.current_workspace set to the schema's own workspace, the role
      reads that workspace's rows (correct access still works).
    * With app.current_workspace set to ANOTHER workspace, OR unset, the same
      query returns ZERO rows — so a query that lands in the wrong schema
      (stale/missing search_path) leaks NOTHING.
    * Cross-tenant: pointed at workspace B's schema while scoped to workspace
      A, the runtime role sees zero of B's rows (a real cross-tenant read is
      blocked).
    * WITH CHECK: the role cannot INSERT a row whose workspace_id != the GUC.

These run against the LIVE stack and SKIP cleanly when it (or the split-role
DSN) isn't configured.
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
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)

pytestmark = pytest.mark.integration

_SCHEMA_A = f"accounting_ops_{str(TEST_WORKSPACE_ID).replace('-', '_')}"
_SCHEMA_B = f"accounting_ops_{str(TEST_WORKSPACE_B_ID).replace('-', '_')}"
EFFECTIVE_DATE = dt.date(2026, 4, 1).isoformat()


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


@pytest.fixture
async def workspace_b_has_a_row(db: asyncpg.Pool, seeded_workspace: StackConfig) -> None:
    """Ensure workspace B's journal_entry table has >=1 row.

    Inserted by the OWNER pool (bypasses RLS, can INSERT). B is provisioned by
    scripts/integration_env.py (schema + entity), but has no COA/membership, so
    we can't post through the app — and we don't need to: the cross-tenant proof
    is about the RUNTIME role being unable to READ B's rows. Idempotent.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_B}", public')
            eid = await conn.fetchval("SELECT id::text FROM entity LIMIT 1")
            assert eid is not None, "workspace B must be provisioned with an entity"
            existing = await conn.fetchval("SELECT count(*) FROM journal_entry")
            if existing == 0:
                await conn.execute(
                    """
                    INSERT INTO journal_entry (
                        id, workspace_id, entity_id, amount, currency_code,
                        usd_equivalent_at_event_time, posted_at, effective_date,
                        decision_verdict, identity, status,
                        debit_account, credit_account, category, materiality_tier,
                        source, source_external_id, description, payee
                    ) VALUES (
                        gen_random_uuid(), $1, $2, 42.0000, 'USD',
                        42.0000, now(), $3,
                        'Approve', 'rls-test-seed', 'posted',
                        '6210·SaaS', '1010·Cash', 'SaaS · AI', 'L',
                        'manual', $4, 'Workspace B private row', 'Vendor B'
                    )
                    """,
                    str(TEST_WORKSPACE_B_ID),
                    eid,
                    dt.date(2026, 4, 1),  # asyncpg needs a real date, not ISO str
                    f"wsb-seed-{_run_tag()}",
                )


async def _post_auto_expense(client: httpx.AsyncClient, ext_id: str, cents: int = -1500) -> str:
    """Post a clean tier-L auto-post expense (via the app) and return its id."""
    r = await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
        json={
            "entity_id": str(TEST_ENTITY_ID),
            "source": TEST_SOURCE,
            "source_external_id": ext_id,
            "effective_date": EFFECTIVE_DATE,
            "amount_cents": cents,
            "description": "Anthropic usage",
            "payee": "Anthropic",
            "memo": None,
            "category": "SaaS · AI",
            "category_status": "auto",
            "sign_matched": True,
            "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
            "user_confirmed": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["outcome"] == "posted", body
    return body["journal_entry_id"]


# ==========================================================================
# 0. The app works end-to-end AS the least-privilege runtime role.
# ==========================================================================
@pytest.mark.asyncio
async def test_app_posts_and_reads_under_runtime_role(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Posting + reading through the app proves the runtime role has exactly the
    privileges it needs (INSERT journal_entry/posting_log, SELECT to read back,
    INSERT/UPDATE chart_of_account via the seed/link in the fixture)."""
    entry_id = await _post_auto_expense(client, f"c3-runtime-ok-{_run_tag()}")
    # Read it back through the app (SELECT under the runtime role + RLS).
    r = await client.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/{entry_id}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "posted"


# ==========================================================================
# 1. The runtime role cannot UPDATE / DELETE the append-only ledger.
# ==========================================================================
@pytest.mark.asyncio
async def test_runtime_role_cannot_update_journal_entry(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    entry_id = await _post_auto_expense(client, f"c3-noupd-{_run_tag()}")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_A}", public')
                await conn.execute(
                    "SELECT set_config('app.current_workspace', $1, true)",
                    str(TEST_WORKSPACE_ID),
                )
                await conn.execute(
                    "UPDATE journal_entry SET amount = amount + 1 WHERE id::text = $1",
                    entry_id,
                )


@pytest.mark.asyncio
async def test_runtime_role_cannot_delete_journal_entry(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    entry_id = await _post_auto_expense(client, f"c3-nodel-{_run_tag()}")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_A}", public')
                await conn.execute(
                    "SELECT set_config('app.current_workspace', $1, true)",
                    str(TEST_WORKSPACE_ID),
                )
                await conn.execute("DELETE FROM journal_entry WHERE id::text = $1", entry_id)


# ==========================================================================
# 2. TRUNCATE is rejected — both by the missing grant (runtime role) and by
#    the BEFORE TRUNCATE trigger (which also stops the OWNER).
# ==========================================================================
@pytest.mark.asyncio
async def test_runtime_role_cannot_truncate_journal_entry(
    app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """The runtime role lacks the TRUNCATE privilege entirely."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_A}", public')
                await conn.execute("TRUNCATE journal_entry")


@pytest.mark.asyncio
async def test_owner_truncate_journal_entry_is_blocked_by_trigger(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Even the OWNER (who has TRUNCATE privilege + bypasses RLS) is stopped by
    the BEFORE TRUNCATE statement trigger — the row-level append-only trigger
    does NOT fire on TRUNCATE, so this guard is what closes that hole.

    CASCADE so the inbound FK from recon_link (whose FK check would otherwise
    raise first) is satisfied and execution actually reaches the journal_entry
    BEFORE TRUNCATE trigger, which RAISEs. Runs in a rolled-back transaction."""
    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_A}", public')
                await conn.execute("TRUNCATE journal_entry CASCADE")
    assert "truncate is forbidden" in str(excinfo.value).lower(), excinfo.value


@pytest.mark.asyncio
async def test_owner_truncate_event_log_is_blocked_by_trigger(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_A}", public')
                await conn.execute("TRUNCATE event_log")
    assert "truncate is forbidden" in str(excinfo.value).lower(), excinfo.value


# ==========================================================================
# 3. The runtime role cannot DISABLE the append-only trigger (needs ownership).
#    This closes the bypass the safety-invariant tamper test uses (it relies on
#    OWNER-level DISABLE TRIGGER to *simulate* an attacker — the runtime role
#    can't do it).
# ==========================================================================
@pytest.mark.asyncio
async def test_runtime_role_cannot_disable_append_only_trigger(
    app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_A}", public')
                await conn.execute("ALTER TABLE journal_entry DISABLE TRIGGER USER")


@pytest.mark.asyncio
async def test_runtime_role_cannot_set_session_replication_role(
    app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """session_replication_role='replica' would skip triggers — it's superuser-
    only, and the runtime role is NOSUPERUSER."""
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("SET session_replication_role = 'replica'")


# ==========================================================================
# 4. RLS backstop — keyed on app.current_workspace.
# ==========================================================================
async def _count_journal(conn: asyncpg.Connection, schema: str, workspace_for_guc) -> int:
    async with conn.transaction():
        await conn.execute(f'SET LOCAL search_path = "{schema}", public')
        if workspace_for_guc is not None:
            await conn.execute(
                "SELECT set_config('app.current_workspace', $1, true)",
                str(workspace_for_guc),
            )
        # else: leave the GUC unset for this transaction.
        return await conn.fetchval("SELECT count(*) FROM journal_entry")


@pytest.mark.asyncio
async def test_rls_correct_workspace_sees_rows(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Non-vacuous baseline: with the right GUC, the runtime role DOES see A's
    rows (so the zero-row results below aren't just 'always empty')."""
    await _post_auto_expense(client, f"c3-rls-baseline-{_run_tag()}")
    async with app_db.acquire() as conn:
        n = await _count_journal(conn, _SCHEMA_A, TEST_WORKSPACE_ID)
    assert n > 0, "runtime role should see workspace A's rows when the GUC matches"


@pytest.mark.asyncio
async def test_rls_unset_workspace_guc_sees_nothing(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Unset app.current_workspace => NULLIF(...)::uuid is NULL => default-deny.
    A stray query that forgot to scope sees zero rows, not the tenant's data."""
    await _post_auto_expense(client, f"c3-rls-unset-{_run_tag()}")
    async with app_db.acquire() as conn:
        n = await _count_journal(conn, _SCHEMA_A, workspace_for_guc=None)
    assert n == 0, "with no app.current_workspace, RLS must hide all rows"


@pytest.mark.asyncio
async def test_rls_wrong_workspace_guc_sees_nothing(
    client: httpx.AsyncClient, app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Pointed at A's schema (which has rows) but with the GUC set to workspace
    B: RLS filters workspace_id = B, and A's schema has only workspace_id = A
    rows => zero. This is EXACTLY the 'query landed in the wrong schema' case."""
    await _post_auto_expense(client, f"c3-rls-wrong-{_run_tag()}")
    async with app_db.acquire() as conn:
        n = await _count_journal(conn, _SCHEMA_A, TEST_WORKSPACE_B_ID)
    assert n == 0, "GUC=B against A's schema must reveal none of A's rows"


@pytest.mark.asyncio
async def test_rls_blocks_cross_tenant_read_with_wrong_search_path(
    app_db: asyncpg.Pool, workspace_b_has_a_row: None, seeded_workspace: StackConfig
) -> None:
    """The headline cross-tenant proof. The runtime role is scoped to workspace
    A (GUC=A) but its search_path points at workspace B's schema (modelling a
    missed/stale search_path). It must see ZERO of B's rows — RLS filters
    workspace_id = A and B's schema holds only workspace_id = B rows.

    Without RLS this would return B's entire journal_entry table — a textbook
    cross-tenant leak."""
    async with app_db.acquire() as conn:
        # Sanity: B's schema is reachable to the role and (under the matching
        # GUC) does contain rows, so the zero below is meaningful.
        b_rows_under_b = await _count_journal(conn, _SCHEMA_B, TEST_WORKSPACE_B_ID)
    assert b_rows_under_b > 0, (
        "workspace B should have been provisioned with a seeded journal entry; "
        "got an empty B — the cross-tenant assertion would be vacuous"
    )
    async with app_db.acquire() as conn:
        leaked = await _count_journal(conn, _SCHEMA_B, TEST_WORKSPACE_ID)  # GUC = A
    assert leaked == 0, "RLS must block reading workspace B's rows while scoped to A"


@pytest.mark.asyncio
async def test_rls_with_check_blocks_cross_workspace_insert(
    app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """The WITH CHECK clause: the runtime role cannot write a row whose
    workspace_id differs from the GUC, even into a mutable table it can INSERT
    into (chart_of_account). Prevents a confused/compromised query from planting
    rows under another workspace's id."""
    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with app_db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA_A}", public')
                await conn.execute(
                    "SELECT set_config('app.current_workspace', $1, true)",
                    str(TEST_WORKSPACE_ID),  # GUC = A
                )
                # Try to write a chart_of_account row stamped with workspace B.
                await conn.execute(
                    """
                    INSERT INTO chart_of_account (
                        workspace_id, entity_id, account_number, account_name,
                        canonical_form, account_type, normal_balance
                    ) VALUES (
                        $1, $2, '9999', 'RLS smuggle attempt',
                        '9999·RLS smuggle attempt', 'expense', 'debit'
                    )
                    """,
                    str(TEST_WORKSPACE_B_ID),  # workspace_id = B != GUC
                    str(TEST_ENTITY_ID),
                )
    msg = str(excinfo.value).lower()
    assert "row-level security" in msg or "row level security" in msg or "policy" in msg, excinfo.value


# ==========================================================================
# 5. Public-schema credential RLS (the C1 surface, now DB-enforced).
# ==========================================================================
@pytest.mark.asyncio
async def test_runtime_role_cannot_truncate_credential_access_log(
    app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("TRUNCATE public.credential_access_log")


@pytest.mark.asyncio
async def test_runtime_role_cannot_update_or_delete_credential_access_log(
    app_db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    # No UPDATE grant.
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("UPDATE public.credential_access_log SET success = false")
    # No DELETE grant.
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        async with app_db.acquire() as conn:
            await conn.execute("DELETE FROM public.credential_access_log")
