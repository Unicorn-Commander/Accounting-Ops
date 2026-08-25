"""Integration tests for the MCP WRITE tools (+ get_runway) over the live /mcp.

Drives each tool through a token-bound MCP streamable-HTTP client against the
real docker-compose stack (the same path an agent uses) and asserts:

  (a) the correct DB EFFECT
      * categorize  -> append-only correction: a reversal entry (reverses_entry_id
                       -> original) + a re-stated entry on the new category
                       account; the original is untouched; verify-chain stays green
      * link_receipt-> a recon_link row joining the receipt to the journal entry
      * add_rule    -> a compliance_rule under an INACTIVE profile (so it is NOT
                       returned by query_specialty_rules) + a pending approval_queue row
      * get_runway  -> a coherent runway projection with all inputs echoed
  (b) ROLE denial   — a viewer cannot drive a write tool
  (c) CROSS-WORKSPACE denial — the token cannot write into a workspace it isn't a
      member of (the workspace ARGUMENT can't grant reach)
  (d) ENTITY-SCOPE denial — a member scoped to entity X cannot write to entity Y

(b) and (d) flip the seeded membership's role / entity_scope in public.workspace_
membership (owner pool), exercise the REAL _resolve_membership path with the REAL
token, then restore — so the denial is proven end-to-end through the token, not a
mock. All DB-effect assertions read back through the owner pool.

Skips cleanly when the stack isn't reachable.
"""
from __future__ import annotations

import contextlib
import datetime as dt

import asyncpg
import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_EXTERNAL_ACCOUNT_ID,
    TEST_SOURCE,
    TEST_WORKSPACE_B_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.test_mcp_tools import _call_tool, _result_payload

pytestmark = pytest.mark.integration

_SCHEMA = "books"  # shared-schema model (was per-workspace accounting_ops_<uuid>); mirrors app.workspace_session.SHARED_SCHEMA
EFFECTIVE_DATE = dt.date(2026, 2, 10).isoformat()


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


# --------------------------------------------------------------------------
# Seeding helpers (through the real product surface where possible).
# --------------------------------------------------------------------------
async def _post_auto_expense(
    base_url: str, token: str, ext_id: str, cents: int = -1500,
    category: str = "SaaS · AI",
) -> str:
    """Post a clean tier-L auto-post expense via HTTP; return its entry id."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        r = await c.post(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
            json={
                "entity_id": str(TEST_ENTITY_ID),
                "source": TEST_SOURCE,
                "source_external_id": ext_id,
                "effective_date": EFFECTIVE_DATE,
                "amount_cents": cents,
                "description": "Anthropic usage",
                "payee": "Anthropic",
                "category": category,
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


async def _insert_receipt(db: asyncpg.Pool, *, content_hash: str) -> str:
    """Seed a receipt row for the test entity (owner pool). Returns its id."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            return await conn.fetchval(
                """
                INSERT INTO receipt (
                    id, workspace_id, entity_id, content_hash, garage_bucket,
                    object_key, mime_type, amount, currency_code
                ) VALUES (
                    gen_random_uuid(), $1, $2, $3, 'acctops-receipts',
                    $4, 'application/pdf', 15.0000, 'USD'
                )
                RETURNING id::text
                """,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
                content_hash,
                f"receipts/{content_hash}.pdf",
            )


