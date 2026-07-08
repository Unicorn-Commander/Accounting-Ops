"""End-to-end integration test of the real posting pipeline.

Path under test (against a live docker-compose stack):

  seed workspace + entity + default COA   (fixtures + POST /coa/seed)
  link COA 1010 -> SimpleFIN account       (POST /coa/link)
  register account_entity_mapping          (fixture)
        |
        v  inject a SimpleFIN-style transaction and CONFIRM it
  POST /api/workspaces/{wid}/journal-entries
        |
        +--> assert a journal_entry row was actually POSTED (DB + API)
        +--> GET .../journal-entries/verify-chain  => ok, 0 tampered
        +--> GET .../reports/pl                     => revenue/expense/net correct
        +--> exercise a CPA export (cpa-export /exports/csv if present,
             else assert the export-source rows the CPA bundle is built from)

The two transactions:
  * expense: "Anthropic" -$12.00 -> SaaS · AI (6210). Tier L, vendor rule
    matches cleanly + entity mapped + sign matches => AUTO-POST (the headline
    SimpleFIN happy path).
  * revenue: "Stripe payout" +$500.00 -> Revenue · Marketplace (4010). Tier M,
    so it requires user_confirmed=True (the human-confirm path).
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_EXTERNAL_ACCOUNT_ID,
    TEST_SOURCE,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.conftest import fetch_chain_rows

pytestmark = pytest.mark.integration

# A fixed year keeps the P&L period deterministic regardless of run date.
PERIOD_YEAR = 2026
EFFECTIVE_DATE = dt.date(PERIOD_YEAR, 3, 15).isoformat()


def _expense_payload(ext_id: str) -> dict:
    return {
        "entity_id": str(TEST_ENTITY_ID),
        "source": TEST_SOURCE,
        "source_external_id": ext_id,
        "effective_date": EFFECTIVE_DATE,
        "amount_cents": -1200,  # -$12.00 outflow, tier L
        "description": "Anthropic May subscription",
        "payee": "Anthropic",
        "memo": "Claude API",
        "category": "SaaS · AI",
        "category_status": "auto",
        "sign_matched": True,
        "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
        "user_confirmed": False,  # tier L auto-posts without explicit confirm
    }


def _revenue_payload(ext_id: str) -> dict:
    return {
        "entity_id": str(TEST_ENTITY_ID),
        "source": TEST_SOURCE,
        "source_external_id": ext_id,
        "effective_date": EFFECTIVE_DATE,
        "amount_cents": 50000,  # +$500.00 inflow, tier M -> needs confirm
        "description": "Stripe payout",
        "payee": "Stripe",
        "memo": "April marketplace payout",
        "category": "Revenue · Marketplace",
        "category_status": "auto",
        "sign_matched": True,
        "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
        "user_confirmed": True,  # human-confirm path for a material entry
    }


@pytest.mark.asyncio
async def test_full_pipeline(
    client: httpx.AsyncClient, db, seeded_workspace: StackConfig
) -> None:
    wid = TEST_WORKSPACE_ID
    # Unique external ids per run so re-runs aren't blocked by idempotency.
    run_tag = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
    exp_ext = f"txn-exp-{run_tag}"
    rev_ext = f"txn-rev-{run_tag}"

    # ---- 1. Inject + confirm the SimpleFIN expense (auto-post) ----
    r = await client.post(
        f"/api/workspaces/{wid}/journal-entries", json=_expense_payload(exp_ext)
    )
    assert r.status_code == 200, f"expense post failed: {r.status_code} {r.text}"
    exp = r.json()
    assert exp["ok"] is True, exp
    assert exp["outcome"] == "posted", exp
    assert exp["materiality_tier"] == "L", exp
    expense_id = exp["journal_entry_id"]
    assert expense_id

    # ---- 2. Inject + confirm the revenue (human-confirmed material entry) ----
    r = await client.post(
        f"/api/workspaces/{wid}/journal-entries", json=_revenue_payload(rev_ext)
    )
    assert r.status_code == 200, f"revenue post failed: {r.status_code} {r.text}"
    rev = r.json()
    assert rev["ok"] is True, rev
    assert rev["outcome"] == "posted", rev
    assert rev["materiality_tier"] == "M", rev
    revenue_id = rev["journal_entry_id"]

    # ---- 3. A journal_entry row was actually persisted (verify via DB) ----
    async with db.acquire() as conn:
        schema = f"accounting_ops_{str(wid).replace('-', '_')}"
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path = "{schema}", public')
            row = await conn.fetchrow(
                """
                SELECT amount, debit_account, credit_account, category,
                       materiality_tier, status, source, source_external_id,
                       this_hash, approving_human_uc_uid
                FROM journal_entry WHERE id::text = $1
                """,
                expense_id,
            )
    assert row is not None, "expense journal_entry not found in DB"
    assert row["status"] == "posted"
    assert row["source"] == TEST_SOURCE
    assert row["source_external_id"] == exp_ext
    assert row["category"] == "SaaS · AI"
    # Outflow: DR the expense (6210), CR cash (1010).
    assert row["debit_account"].startswith("6210"), row["debit_account"]
    assert row["credit_account"].startswith("1010"), row["credit_account"]
    # amount is stored as the absolute value, NUMERIC(20,4).
    assert str(row["amount"]) in ("12.0000", "12.00", "12"), row["amount"]
    assert row["this_hash"] is not None and len(bytes(row["this_hash"])) == 32

    # And via the read API.
    r = await client.get(
        f"/api/workspaces/{wid}/journal-entries/{expense_id}"
    )
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["status"] == "posted"
    assert detail["category"] == "SaaS · AI"

    # ---- 4. Hash chain verifies clean ----
    r = await client.get(
        f"/api/workspaces/{wid}/journal-entries/verify-chain",
        params={"entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    chain = r.json()
    assert chain["ok"] is True, chain
    assert chain["tampered_count"] == 0, chain

    # The two entries we posted are part of the chain.
    rows = await fetch_chain_rows(db, TEST_ENTITY_ID)
    ids = {row["id"] for row in rows}
    assert expense_id in ids
    assert revenue_id in ids

    # ---- 5. P&L report reflects the postings ----
    r = await client.get(
        f"/api/workspaces/{wid}/reports/pl",
        params={"period": str(PERIOD_YEAR), "entity_id": str(TEST_ENTITY_ID)},
    )
    assert r.status_code == 200, r.text
    pl = r.json()
    # The expense ($12) lands under 6210; the revenue ($500) under 4010.
    expense_numbers = {line["account_number"] for line in pl["expenses"]["lines"]}
    revenue_numbers = {line["account_number"] for line in pl["revenue"]["lines"]}
    assert "6210" in expense_numbers, pl["expenses"]
    assert "4010" in revenue_numbers, pl["revenue"]
    # Totals are cumulative if the suite is re-run, so assert the floor + that
    # this run's contribution is present (revenue >= 500, expense >= 12).
    assert float(pl["revenue"]["total"]) >= 500.0, pl["revenue"]
    assert float(pl["expenses"]["total"]) >= 12.0, pl["expenses"]
    # net_income == revenue_total - expense_total, exactly.
    assert (
        float(pl["net_income"])
        == pytest.approx(
            float(pl["revenue"]["total"]) - float(pl["expenses"]["total"])
        )
    ), pl

    # ---- 6. CPA export ----
    await _exercise_cpa_export(seeded_workspace, expense_id, revenue_id)


async def _exercise_cpa_export(
    stack: StackConfig, expense_id: str, revenue_id: str
) -> None:
    """Pull a CPA export and assert our posted entries are in it.

    If the cpa-export service is wired (CPA_EXPORT_URL set) we hit its real
    /exports/csv endpoint over HTTP with the service token. Otherwise we
    assert the export-SOURCE data (the journal-entries listing the bundle is
    serialized from) so the invariant — "the export contains exactly what was
    posted" — is still proven without the optional service.
    """
    period = str(PERIOD_YEAR)
    if stack.cpa_export_url:
        headers = {"Authorization": f"Bearer {stack.cpa_export_token}"}
        async with httpx.AsyncClient(timeout=30, headers=headers) as c:
            r = await c.get(
                f"{stack.cpa_export_url}/exports/csv",
                params={
                    "workspace_id": str(TEST_WORKSPACE_ID),
                    "entity_id": str(TEST_ENTITY_ID),
                    "period": period,
                },
            )
        assert r.status_code == 200, f"cpa-export csv failed: {r.status_code} {r.text}"
        assert "text/csv" in r.headers.get("content-type", "")
        body = r.text
        # CSV header from CsvExporter + our two entries (by id).
        assert "debit_account" in body and "credit_account" in body
        assert expense_id in body, "expense entry missing from CPA CSV export"
        assert revenue_id in body, "revenue entry missing from CPA CSV export"
    else:
        # Fallback: the listing endpoint is the export source of record.
        headers = {"Authorization": f"Bearer {stack.token}"}
        async with httpx.AsyncClient(
            base_url=stack.base_url, headers=headers, timeout=30
        ) as c:
            r = await c.get(
                f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
                params={
                    "entity_id": str(TEST_ENTITY_ID),
                    "start_date": dt.date(PERIOD_YEAR, 1, 1).isoformat(),
                    "end_date": dt.date(PERIOD_YEAR, 12, 31).isoformat(),
                    "status_filter": "posted",
                    "limit": 500,
                },
            )
        assert r.status_code == 200, r.text
        listing = r.json()
        ids = {e["id"] for e in listing["entries"]}
        assert expense_id in ids, "expense entry missing from export-source listing"
        assert revenue_id in ids, "revenue entry missing from export-source listing"
        # Each row carries the data a CPA CSV/IIF needs (debit/credit/hash).
        sample = next(e for e in listing["entries"] if e["id"] == expense_id)
        assert sample["debit_account"] and sample["credit_account"]
        assert sample["this_hash_hex"]
