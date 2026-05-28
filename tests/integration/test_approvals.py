"""Integration tests for the approval-queue apply workflow (live stack).

Closes the gap the feature fixes: material actions QUEUE but couldn't be
finalized. These prove the full loop end-to-end against the real app + DB:

  (1) material recat -> QUEUED (approval_queue 'pending' with structured payload)
      -> APPROVE -> the held reversal + restatement are APPLIED (original
      untouched, restated entry on the new category account), the approving human
      is stamped on the appended entries, and verify-chain stays GREEN.
  (2) material recat -> QUEUED -> REJECT -> ledger UNTOUCHED (no reversal/restate),
      row marked rejected.
  (3) proposed rule (add_specialty_rule, inactive) -> APPROVE -> becomes ACTIVE,
      i.e. now visible to query_specialty_rules.
  (4) RBAC: a viewer cannot list approvals; an accountant (< admin) cannot decide.
  (5) cross-workspace + out-of-scope-entity decide are denied.

All assertions read back through the owner DB pool. The decide calls go through
the REAL HTTP route with the REAL token; RBAC denials flip the seeded membership
(then restore) to drive the real authz path. Skips cleanly when the stack is
down.
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
    TEST_WORKSPACE_B_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.conftest import fetch_chain_rows
from tests.integration.test_mcp_write_tools import _membership_as

pytestmark = pytest.mark.integration

_SCHEMA = f"accounting_ops_{str(TEST_WORKSPACE_ID).replace('-', '_')}"
EFFECTIVE_DATE = dt.date(2026, 3, 5).isoformat()


def _run_tag() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")


async def _post_expense(
    base_url: str, token: str, ext_id: str, cents: int, category: str = "SaaS · AI",
) -> str:
    """Post one expense via HTTP, returning its journal_entry_id.

    Tier-M (e.g. -$300) needs user_confirmed=True to post in the first place (so
    we have an entry to later RECATEGORIZE). The recat of that posted entry is
    what then queues, since post_correction passes user_confirmed=False.
    """
    tier_h_or_m = abs(cents) >= 7_500  # >= $75 -> not tier L
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
                "description": "Vendor charge",
                "payee": "Anthropic",
                "memo": "seed for approval test",
                "category": category,
                "category_status": "auto",
                "sign_matched": True,
                "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
                # tier M/H won't auto-post; confirm so the seed entry lands.
                "user_confirmed": bool(tier_h_or_m),
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["outcome"] == "posted", body
    return body["journal_entry_id"]


async def _queue_material_recat(
    db: asyncpg.Pool, original_id: str, new_category: str,
) -> str:
    """Reproduce the 'material recat queued' state that post_correction writes.

    The production queue path is categorize_transaction (MCP tool) ->
    post_correction, which on a material (M/H) recat inserts a 'pending'
    approval_queue row carrying the structured action in payload. The HTTP
    categorize route can't be used from a black-box test (those routes call the
    MCP-tool fns which require the /mcp auth contextvar — a separate pre-existing
    limitation), and the MCP client isn't installed on the test host. So we seed
    the SAME approval_queue row post_correction would write — exact subject_type
    / subject_id (the ORIGINAL entry) / status / payload shape — via the owner
    pool. The thing under test (approve/reject -> apply) then runs entirely
    through the real /approvals/.../decide HTTP endpoint + the shared apply
    primitive. Returns the approval_queue id.
    """
    import json
    import uuid as _uuid

    approval_id = str(_uuid.uuid4())
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchrow(
                "SELECT amount, category FROM journal_entry WHERE id::text = $1",
                original_id,
            )
            from_category = row["category"] or "Uncategorized"
            amount_cents = int(row["amount"] * 100)
            payload = {
                "action": "recategorize",
                "original_entry_id": original_id,
                "from_category": from_category,
                "new_category": new_category,
                "amount_cents": amount_cents,
                "agent_reasoning": "Reclass — material, must be human-approved.",
                "materiality_tier": "M",
            }
            await conn.execute(
                """
                INSERT INTO approval_queue (
                    id, workspace_id, entity_id, subject_type, subject_id,
                    amount, currency_code, usd_equivalent_at_event_time,
                    status, reasoning, requested_by_identity, payload
                ) VALUES (
                    $1, $2, $3, 'journal_recategorization', $4,
                    $5, 'USD', $5, 'pending', $6, 'test-agent', $7::jsonb
                )
                """,
                approval_id,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
                original_id,
                row["amount"],
                f"Re-categorize entry {original_id} from '{from_category}' to "
                f"'{new_category}'. Material — awaits human confirm.",
                json.dumps(payload),
            )
    return approval_id


async def _verify_chain(base_url: str, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        r = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries/verify-chain",
            params={"entity_id": str(TEST_ENTITY_ID)},
        )
    assert r.status_code == 200, r.text
    return r.json()


async def _decide(
    base_url: str, token: str, approval_id: str, decision: str,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as c:
        return await c.post(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/approvals/{approval_id}/decide",
            json={"decision": decision},
        )


# ==========================================================================
# (1) Approve a material recat -> reversal + restatement applied; chain green
# ==========================================================================
@pytest.mark.asyncio
async def test_approve_material_recat_applies_reversal_and_restatement(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    # -$300 => tier M. Post it (confirmed), then recategorize -> queues.
    original_id = await _post_expense(
        base, token, f"appr-recat-{_run_tag()}", cents=-30000, category="SaaS · AI"
    )

    # Chain clean before.
    assert (await _verify_chain(base, token))["ok"] is True

    approval_id = await _queue_material_recat(
        db, original_id, "Infrastructure · VPS"
    )

    # The approval is pending with a structured payload; the LEDGER is UNTOUCHED.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            aq = await conn.fetchrow(
                "SELECT subject_type, subject_id::text AS sid, status::text AS status, "
                "payload FROM approval_queue WHERE id::text = $1", approval_id,
            )
            # No reversal of the original exists yet.
            rev_before = await conn.fetchval(
                "SELECT count(*) FROM journal_entry WHERE reverses_entry_id::text = $1",
                original_id,
            )
    assert aq["subject_type"] == "journal_recategorization"
    assert aq["sid"] == original_id
    assert aq["status"] == "pending"
    import json
    payload = aq["payload"] if isinstance(aq["payload"], dict) else json.loads(aq["payload"])
    assert payload["new_category"] == "Infrastructure · VPS"
    assert payload["action"] == "recategorize"
    assert rev_before == 0, "queued recat must NOT touch the ledger"

    # APPROVE — applies the held recat.
    r = await _decide(base, token, approval_id, "approve")
    assert r.status_code == 200, r.text
    decided = r.json()
    assert decided["ok"] is True and decided["decision"] == "approved"
    assert decided["applied"] is True
    reversal_id = decided["reversal_entry_id"]
    restated_id = decided["restated_entry_id"]

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            orig = await conn.fetchrow(
                "SELECT status, category, debit_account, credit_account "
                "FROM journal_entry WHERE id::text = $1", original_id,
            )
            rev = await conn.fetchrow(
                "SELECT reverses_entry_id::text AS rev, debit_account, credit_account, "
                "source, approving_human_uc_uid FROM journal_entry WHERE id::text = $1",
                reversal_id,
            )
            new = await conn.fetchrow(
                "SELECT category, debit_account, credit_account, source, "
                "approving_human_uc_uid FROM journal_entry WHERE id::text = $1",
                restated_id,
            )
            aq_after = await conn.fetchrow(
                "SELECT status::text AS status, decided_by_uc_uid, decided_at "
                "FROM approval_queue WHERE id::text = $1", approval_id,
            )
            audit = await conn.fetchval(
                "SELECT count(*) FROM decision_audit "
                "WHERE tool_name = 'approval_decision' AND outcome = 'approved' "
                "AND entity_id = $1", TEST_ENTITY_ID,
            )

    # Original is untouched + still posted (append-only).
    assert orig["status"] == "posted" and orig["category"] == "SaaS · AI"
    # Reversal points back at the original and flips debit/credit.
    assert rev["rev"] == original_id and rev["source"] == "reversal"
    assert rev["debit_account"] == orig["credit_account"]
    assert rev["credit_account"] == orig["debit_account"]
    # Restatement lands the category side on 6110 and keeps cash (1010).
    assert new["category"] == "Infrastructure · VPS"
    assert new["debit_account"].startswith("6110"), new["debit_account"]
    assert new["credit_account"].startswith("1010"), new["credit_account"]
    # The approving HUMAN is stamped on BOTH appended entries (the attestation).
    assert rev["approving_human_uc_uid"] == seeded_workspace.uc_uid
    assert new["approving_human_uc_uid"] == seeded_workspace.uc_uid
    # Row marked decided + a decision_audit row written.
    assert aq_after["status"] == "approved"
    assert aq_after["decided_by_uc_uid"] == seeded_workspace.uc_uid
    assert aq_after["decided_at"] is not None
    assert audit >= 1

    # And the chain still verifies clean after the two appended entries.
    assert (await _verify_chain(base, token))["ok"] is True


# ==========================================================================
# (2) Reject a material recat -> ledger untouched
# ==========================================================================
@pytest.mark.asyncio
async def test_reject_material_recat_leaves_ledger_untouched(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    original_id = await _post_expense(
        base, token, f"appr-rej-{_run_tag()}", cents=-30000, category="SaaS · AI"
    )
    approval_id = await _queue_material_recat(
        db, original_id, "Infrastructure · VPS"
    )

    rows_before = await fetch_chain_rows(db, TEST_ENTITY_ID)

    r = await _decide(base, token, approval_id, "reject")
    assert r.status_code == 200, r.text
    decided = r.json()
    assert decided["decision"] == "rejected" and decided["applied"] is False

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            aq = await conn.fetchrow(
                "SELECT status::text AS status, decided_by_uc_uid "
                "FROM approval_queue WHERE id::text = $1", approval_id,
            )
            rev = await conn.fetchval(
                "SELECT count(*) FROM journal_entry WHERE reverses_entry_id::text = $1",
                original_id,
            )
            audit = await conn.fetchval(
                "SELECT count(*) FROM decision_audit "
                "WHERE tool_name = 'approval_decision' AND outcome = 'rejected' "
                "AND entity_id = $1", TEST_ENTITY_ID,
            )

    assert aq["status"] == "rejected"
    assert aq["decided_by_uc_uid"] == seeded_workspace.uc_uid
    assert rev == 0, "reject must NOT post a reversal"
    assert audit >= 1

    # No NEW chain rows were appended by the reject.
    rows_after = await fetch_chain_rows(db, TEST_ENTITY_ID)
    assert len(rows_after) == len(rows_before), "reject must not append ledger rows"

    # Re-deciding a decided approval is a clean 409 (not a double-apply).
    again = await _decide(base, token, approval_id, "approve")
    assert again.status_code == 409, again.text


# ==========================================================================
# (3) Approve a proposed rule -> becomes active (visible to query)
# ==========================================================================
async def _query_specialty_rules_visible(db: asyncpg.Pool, kind: str) -> bool:
    """Replicate query_specialty_rules' visibility predicate against the DB.

    query_specialty_rules returns a rule iff its profile is ACTIVE and it's
    temporally valid. We assert exactly that predicate (the HTTP/MCP query route
    can't be reached from the test host — it depends on the /mcp auth contextvar,
    a separate pre-existing limitation), so 'visible to query_specialty_rules'
    is proven via the same SQL the tool runs.
    """
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            row = await conn.fetchval(
                """
                SELECT count(*) FROM compliance_rule r
                JOIN compliance_profile p ON p.id = r.profile_id
                WHERE r.entity_id = $1
                  AND r.kind = $2
                  AND p.active = true
                  AND r.effective_from <= now()
                  AND (r.effective_to IS NULL OR r.effective_to > now())
                """,
                TEST_ENTITY_ID, kind,
            )
    return row > 0


async def _seed_proposed_rule(db: asyncpg.Pool, kind: str) -> tuple[str, str]:
    """Reproduce the 'proposed rule (inactive) + pending approval' state that
    add_specialty_rule writes: a compliance_rule under an INACTIVE holding
    profile + a 'pending' approval_queue row carrying the activate_rule payload.
    Returns (approval_id, rule_id). (Same rationale as _queue_material_recat: the
    HTTP/MCP add-rule route isn't reachable from the test host.)"""
    import json
    import uuid as _uuid

    profile_id = str(_uuid.uuid4())
    rule_id = str(_uuid.uuid4())
    approval_id = str(_uuid.uuid4())
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute(
                """
                INSERT INTO compliance_profile (id, workspace_id, entity_id, name, active)
                VALUES ($1, $2, $3, 'Agent-proposed (pending approval)', false)
                """,
                profile_id, str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID),
            )
            await conn.execute(
                """
                INSERT INTO compliance_rule (
                    id, workspace_id, entity_id, profile_id, kind, params,
                    trigger, action, effective_from, effective_to, source, reasoning
                ) VALUES (
                    $1, $2, $3, $4, $5, '{"max_usd": 750}'::jsonb,
                    'event'::compliance_trigger, 'require_approval'::compliance_action,
                    now(), NULL, 'ai_inferred'::compliance_source, 'Cap this category.'
                )
                """,
                rule_id, str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), profile_id, kind,
            )
            payload = {
                "action": "activate_rule",
                "rule_id": rule_id,
                "pending_profile_id": profile_id,
                "kind": kind,
                "trigger": "event",
                "action_enum": "require_approval",
            }
            await conn.execute(
                """
                INSERT INTO approval_queue (
                    id, workspace_id, entity_id, subject_type, subject_id,
                    status, reasoning, requested_by_identity, payload
                ) VALUES (
                    $1, $2, $3, 'compliance_rule', $4,
                    'pending', $5, 'test-agent', $6::jsonb
                )
                """,
                approval_id, str(TEST_WORKSPACE_ID), str(TEST_ENTITY_ID), rule_id,
                f"Proposed compliance rule '{kind}'.", json.dumps(payload),
            )
    return approval_id, rule_id