@contextlib.asynccontextmanager
async def _membership_as(db: asyncpg.Pool, uc_uid: str, *, role: str, entity_scope):
    """Temporarily set the token's workspace_membership role + entity_scope, then
    restore it to the seeded owner/unscoped state. Drives the REAL authz path."""
    async with db.acquire() as conn:
        prev = await conn.fetchrow(
            """
            SELECT m.role::text AS role, m.entity_scope
            FROM public.workspace_membership m
            JOIN public.user_account u ON u.id = m.user_id
            WHERE u.uc_uid = $1 AND m.workspace_id = $2
            """,
            uc_uid, str(TEST_WORKSPACE_ID),
        )
        assert prev is not None, "seeded membership must exist"
    scope_json = None
    if entity_scope is not None:
        import json
        scope_json = json.dumps([str(e) for e in entity_scope])
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE public.workspace_membership m
            SET role = $3::workspace_role, entity_scope = $4::jsonb
            FROM public.user_account u
            WHERE u.id = m.user_id AND u.uc_uid = $1 AND m.workspace_id = $2
            """,
            uc_uid, str(TEST_WORKSPACE_ID), role, scope_json,
        )
    try:
        yield
    finally:
        async with db.acquire() as conn:
            await conn.execute(
                """
                UPDATE public.workspace_membership m
                SET role = $3::workspace_role, entity_scope = $4::jsonb
                FROM public.user_account u
                WHERE u.id = m.user_id AND u.uc_uid = $1 AND m.workspace_id = $2
                """,
                uc_uid, str(TEST_WORKSPACE_ID), prev["role"], prev["entity_scope"],
            )


def _is_error(result) -> bool:
    return bool(getattr(result, "isError", False))


def _error_text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content).lower()


# ==========================================================================
# categorize — append-only correction
# ==========================================================================
@pytest.mark.asyncio
async def test_categorize_appends_reversal_and_restatement(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A tier-L recategorization appends a reversal + a re-stated entry on the new
    category account, leaves the original posted, and keeps verify-chain green."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    ext = f"mcp-cat-{_run_tag()}"
    # Original posts to 6210 (SaaS · AI). Recategorize to 6110 (Infra · VPS).
    original_id = await _post_auto_expense(base, token, ext, cents=-1500, category="SaaS · AI")

    result = await _call_tool(
        base, token, "categorize_transaction",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "id": original_id,
            "category": "Infrastructure · VPS",
            "reasoning": "Reclassified: this is a VPS line, not AI SaaS.",
        },
    )
    assert not _is_error(result), result.content
    payload = _result_payload(result)
    assert payload["outcome"] == "applied", payload
    assert payload["from_category"] == "SaaS · AI"
    assert payload["to_category"] == "Infrastructure · VPS"
    reversal_id = payload["reversal_entry_id"]
    restated_id = payload["restated_entry_id"]

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            # Original untouched + still posted.
            orig = await conn.fetchrow(
                "SELECT status, category, debit_account, credit_account "
                "FROM journal_entry WHERE id::text = $1", original_id,
            )
            # Reversal points back at the original and flips debit/credit.
            rev = await conn.fetchrow(
                "SELECT reverses_entry_id::text AS rev, debit_account, credit_account, "
                "source FROM journal_entry WHERE id::text = $1", reversal_id,
            )
            # Restatement lands the category side on 6110 and keeps cash (1010).
            new = await conn.fetchrow(
                "SELECT category, debit_account, credit_account, source "
                "FROM journal_entry WHERE id::text = $1", restated_id,
            )
            # A decision_audit row was written for the categorize call.
            audits = await conn.fetchval(
                "SELECT count(*) FROM decision_audit "
                "WHERE tool_name = 'categorize' AND entity_id = $1",
                TEST_ENTITY_ID,
            )

    assert orig["status"] == "posted" and orig["category"] == "SaaS · AI"
    # Original expense: DR 6210, CR 1010. Reversal flips: DR 1010, CR 6210.
    assert rev["rev"] == original_id
    assert rev["source"] == "reversal"
    assert rev["debit_account"] == orig["credit_account"]
    assert rev["credit_account"] == orig["debit_account"]
    # Restatement: cash side (1010) unchanged on debit-or-credit, category -> 6110.
    assert new["category"] == "Infrastructure · VPS"
    assert new["debit_account"].startswith("6110"), new["debit_account"]
    assert new["credit_account"].startswith("1010"), new["credit_account"]
    assert audits >= 1

    # Chain still verifies clean after the two appended entries.
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
        r = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
            params={"entity_id": str(TEST_ENTITY_ID)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True, r.json()


@pytest.mark.asyncio
async def test_categorize_denied_for_viewer(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    original_id = await _post_auto_expense(base, token, f"mcp-cat-viewer-{_run_tag()}")
    async with _membership_as(db, seeded_workspace.uc_uid, role="viewer", entity_scope=None):
        result = await _call_tool(
            base, token, "categorize_transaction",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "id": original_id,
                "category": "Infrastructure · VPS",
                "reasoning": "viewer attempt",
            },
        )
    assert _is_error(result), "viewer must be denied the categorize write"
    assert "insufficient" in _error_text(result) or "permission" in _error_text(result)


@pytest.mark.asyncio
async def test_categorize_denied_cross_workspace(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Targeting workspace B (no membership) is denied even though B is valid."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    result = await _call_tool(
        base, token, "categorize_transaction",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "id": "00000000-0000-0000-0000-000000000000",
            "category": "Infrastructure · VPS",
            "reasoning": "cross-tenant attempt",
        },
    )
    assert _is_error(result), "cross-workspace categorize must be denied"
    txt = _error_text(result)
    assert "membership" in txt or "permission" in txt or "no active" in txt


@pytest.mark.asyncio
async def test_categorize_denied_out_of_scope_entity(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A member scoped to workspace B's entity id (NOT the test entity) is denied
    writing to the test entity — entity-scope enforced through the real token."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    original_id = await _post_auto_expense(base, token, f"mcp-cat-scope-{_run_tag()}")
    # Scope the membership to an entity that is NOT TEST_ENTITY_ID.
    async with _membership_as(
        db, seeded_workspace.uc_uid, role="accountant",
        entity_scope=(TEST_WORKSPACE_B_ENTITY_ID,),
    ):
        result = await _call_tool(
            base, token, "categorize_transaction",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "id": original_id,
                "category": "Infrastructure · VPS",
                "reasoning": "out-of-scope attempt",
            },
        )
    assert _is_error(result), "out-of-scope entity must be denied"
    assert "scope" in _error_text(result) or "permission" in _error_text(result)


# ==========================================================================
# link_receipt — recon_link row
# ==========================================================================
@pytest.mark.asyncio
async def test_link_receipt_creates_recon_link(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    tx_id = await _post_auto_expense(base, token, f"mcp-link-{_run_tag()}")
    receipt_id = await _insert_receipt(db, content_hash=f"hash{_run_tag()}")

    result = await _call_tool(
        base, token, "link_receipt_to_transaction",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "receipt_id": receipt_id,
            "tx_id": tx_id,
        },
    )
    assert not _is_error(result), result.content
    payload = _result_payload(result)
    assert payload["created"] is True
    link_id = payload["recon_link_id"]

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT journal_entry_id::text AS je, receipt_id::text AS rid, "
                "amount, currency_code FROM recon_link WHERE id::text = $1", link_id,
            )
    assert row is not None, "recon_link row must exist"
    assert row["je"] == tx_id
    assert row["rid"] == receipt_id

    # Idempotent: linking again returns the SAME link, created=False.
    again = await _call_tool(
        base, token, "link_receipt_to_transaction",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "receipt_id": receipt_id,
            "tx_id": tx_id,
        },
    )
    assert not _is_error(again), again.content
    again_payload = _result_payload(again)
    assert again_payload["created"] is False
    assert again_payload["recon_link_id"] == link_id


@pytest.mark.asyncio
async def test_link_receipt_denied_for_viewer(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    tx_id = await _post_auto_expense(base, token, f"mcp-link-viewer-{_run_tag()}")
    receipt_id = await _insert_receipt(db, content_hash=f"hashv{_run_tag()}")
    async with _membership_as(db, seeded_workspace.uc_uid, role="viewer", entity_scope=None):
        result = await _call_tool(
            base, token, "link_receipt_to_transaction",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "receipt_id": receipt_id,
                "tx_id": tx_id,
            },
        )
    assert _is_error(result), "viewer must be denied the link_receipt write"


@pytest.mark.asyncio
async def test_link_receipt_denied_cross_workspace(
    seeded_workspace: StackConfig,
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    result = await _call_tool(
        base, token, "link_receipt_to_transaction",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "receipt_id": "00000000-0000-0000-0000-000000000000",
            "tx_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert _is_error(result), "cross-workspace link_receipt must be denied"


@pytest.mark.asyncio
async def test_link_receipt_denied_out_of_scope_entity(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    tx_id = await _post_auto_expense(base, token, f"mcp-link-scope-{_run_tag()}")
    receipt_id = await _insert_receipt(db, content_hash=f"hashs{_run_tag()}")
    async with _membership_as(
        db, seeded_workspace.uc_uid, role="accountant",
        entity_scope=(TEST_WORKSPACE_B_ENTITY_ID,),
    ):
        result = await _call_tool(
            base, token, "link_receipt_to_transaction",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "receipt_id": receipt_id,
                "tx_id": tx_id,
            },
        )
    assert _is_error(result), "out-of-scope entity must be denied"


# ==========================================================================
# add_rule — proposed (inactive) compliance_rule + approval
# ==========================================================================
@pytest.mark.asyncio
async def test_add_rule_creates_pending_inactive_rule(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    kind = f"expense_cap_{_run_tag()}"
    result = await _call_tool(
        base, token, "add_specialty_rule",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "kind": kind,
            "params": {"max_usd": 500, "action": "require_approval"},
            "reasoning": "Cap discretionary SaaS at $500/mo for this entity.",
        },
    )
    assert not _is_error(result), result.content
    payload = _result_payload(result)
    assert payload["outcome"] == "queued_for_approval", payload
    assert payload["active"] is False
    rule_id = payload["rule_id"]
    approval_id = payload["approval_id"]

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            rule = await conn.fetchrow(
                """
                SELECT r.kind, r.source::text AS source, p.active AS profile_active
                FROM compliance_rule r JOIN compliance_profile p ON p.id = r.profile_id
                WHERE r.id::text = $1
                """,
                rule_id,
            )
            approval = await conn.fetchrow(
                "SELECT subject_type, subject_id::text AS sid, status::text AS status "
                "FROM approval_queue WHERE id::text = $1", approval_id,
            )
    assert rule is not None
    assert rule["kind"] == kind
    assert rule["source"] == "ai_inferred"
    assert rule["profile_active"] is False, "proposed rule must be under an INACTIVE profile"
    assert approval is not None
    assert approval["subject_type"] == "compliance_rule"
    assert approval["sid"] == rule_id
    assert approval["status"] == "pending"

    # Crucially: query_specialty_rules must NOT surface the pending rule (its
    # profile is inactive) — i.e. it is not yet enforced.
    listed = await _call_tool(
        base, token, "query_specialty_rules",
        {"workspace_id": str(TEST_WORKSPACE_ID), "entity_id": str(TEST_ENTITY_ID)},
    )
    rows = _result_payload(listed)
    rows = rows.get("result", rows) if isinstance(rows, dict) else rows
    assert all(r.get("kind") != kind for r in rows), "pending rule must be hidden until activated"


@pytest.mark.asyncio
async def test_add_rule_denied_for_accountant_below_admin(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """add_rule requires admin (the HTTP route's floor). An accountant is denied."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    async with _membership_as(db, seeded_workspace.uc_uid, role="accountant", entity_scope=None):
        result = await _call_tool(
            base, token, "add_specialty_rule",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "kind": f"k{_run_tag()}",
                "params": {"max_usd": 1},
                "reasoning": "accountant attempt",
            },
        )
    assert _is_error(result), "accountant (< admin) must be denied add_rule"


