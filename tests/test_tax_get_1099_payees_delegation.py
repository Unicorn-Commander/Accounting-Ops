"""Smoke test: verify get_1099_payees delegates to form_1099_nec correctly.

Ensures that the tool's aggregation matches the canonical form_1099_nec
implementation (debit-side, netting refunds down) rather than the old
ABS-inflated sum.
"""
from __future__ import annotations

import pytest
from uuid import uuid4

from app.tools.tax_get_1099_payees import get_1099_payees


@pytest.mark.asyncio
async def test_get_1099_payees_delegates_to_form_1099_nec(
    workspace_id: str,
    entity_id: str,
    session,
) -> None:
    """Post contractor entries including a refund, then assert the tool's
    total equals form_1099_nec's netted total and NOT the ABS-inflated sum."""
    from app.api.reports import form_1099_nec as _http_1099_nec
    from app.db import SessionLocal
    from app.workspace_session import entity_session

    # Create a test payee
    payee_name = "Test Contractor"
    tax_year = 2024

    # Post a contractor payment of $700
    # and a refund of $100
    # Netted total should be $600 (60,000 cents)
    # ABS-inflated total would be $800 (80,000 cents)

    async with entity_session(SessionLocal, workspace_id, entity_id) as (sess, _eid):
        # Insert a journal entry for the payment
        # We'll use raw SQL to insert directly into journal_entry
        # Assuming the contract-labor account is 6300
        await sess.execute(
            """
            INSERT INTO journal_entry (
                workspace_id, entity_id, status, effective_date,
                payee, description, category, amount,
                debit_account, credit_account
            ) VALUES (
                :wid, :eid, 'posted', :eff_date,
                :payee, :desc, :cat, :amt,
                '6300', '1010'
            )
            """,
            {
                "wid": workspace_id,
                "eid": entity_id,
                "eff_date": f"{tax_year}-06-15",
                "payee": payee_name,
                "desc": "Consulting services",
                "cat": "Contractor · 1099-NEC",
                "amt": 700.00,
            }
        )

        # Insert a refund entry (negative amount)
        await sess.execute(
            """
            INSERT INTO journal_entry (
                workspace_id, entity_id, status, effective_date,
                payee, description, category, amount,
                debit_account, credit_account
            ) VALUES (
                :wid, :eid, 'posted', :eff_date,
                :payee, :desc, :cat, :amt,
                '6300', '1010'
            )
            """,
            {
                "wid": workspace_id,
                "eid": entity_id,
                "eff_date": f"{tax_year}-07-20",
                "payee": payee_name,
                "desc": "Refund for services",
                "cat": "Contractor · 1099-NEC",
                "amt": -100.00,
            }
        )

        await sess.commit()

    # Call the tool
    tool_result = await get_1099_payees(
        workspace_id=workspace_id,
        entity_id=entity_id,
        tax_year=tax_year,
        threshold_cents=60_000,
    )

    # Call the form directly
    form_result = await _http_1099_nec(
        workspace_id=workspace_id,
        year=str(tax_year),
        entity_id=entity_id,
        workspace=None,
    )

    # Extract totals
    tool_payees = tool_result.get("payees", [])
    form_payees = form_result.get("payees", [])

    # There should be exactly one payee in both results
    assert len(tool_payees) == 1, f"Expected 1 payee, got {len(tool_payees)}"
    assert len(form_payees) == 1, f"Expected 1 payee from form, got {len(form_payees)}"

    tool_total = tool_payees[0]["total_amount_cents"]
    form_total = form_payees[0]["total_amount_cents"]

    # The tool's total should match the form's netted total
    assert tool_total == form_total, (
        f"Tool total ({tool_total}) does not match form total ({form_total})"
    )

    # The netted total should be $600 (60,000 cents), not $800 (80,000 cents)
    assert tool_total == 60_000, (
        f"Expected netted total of 60,000 cents, got {tool_total} cents"
    )

    # Clean up: delete the inserted journal entries
    async with entity_session(SessionLocal, workspace_id, entity_id) as (sess, _eid):
        await sess.execute(
            """
            DELETE FROM journal_entry
            WHERE workspace_id = :wid
              AND entity_id = :eid
              AND payee = :payee
              AND category = :cat
            """,
            {
                "wid": workspace_id,
                "eid": entity_id,
                "payee": payee_name,
                "cat": "Contractor · 1099-NEC",
            }
        )
        await sess.commit()