@pytest.mark.asyncio
async def test_approve_compliance_rule_activates_it(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    base, token = seeded_workspace.base_url, seeded_workspace.token
    kind = f"appr_rule_{_run_tag()}"
    approval_id, rule_id = await _seed_proposed_rule(db, kind)

    # Proposed under an inactive profile -> NOT visible to query_specialty_rules.
    assert not await _query_specialty_rules_visible(db, kind), "pending rule must be hidden"

    # APPROVE -> activate.
    r = await _decide(base, token, approval_id, "approve")
    assert r.status_code == 200, r.text
    decided = r.json()
    assert decided["applied"] is True
    assert decided["rule_id"] == rule_id

    # The rule's profile is now active; query_specialty_rules surfaces it.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            prof_active = await conn.fetchval(
                """
                SELECT p.active FROM compliance_rule r
                JOIN compliance_profile p ON p.id = r.profile_id
                WHERE r.id::text = $1
                """,
                rule_id,
            )
            aq_after = await conn.fetchval(
                "SELECT status::text FROM approval_queue WHERE id::text = $1", approval_id,
            )
    assert prof_active is True, "approved rule must live under an ACTIVE profile"
    assert aq_after == "approved"

    assert await _query_specialty_rules_visible(db, kind), "activated rule must be visible"