@pytest.mark.asyncio
async def test_add_rule_denied_cross_workspace(seeded_workspace: StackConfig) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    result = await _call_tool(
        base, token, "add_specialty_rule",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "kind": f"k{_run_tag()}",
            "params": {"max_usd": 1},
            "reasoning": "cross-tenant attempt",
        },
    )
    assert _is_error(result), "cross-workspace add_rule must be denied"


@pytest.mark.asyncio
async def test_add_rule_denied_out_of_scope_entity(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    async with _membership_as(
        db, seeded_workspace.uc_uid, role="admin",
        entity_scope=(TEST_WORKSPACE_B_ENTITY_ID,),
    ):
        result = await _call_tool(
            base, token, "add_specialty_rule",
            {
                "workspace_id": str(TEST_WORKSPACE_ID),
                "entity_id": str(TEST_ENTITY_ID),
                "kind": f"k{_run_tag()}",
                "params": {"max_usd": 1},
                "reasoning": "out-of-scope attempt",
            },
        )
    assert _is_error(result), "out-of-scope entity must be denied for add_rule"


# ==========================================================================
# get_runway — read tool
# ==========================================================================
@pytest.mark.asyncio
async def test_get_runway_projects_from_journal(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """get_runway returns a coherent projection: it echoes its inputs and, when
    the trailing window shows net burn, months_of_runway = cash / monthly burn."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    today = dt.date.today().isoformat()
    result = await _call_tool(
        base, token, "get_runway",
        {
            "workspace_id": str(TEST_WORKSPACE_ID),
            "entity_id": str(TEST_ENTITY_ID),
            "scenario": {"window_months": 3, "as_of": today},
        },
    )
    assert not _is_error(result), result.content
    data = _result_payload(result)
    # Structurally a runway projection with all inputs echoed.
    assert "months_of_runway" in data
    assert data["status"] in ("burning", "cash_flow_positive")
    inputs = data["inputs"]
    for key in (
        "current_cash", "window_months", "window_start", "window_end",
        "trailing_revenue", "trailing_expenses", "trailing_net_income",
        "avg_monthly_net_burn",
    ):
        assert key in inputs, f"missing runway input: {key}"
    assert inputs["window_months"] == 3
    assert data["as_of"] == today
    # If burning, the headline equals cash / monthly burn (to cents).
    if data["status"] == "burning":
        from decimal import Decimal
        cash = Decimal(inputs["current_cash"])
        burn = Decimal(inputs["avg_monthly_net_burn"])
        expected = (cash / burn).quantize(Decimal("0.01"))
        assert Decimal(data["months_of_runway"]) == expected, (data, expected)
    else:
        assert data["months_of_runway"] is None


@pytest.mark.asyncio
async def test_get_runway_denied_cross_workspace(seeded_workspace: StackConfig) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    result = await _call_tool(
        base, token, "get_runway",
        {
            "workspace_id": str(TEST_WORKSPACE_B_ID),
            "entity_id": str(TEST_WORKSPACE_B_ENTITY_ID),
            "scenario": {},
        },
    )
    assert _is_error(result), "cross-workspace get_runway must be denied"
