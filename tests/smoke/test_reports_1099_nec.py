"""1099-NEC report logic tests.

The repo has no Postgres test harness (smoke tests are pure-Python, no DB), so
these tests exercise the report two ways:

  1. ``_build_1099_nec_payees`` — the pure threshold + processor-flag + sort
     logic, with no DB. Covers the $600 threshold and that
     refunded-below-threshold contractors fall off.
  2. ``form_1099_nec`` end-to-end with the DB session faked, asserting the
     generated SQL restricts to the contract-labor account on BOTH sides
     (so revenue/non-expense postings are structurally excluded) and nets
     debit-minus-credit — i.e. it is NOT the old buggy
     ``SUM(je.amount) GROUP BY je.payee`` with no account filter.
"""
from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from uuid import uuid4

from app.api import reports


# ---------------------------------------------------------------------------
# Pure logic: $600 threshold, processor flagging, sort, refund netting
# ---------------------------------------------------------------------------


def test_threshold_keeps_600_and_above_drops_below():
    rows = [
        {"payee": "Alice Contractor", "net_amount": Decimal("600.00"), "posting_count": 2},
        {"payee": "Bob Freelance", "net_amount": Decimal("599.99"), "posting_count": 1},
        {"payee": "Carol Designs", "net_amount": Decimal("12000.00"), "posting_count": 5},
    ]
    payees = reports._build_1099_nec_payees(rows)
    names = [p["payee"] for p in payees]

    # Exactly $600 is reportable; $599.99 is not.
    assert "Alice Contractor" in names
    assert "Carol Designs" in names
    assert "Bob Freelance" not in names
    assert len(payees) == 2


def test_refunds_netted_below_threshold_are_excluded():
    # Paid $700 then refunded $200 -> net $500, below the $600 threshold.
    rows = [
        {"payee": "Returned Work LLC", "net_amount": Decimal("500.00"), "posting_count": 2},
    ]
    assert reports._build_1099_nec_payees(rows) == []


def test_sorted_descending_by_amount():
    rows = [
        {"payee": "Small", "net_amount": Decimal("600"), "posting_count": 1},
        {"payee": "Big", "net_amount": Decimal("9000"), "posting_count": 1},
        {"payee": "Mid", "net_amount": Decimal("2500"), "posting_count": 1},
    ]
    payees = reports._build_1099_nec_payees(rows)
    assert [p["payee"] for p in payees] == ["Big", "Mid", "Small"]


def test_processor_payees_flagged_for_human_review():
    rows = [
        {"payee": "PayPal *JoeDev", "net_amount": Decimal("4000"), "posting_count": 3},
        {"payee": "Jane Smith", "net_amount": Decimal("4000"), "posting_count": 3},
    ]
    flagged = {p["payee"]: p["requires_human_review"] for p in reports._build_1099_nec_payees(rows)}
    assert flagged["PayPal *JoeDev"] is True   # 1099-K territory -> review
    assert flagged["Jane Smith"] is False


def test_amounts_stringified_two_decimal_threshold():
    out = reports.form_1099_nec  # noqa: F841 — referenced so import is exercised
    assert reports.NEC_THRESHOLD == Decimal("600")
    assert reports.CONTRACT_LABOR_ACCOUNTS == ("6300",)


# ---------------------------------------------------------------------------
# End-to-end handler with a faked DB session: asserts the SQL shape
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Captures every executed statement; returns canned rows by call order."""

    def __init__(self, entity_label_row, payee_rows):
        self.statements: list[str] = []
        self._entity_label_row = entity_label_row
        self._payee_rows = payee_rows

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        # The first query in the handler is _entity_label's SELECT display_name
        # FROM entity; everything else is the aggregation. Route by content.
        if "FROM entity" in sql:
            return _FakeResult([self._entity_label_row] if self._entity_label_row else [])
        return _FakeResult(self._payee_rows)


@contextlib.asynccontextmanager
async def _fake_workspace_session(session):
    yield session


def _run_handler(payee_rows, entity_label_row=None, monkeypatch_target=None):
    """Invoke form_1099_nec with workspace_session patched to a fake."""
    fake = _FakeSession(entity_label_row, payee_rows)

    original = reports.workspace_session
    reports.workspace_session = lambda *_a, **_k: _fake_workspace_session(fake)
    try:
        ws = reports.WorkspaceContext(
            workspace_id=uuid4(),
            workspace_slug="test",
            role="owner",
            user_uc_uid="u",
            agent_id=None,
            entity_scope=None,
            hipaa_protected=False,
        )
        result = asyncio.run(
            reports.form_1099_nec(
                workspace_id=ws.workspace_id,
                year="2025",
                entity_id=None,
                workspace=ws,
            )
        )
    finally:
        reports.workspace_session = original
    return result, fake


def test_sql_restricts_to_contract_labor_account_both_sides():
    # Canned rows already netted by the (faked) DB; here we only care about SQL.
    result, fake = _run_handler(
        payee_rows=[
            {"payee": "Carol Designs", "net_amount": Decimal("12000.00"), "posting_count": 5},
            {"payee": "Sub600 LLC", "net_amount": Decimal("100.00"), "posting_count": 1},
        ],
    )

    agg_sql = [s for s in fake.statements if "nec_sides" in s]
    assert agg_sql, "aggregation query not executed"
    sql = agg_sql[0]

    # Regression guard: the old WIP did `SUM(je.amount) ... GROUP BY je.payee`
    # with NO account filter and referenced the wrong grouping. Ensure we don't.
    assert "GROUP BY je.payee" not in sql

    # Must restrict on the contract-labor account number on BOTH the debit
    # and credit sides — this is what excludes revenue (4xxx) and other
    # expense postings from the NEC total.
    #
    # Bug 13 fix (2026-05-31): the predicate now matches the indexed
    # je.debit_account_number / je.credit_account_number generated stored
    # columns from migration 0015 instead of split_part on the canonical
    # string. The SEMANTIC check (filtering by the contract-labor account
    # set, on both sides) is unchanged.
    assert "je.debit_account_number IN" in sql
    assert "je.credit_account_number IN" in sql
    # Netting: debit minus credit (so refunds/reversals reduce the total).
    assert "SUM(s.debit) - SUM(s.credit)" in sql
    # Aggregates by counterparty, joined for the display name.
    assert "JOIN counterparty" in sql
    assert "c.display_name" in sql


def test_handler_shape_and_threshold_passthrough():
    result, fake = _run_handler(
        payee_rows=[
            {"payee": "Carol Designs", "net_amount": Decimal("12000.00"), "posting_count": 5},
            {"payee": "PayPal *Dev", "net_amount": Decimal("900.00"), "posting_count": 2},
        ],
        entity_label_row=None,
    )

    assert result["year"] == "2025"
    assert result["threshold"] == "600.00"
    assert result["basis_accounts"] == ["6300"]
    assert result["total_payees"] == 2
    assert result["total_paid"] == "12900.00"
    # Highest first; processor name flagged.
    assert result["payees"][0]["payee"] == "Carol Designs"
    paypal = next(p for p in result["payees"] if "PayPal" in p["payee"])
    assert paypal["requires_human_review"] is True


def test_invalid_year_rejected():
    import pytest
    from fastapi import HTTPException

    ws = reports.WorkspaceContext(
        workspace_id=uuid4(),
        workspace_slug="test",
        role="owner",
        user_uc_uid="u",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            reports.form_1099_nec(
                workspace_id=ws.workspace_id, year="20xx", entity_id=None, workspace=ws
            )
        )
    assert exc.value.status_code == 400