# ==========================================================================
# (4) RBAC — viewer can't list; accountant (< admin) can't decide
# ==========================================================================
@pytest.mark.asyncio
async def test_list_approvals_denied_for_viewer_is_below_accountant(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """The list floor is accountant; a viewer is denied (403)."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    headers = {"Authorization": f"Bearer {token}"}
    async with _membership_as(db, seeded_workspace.uc_uid, role="viewer", entity_scope=None):
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
            r = await c.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/approvals")
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_decide_denied_for_accountant_below_admin(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Deciding requires admin (a human attestation). An accountant is denied,
    and the held action is NOT applied."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    original_id = await _post_expense(
        base, token, f"appr-rbac-{_run_tag()}", cents=-30000, category="SaaS · AI"
    )
    approval_id = await _queue_material_recat(
        db, original_id, "Infrastructure · VPS"
    )

    async with _membership_as(db, seeded_workspace.uc_uid, role="accountant", entity_scope=None):
        r = await _decide(base, token, approval_id, "approve")
    assert r.status_code == 403, r.text

    # Still pending; ledger still untouched.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            aq = await conn.fetchval(
                "SELECT status::text FROM approval_queue WHERE id::text = $1", approval_id,
            )
            rev = await conn.fetchval(
                "SELECT count(*) FROM journal_entry WHERE reverses_entry_id::text = $1",
                original_id,
            )
    assert aq == "pending"
    assert rev == 0, "a denied decide must not apply the recat"


# ==========================================================================
# (5) Cross-workspace + out-of-scope-entity decide are denied
# ==========================================================================
@pytest.mark.asyncio
async def test_decide_denied_cross_workspace(seeded_workspace: StackConfig) -> None:
    """Deciding in workspace B (no membership) is denied even though B is valid."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
        r = await c.post(
            f"/api/workspaces/{TEST_WORKSPACE_B_ID}/approvals/"
            "00000000-0000-0000-0000-000000000000/decide",
            json={"decision": "approve"},
        )
    assert r.status_code in (401, 403), r.text


@pytest.mark.asyncio
async def test_decide_denied_out_of_scope_entity(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """A member scoped to a DIFFERENT entity cannot decide an approval that
    belongs to the test entity — entity-scope enforced through the real token."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    original_id = await _post_expense(
        base, token, f"appr-scope-{_run_tag()}", cents=-30000, category="SaaS · AI"
    )
    approval_id = await _queue_material_recat(
        db, original_id, "Infrastructure · VPS"
    )
    # Admin role but scoped to workspace B's entity (NOT the test entity).
    async with _membership_as(
        db, seeded_workspace.uc_uid, role="admin",
        entity_scope=(TEST_WORKSPACE_B_ENTITY_ID,),
    ):
        r = await _decide(base, token, approval_id, "approve")
    assert r.status_code == 403, r.text

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            aq = await conn.fetchval(
                "SELECT status::text FROM approval_queue WHERE id::text = $1", approval_id,
            )
    assert aq == "pending", "out-of-scope decide must not finalize the approval"


# ==========================================================================
# (6) Approve a bank_transaction_import queued row -> journal entry posted
# ==========================================================================


async def _seed_bank_txn_import_approval(
    db: asyncpg.Pool,
    *,
    sxid: str,
    effective_date: str,
    amount_cents: int,
    category: str = "Banking · Fees",
) -> str:
    """Reproduce the 'bank_transaction_import pending' row the ingest script writes.

    The ingest script can't be run in integration tests (no live bank statements),
    so we seed the same approval_queue row shape it would write — exact
    subject_type / status / payload structure — via the owner pool.  The thing
    under test (approve → apply) then runs entirely through the real HTTP endpoint.
    Returns the approval_queue id.
    """
    import json
    import uuid as _uuid

    approval_id = str(_uuid.uuid4())
    subject_id = str(_uuid.uuid4())  # per-row UUID (not the source_external_id)
    payload = {
        "action": "bank_transaction_import",
        "source_external_id": sxid,
        "effective_date": effective_date,
        "amount_cents": amount_cents,
        "category": category,
        "category_status": "auto",
        "categorization_source": "vendor_rules",
        "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
        "materiality_tier": "M",
        "block_reason": "queue_for_confirm",
        "llm_suggested": False,
    }
    amount_dec = abs(amount_cents) / 100
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            await conn.execute(
                """
                INSERT INTO approval_queue (
                    id, workspace_id, entity_id, subject_type, subject_id,
                    amount, currency_code, usd_equivalent_at_event_time,
                    status, reasoning, requested_by_identity, payload
                ) VALUES (
                    $1, $2, $3, 'bank_transaction_import', $4::uuid,
                    $5, 'USD', $5, 'pending', $6, 'ingest_bofa_live.py', $7::jsonb
                )
                """,
                approval_id,
                str(TEST_WORKSPACE_ID),
                str(TEST_ENTITY_ID),
                subject_id,
                amount_dec,
                f"BofA import: {effective_date} category={category!r} tier=M",
                json.dumps(payload),
            )
    return approval_id


@pytest.mark.asyncio
async def test_approve_bank_transaction_import_posts_journal_entry(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Approve a queued bank_transaction_import → journal entry posted + audit written.

    Shape mirrors test_approve_material_recat_applies_reversal_and_restatement:
      seed pending row → APPROVE → assert journal entry + status=approved + audit.
    """
    base, token = seeded_workspace.base_url, seeded_workspace.token
    tag = _run_tag()
    sxid = f"bofa-1792:2022-01-31:001:{tag}"

    approval_id = await _seed_bank_txn_import_approval(
        db,
        sxid=sxid,
        effective_date="2022-01-15",
        amount_cents=-5000,  # -$50, Banking · Fees outflow (tier M — needs confirm)
        category="Banking · Fees",
    )

    # Approval is pending; no journal entry for this sxid yet.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            aq_before = await conn.fetchrow(
                "SELECT status::text AS status, subject_type "
                "FROM approval_queue WHERE id::text = $1", approval_id,
            )
            je_before = await conn.fetchval(
                "SELECT count(*) FROM journal_entry "
                "WHERE source = 'bofa-statement' AND source_external_id = $1", sxid,
            )
    assert aq_before["status"] == "pending"
    assert aq_before["subject_type"] == "bank_transaction_import"
    assert je_before == 0, "no journal entry should exist before approve"

    # APPROVE via real HTTP endpoint.
    r = await _decide(base, token, approval_id, "approve")
    assert r.status_code == 200, r.text
    decided = r.json()
    assert decided["ok"] is True
    assert decided["decision"] == "approved"
    assert decided["applied"] is True
    journal_entry_id = decided["journal_entry_id"]
    assert journal_entry_id  # must be a non-empty UUID string

    # Verify journal entry was posted + approval row marked decided + audit written.
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            je = await conn.fetchrow(
                "SELECT status, source, source_external_id, category, "
                "debit_account, credit_account, approving_human_uc_uid "
                "FROM journal_entry WHERE id::text = $1", journal_entry_id,
            )
            aq_after = await conn.fetchrow(
                "SELECT status::text AS status, decided_by_uc_uid, decided_at "
                "FROM approval_queue WHERE id::text = $1", approval_id,
            )
            audit_count = await conn.fetchval(
                "SELECT count(*) FROM decision_audit "
                "WHERE tool_name = 'approval_decision' AND outcome = 'approved' "
                "AND entity_id = $1", TEST_ENTITY_ID,
            )

    # Journal entry: source=bofa-statement, correct sxid, status=posted.
    assert je is not None, "journal entry must exist after approve"
    assert je["status"] == "posted"
    assert je["source"] == "bofa-statement"
    assert je["source_external_id"] == sxid
    assert je["category"] == "Banking · Fees"
    # Banking · Fees is account 6700; outflow: DR 6700 (expense), CR 1010 (cash).
    assert je["debit_account"].startswith("6700"), je["debit_account"]
    assert je["credit_account"].startswith("1010"), je["credit_account"]
    # The approving human is stamped on the journal entry.
    assert je["approving_human_uc_uid"] == seeded_workspace.uc_uid

    # Approval row marked approved + decided_by + audit.
    assert aq_after["status"] == "approved"
    assert aq_after["decided_by_uc_uid"] == seeded_workspace.uc_uid
    assert aq_after["decided_at"] is not None
    assert audit_count >= 1


@pytest.mark.asyncio
async def test_reject_bank_transaction_import_leaves_ledger_untouched(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> None:
    """Reject a queued bank_transaction_import → no journal entry; row rejected."""
    base, token = seeded_workspace.base_url, seeded_workspace.token
    tag = _run_tag()
    sxid = f"bofa-1792:2022-01-31:002:{tag}"

    approval_id = await _seed_bank_txn_import_approval(
        db,
        sxid=sxid,
        effective_date="2022-01-20",
        amount_cents=-9999,
        category="Banking · Fees",
    )

    r = await _decide(base, token, approval_id, "reject")
    assert r.status_code == 200, r.text
    decided = r.json()
    assert decided["decision"] == "rejected" and decided["applied"] is False

    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{_SCHEMA}", public')
            aq = await conn.fetchrow(
                "SELECT status::text AS status, decided_by_uc_uid "
                "FROM approval_queue WHERE id::text = $1", approval_id,
            )
            je_count = await conn.fetchval(
                "SELECT count(*) FROM journal_entry "
                "WHERE source = 'bofa-statement' AND source_external_id = $1", sxid,
            )

    assert aq["status"] == "rejected"
    assert aq["decided_by_uc_uid"] == seeded_workspace.uc_uid
    assert je_count == 0, "reject must not post a journal entry"
