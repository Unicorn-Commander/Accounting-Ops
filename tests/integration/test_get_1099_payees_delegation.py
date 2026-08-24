"""Integration proof: get_1099_payees delegates to form_1099_nec and NETS refunds.

Against a live docker-compose stack (postgres + app), this posts a contractor
PAYMENT plus a REFUND for the SAME payee through the real posting pipeline
(POST /journal-entries -> DR/CR the contract-labor account 6300, counterparty_id
NULL so grouping falls back to the ledger payee text), then proves:

  * get_1099_payees returns that payee as EXACTLY ONE row (grouped, not split);
  * its total_amount_cents equals the canonical 1099-NEC report's NETTED total
    for that payee (the refund nets the payment DOWN), and NOT the SUM(ABS)
    inflated figure the old second implementation produced.

Revert-sensitive: run against the pre-fix tool (which passed workspace=None and
read the wrong output keys) the MCP call errors (AttributeError -> isError) or
returns an empty payee list, and every assertion below fails.

Skips cleanly when the stack isn't reachable (seeded_workspace / client skip).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import httpx
import pytest

from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_EXTERNAL_ACCOUNT_ID,
    TEST_SOURCE,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.test_mcp_tools import _call_tool, _result_payload

pytestmark = pytest.mark.integration

_YEAR = 2026
_EFFECTIVE = dt.date(_YEAR, 4, 10).isoformat()

# One contractor paid $1,000 then refunded $250. The 1099-NEC basis is the
# NETTED amount ($750), never the gross-of-refund SUM(ABS) ($1,250).
PAYMENT_CENTS = -100_000  # $1,000.00 outflow  -> DR 6300 (Contractor · 1099-NEC)
REFUND_CENTS = 25_000     # $250.00  inflow   -> CR 6300 (refund nets down)
NET_CENTS = 75_000        # 100,000 - 25,000  (correct netted basis)
ABS_CENTS = 125_000       # 100,000 + 25,000  (the OLD SUM(ABS) bug)


def _contractor_payload(ext_id: str, payee: str, amount_cents: int, memo: str) -> dict:
    return {
        "entity_id": str(TEST_ENTITY_ID),
        "source": TEST_SOURCE,
        "source_external_id": ext_id,
        "effective_date": _EFFECTIVE,
        "amount_cents": amount_cents,
        "description": memo,
        "payee": payee,
        "memo": memo,
        # Maps to GL 6300, a CONTRACT_LABOR_ACCOUNTS member -> 1099-NEC basis.
        "category": "Contractor · 1099-NEC",
        "category_status": "auto",
        "sign_matched": True,
        "external_account_id": TEST_EXTERNAL_ACCOUNT_ID,
        # Material entry: human-confirm path so it posts at any materiality tier.
        "user_confirmed": True,
    }


async def _post(client: httpx.AsyncClient, wid, payload: dict) -> None:
    r = await client.post(f"/api/workspaces/{wid}/journal-entries", json=payload)
    assert r.status_code == 200, f"post failed: {r.status_code} {r.text}"
    assert r.json().get("outcome") == "posted", r.text


@pytest.mark.asyncio
async def test_get_1099_payees_nets_refunds_like_form_1099_nec(
    client: httpx.AsyncClient,
    seeded_workspace: StackConfig,
) -> None:
    wid = TEST_WORKSPACE_ID
    # Unique per run: unique external ids (posting is idempotent on them) AND a
    # unique payee so re-runs never accumulate onto the same payee's total.
    run_tag = dt.datetime.now(tz=dt.timezone.utc).strftime("%Y%m%d%H%M%S%f")
    payee = f"ITest Contractor {run_tag}"

    # Post the payment ($1,000 -> DR 6300) and the refund ($250 -> CR 6300).
    await _post(client, wid, _contractor_payload(f"nec-pay-{run_tag}", payee, PAYMENT_CENTS, "Consulting"))
    await _post(client, wid, _contractor_payload(f"nec-ref-{run_tag}", payee, REFUND_CENTS, "Refund"))

    # Canonical source of truth: the HTTP 1099-NEC report (form_1099_nec).
    rr = await client.get(
        f"/api/workspaces/{wid}/reports/1099-nec",
        params={"year": str(_YEAR), "entity_id": str(TEST_ENTITY_ID)},
    )
    assert rr.status_code == 200, f"1099-nec report failed: {rr.status_code} {rr.text}"
    report_rows = [p for p in rr.json().get("payees", []) if p.get("payee") == payee]
    assert len(report_rows) == 1, f"report should net to ONE row for {payee}: {report_rows}"
    report_cents = int(
        (Decimal(str(report_rows[0]["total_amount"])) * 100).to_integral_value()
    )
    assert report_cents == NET_CENTS, f"report netted total {report_cents} != {NET_CENTS}"

    # The tool under test, over the real MCP HTTP surface.
    result = await _call_tool(
        seeded_workspace.base_url,
        seeded_workspace.token,
        "get_1099_payees",
        {
            "workspace_id": str(wid),
            "entity_id": str(TEST_ENTITY_ID),
            "tax_year": _YEAR,
            "threshold_cents": 60_000,
        },
    )
    # Pre-fix (workspace=None) crashes with AttributeError -> isError True.
    assert result.isError is False, result.content
    data = _result_payload(result)
    tool_rows = [p for p in data.get("payees", []) if p.get("payee_name") == payee]

    # NON-EMPTY and grouped into exactly ONE row (not split per descriptor).
    assert len(tool_rows) == 1, f"tool should return ONE row for {payee}: {data.get('payees')}"
    tool_cents = tool_rows[0]["total_amount_cents"]

    # Money is RIGHT: matches the canonical report's NETTED total ...
    assert tool_cents == report_cents, f"tool {tool_cents} != report {report_cents}"
    # ... which is the refund-netted figure, NOT the SUM(ABS)-inflated one.
    assert tool_cents == NET_CENTS, f"tool total {tool_cents} != netted {NET_CENTS}"
    assert tool_cents != ABS_CENTS, "tool total equals SUM(ABS)-inflated figure (refund not netted)"

    # entry_count reflects both postings under the single grouped payee.
    assert tool_rows[0]["entry_count"] >= 2, tool_rows[0]
