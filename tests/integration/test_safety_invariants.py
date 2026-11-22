"""Safety-invariant tests — the guarantees that protect real financial data.

These are the load-bearing invariants for a ledger holding customers' money:

  (a) APPEND-ONLY: the forbid_journal_entry_mutation trigger rejects any
      UPDATE or DELETE on journal_entry. Corrections happen via reversing
      entries, never mutation.

  (b) MATERIALITY GATE: a transaction over $1000 (tier H) cannot auto-post.
      It is refused unless the caller passes user_confirmed=True (an explicit
      human confirmation, carried with a memo). No journal_entry row is
      written on refusal.

  (c) HASH-CHAIN TAMPER DETECTION: if a chained row is mutated at the storage
      layer (simulated here by temporarily disabling the append-only trigger,
      i.e. an attacker with raw DB write access), verify_chain reports the
      tampered entry by its position in the chain.

All run against the live stack; they SKIP if it isn't up.
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
    fetch_chain_rows,
    post_material_entry_via_approval,
)

pytestmark = pytest.mark.integration

_SCHEMA = "books"  # shared-schema model (was per-workspace accounting_ops_<uuid>); mirrors app.workspace_session.SHARED_SCHEMA
EFFECTIVE_DATE = dt.date(2026, 4, 1).isoformat()


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


async def _post_auto_expense(client: httpx.AsyncClient, ext_id: str, cents: int = -1500) -> str:
    """Post a clean tier-L auto-post expense and return its journal_entry_id."""
    r = await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
        json={
            "entity_id": str(TEST_ENTITY_ID),
            "source": TEST_SOURCE,
            "source_external_id": ext_id,
            "effective_date": EFFECTIVE_DATE,
            "amount_cents": cents,  # default -$15.00, tier L
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
# (a) Append-only
# ==========================================================================
@pytest.mark.asyncio
async def test_journal_entry_update_is_rejected(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    entry_id = await _post_auto_expense(client, f"txn-append-upd-{_run_tag()}")

    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
                await conn.execute(
                    "UPDATE journal_entry SET amount = amount + 1 WHERE id::text = $1",
                    entry_id,
                )
    assert "append-only" in str(excinfo.value).lower(), excinfo.value


@pytest.mark.asyncio
async def test_journal_entry_delete_is_rejected(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    entry_id = await _post_auto_expense(client, f"txn-append-del-{_run_tag()}")

    with pytest.raises(asyncpg.PostgresError) as excinfo:
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
                await conn.execute(
                    "DELETE FROM journal_entry WHERE id::text = $1", entry_id
                )
    assert "append-only" in str(excinfo.value).lower(), excinfo.value

    # The row is still there.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            still = await conn.fetchrow(
                "SELECT 1 FROM journal_entry WHERE id::text = $1", entry_id
            )
    assert still is not None


# ==========================================================================
# (b) Materiality gate
# ==========================================================================
@pytest.mark.asyncio
async def test_high_value_entry_without_confirm_is_blocked(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    ext_id = f"txn-material-block-{_run_tag()}"
    # -$1,500.00 => tier H. No user_confirmed => must be blocked.
    payload = {
        "entity_id": str(TEST_ENTITY_ID),
        "source": TEST_SOURCE,
        "source_external_id": ext_id,
        "effective_date": EFFECTIVE_DATE,
        "amount_cents": -150000,
        "description": "Big vendor invoice",
        "payee": "Anthropic",
        "category": "SaaS · AI",
        "category_status": "auto",
        "sign_matched": True,
        "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
        "user_confirmed": False,
    }
    r = await client.post(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries", json=payload
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False, body
    assert body["outcome"] == "blocked_materiality", body

    # No journal_entry row was written for this txn.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT 1 FROM journal_entry WHERE source_external_id = $1", ext_id
            )
    assert row is None, "blocked high-value entry must NOT create a journal_entry"


@pytest.mark.asyncio
async def test_high_value_entry_with_confirm_and_memo_posts(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """The same >$1000 entry posts once a human confirms it (with a memo).

    The integration caller is a service token (machine principal), so the HTTP
    post route refuses to count its user_confirmed claim (trust contract,
    app/api/journal.py) and the tier-H entry QUEUES (blocked_materiality /
    queued_for_confirm) until a human decision row applies it. We drive the
    REAL approval-confirm path: the queued entry is applied through
    POST /approvals/{id}/decide, which posts with user_confirmed=True and
    stamps the attesting human on the row (apply_proposal ->
    post_journal_entry). No product policy is weakened — the entry still goes
    through the materiality gate's queue and a decision.
    """
    ext_id = f"txn-material-confirm-{_run_tag()}"
    memo = "Confirmed annual Claude Enterprise prepayment — approved by the owner"
    entry_id = await post_material_entry_via_approval(
        db,
        seeded_workspace.base_url,
        seeded_workspace.token,
        ext_id=ext_id,
        amount_cents=-150000,
        category="SaaS · AI",
        description="Big vendor invoice",
        payee="Anthropic",
        memo=memo,
        effective_date=EFFECTIVE_DATE,
    )

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                """
                SELECT id::text AS id, status, approving_human_uc_uid, memo,
                       materiality_tier
                FROM journal_entry WHERE source_external_id = $1
                """,
                ext_id,
            )
    assert row is not None and row["status"] == "posted"
    assert row["id"] == entry_id
    # Tier-H materiality is recorded on the row (the gate saw the amount).
    assert row["materiality_tier"] == "H"
    # The approval-confirm decision records the approving human on the row.
    assert row["approving_human_uc_uid"] is not None
    assert row["memo"] == memo


# ==========================================================================
# (c) Hash-chain tamper detection
# ==========================================================================
@pytest.mark.asyncio
async def test_chain_tamper_is_detected(
    client: httpx.AsyncClient, db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    # Post three entries so the chain has interior links to tamper with.
    tag = _run_tag()
    ids = [await _post_auto_expense(client, f"txn-tamper-{tag}-{i}") for i in range(3)]

    # Chain is clean to start.
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tampered_count"] == 0, r.json()

    # Pick the middle of OUR three entries and find its position in the chain.
    target_id = ids[1]
    rows = await fetch_chain_rows(db, TEST_ENTITY_ID)
    positions = {row["id"]: idx for idx, row in enumerate(rows)}
    assert target_id in positions, "posted entry not found in chain"
    target_pos = positions[target_id]
    original_amount = next(r["amount"] for r in rows if r["id"] == target_id)

    # Tamper at the storage layer: temporarily disable the append-only trigger
    # (owner privilege) and mutate the amount. This models an attacker with raw
    # DB write access — the only way tampering can happen, since the app path
    # is UPDATE-proof.
    await _mutate_amount_bypassing_trigger(db, target_id, "999999.0000")
    try:
        r = await client.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
            params={"entity_id": str(TEST_ENTITY_ID)},
        )
        assert r.status_code == 200, r.text
        result = r.json()
        assert result["ok"] is False, result
        assert result["tampered_count"] >= 1, result
        tampered_positions = {t["position"] for t in result["tampered"]}
        # The mutated row breaks its own hash; the next row's prev_hash check
        # also breaks. Either way the tampered row's own position is reported.
        assert target_pos in tampered_positions, (
            f"expected tampered position {target_pos}, got {tampered_positions}"
        )
    finally:
        # Restore the original bytes so the chain re-verifies clean and the
        # suite stays re-runnable.
        await _mutate_amount_bypassing_trigger(db, target_id, str(original_amount))

    # Sanity: clean again after restore.
    r = await client.get(
        f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["tampered_count"] == 0, r.json()


async def _mutate_amount_bypassing_trigger(
    pool: asyncpg.Pool, entry_id: str, new_amount: str
) -> None:
    """Set journal_entry.amount, bypassing the append-only trigger.

    `ALTER TABLE ... DISABLE TRIGGER USER` is a table-owner operation; the test
    DB role owns the schema it created, so this works without superuser. We
    re-enable in the same transaction.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute("ALTER TABLE journal_entry DISABLE TRIGGER USER")
            try:
                await conn.execute(
                    "UPDATE journal_entry SET amount = $1::numeric WHERE id::text = $2",
                    new_amount,
                    entry_id,
                )
            finally:
                await conn.execute("ALTER TABLE journal_entry ENABLE TRIGGER USER")
