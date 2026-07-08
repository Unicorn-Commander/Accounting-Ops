"""Trial Balance report tests (fix #2).

Pure-Python smoke tests — no DB.  The DB session is faked exactly like
test_reports_1099_nec.py.

Three test scenarios as specified:
  1. Synthetic journal with deliberate imbalance → imbalance reported.
  2. Synthetic balanced journal → balanced=True, totals match.
  3. Per-entity vs consolidated (entity_id propagates to SQL).
"""
from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from uuid import uuid4

from app.api import reports


# ---------------------------------------------------------------------------
# Fake session infrastructure
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
    """Routes execute() calls: entity-label query vs main aggregation."""

    def __init__(self, coa_rows, entity_label_row=None):
        self._coa_rows = coa_rows
        self._entity_label_row = entity_label_row
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM entity" in sql:
            return _FakeResult(
                [self._entity_label_row] if self._entity_label_row else []
            )
        return _FakeResult(self._coa_rows)


@contextlib.asynccontextmanager
async def _fake_workspace_session(session):
    yield session


def _build_ws(entity_scope=None):
    return reports.WorkspaceContext(
        workspace_id=uuid4(),
        workspace_slug="test",
        role="owner",
        user_uc_uid="u",
        agent_id=None,
        entity_scope=entity_scope,
        hipaa_protected=False,
    )


def _run_trial_balance(coa_rows, *, entity_id=None, entity_label_row=None):
    """Invoke trial_balance handler with faked session."""
    fake = _FakeSession(coa_rows, entity_label_row)
    original = reports.workspace_session
    reports.workspace_session = lambda *_a, **_k: _fake_workspace_session(fake)
    try:
        ws = _build_ws()
        result = asyncio.run(
            reports.trial_balance(
                workspace_id=ws.workspace_id,
                as_of="2025-12-31",
                entity_id=entity_id,
                workspace=ws,
            )
        )
    finally:
        reports.workspace_session = original
    return result, fake


# ---------------------------------------------------------------------------
# 1. Imbalanced journal → imbalance reported correctly
# ---------------------------------------------------------------------------


def test_imbalanced_journal_reports_correctly():
    """Deliberately imbalanced aggregation: grand debits != grand credits.

    Synthetic COA rows: two accounts with activity.
    - Account 6210 (expense): debit=500, credit=0
    - Account 1010 (asset): debit=300, credit=0  (missing $200 credit side)
    Expected: balanced=False, grand_total_debit=800, grand_total_credit=0.
    """
    rows = [
        {
            "account_number": "6210",
            "canonical_form": "6210·SaaS · AI",
            "account_type": "expense",
            "total_debit": Decimal("500.0000"),
            "total_credit": Decimal("0.0000"),
        },
        {
            "account_number": "1010",
            "canonical_form": "1010·Checking",
            "account_type": "asset",
            "total_debit": Decimal("300.0000"),
            "total_credit": Decimal("0.0000"),
        },
    ]
    result, _ = _run_trial_balance(rows)

    assert result["balanced"] is False
    assert Decimal(result["grand_total_debit"]) == Decimal("800")
    assert Decimal(result["grand_total_credit"]) == Decimal("0")

    # Each row carries account metadata + net_balance
    accts = {a["account_number"]: a for a in result["accounts"]}
    assert Decimal(accts["6210"]["net_balance"]) == Decimal("500")
    assert accts["6210"]["account_type"] == "expense"
    assert Decimal(accts["1010"]["net_balance"]) == Decimal("300")


# ---------------------------------------------------------------------------
# 2. Balanced journal → balanced=True, totals match
# ---------------------------------------------------------------------------


def test_balanced_journal_reports_correctly():
    """Every debit has a matching credit → balanced=True.

    Synthetic entry: $1000 expense debit (6210) / $1000 cash credit (1010).
    The aggregation rows represent the COA-level totals after the JOIN.
    """
    rows = [
        {
            "account_number": "1010",
            "canonical_form": "1010·Checking",
            "account_type": "asset",
            "total_debit": Decimal("0.0000"),
            "total_credit": Decimal("1000.0000"),
        },
        {
            "account_number": "6210",
            "canonical_form": "6210·SaaS · AI",
            "account_type": "expense",
            "total_debit": Decimal("1000.0000"),
            "total_credit": Decimal("0.0000"),
        },
    ]
    result, _ = _run_trial_balance(rows)

    assert result["balanced"] is True
    assert Decimal(result["grand_total_debit"]) == Decimal("1000")
    assert Decimal(result["grand_total_credit"]) == Decimal("1000")
    assert result["entity_id"] == "consolidated"
    assert result["as_of"] == "2025-12-31"
    assert result["currency"] == "USD"

    # net_balance = debit - credit per row
    accts = {a["account_number"]: a for a in result["accounts"]}
    assert Decimal(accts["1010"]["net_balance"]) == Decimal("-1000")  # credit-normal
    assert Decimal(accts["6210"]["net_balance"]) == Decimal("1000")   # debit-normal

    # Totals match grand totals
    assert sum(Decimal(a["total_debit"]) for a in result["accounts"]) == Decimal(
        result["grand_total_debit"]
    )
    assert sum(Decimal(a["total_credit"]) for a in result["accounts"]) == Decimal(
        result["grand_total_credit"]
    )


# ---------------------------------------------------------------------------
# 3. Per-entity vs consolidated — entity_id propagates to SQL
# ---------------------------------------------------------------------------


def test_entity_id_propagates_to_sql():
    """Concrete entity_id must appear as AND je.entity_id = :entity_id in SQL."""
    entity_uuid = str(uuid4())
    rows = [
        {
            "account_number": "1010",
            "canonical_form": "1010·Checking",
            "account_type": "asset",
            "total_debit": Decimal("200.0000"),
            "total_credit": Decimal("200.0000"),
        },
    ]
    result, fake = _run_trial_balance(rows, entity_id=entity_uuid)

    # The aggregation SQL must filter to the specific entity
    agg_sqls = [s for s in fake.statements if "activity" in s or "aggregated" in s]
    assert agg_sqls, "aggregation query not executed"
    sql = agg_sqls[0]
    assert "je.entity_id = :entity_id" in sql

    # Result must carry the concrete entity_id (not 'consolidated')
    assert result["entity_id"] == entity_uuid


def test_consolidated_has_no_entity_filter_in_sql():
    """Consolidated view (entity_id=None) must NOT inject an entity filter."""
    rows = []
    result, fake = _run_trial_balance(rows, entity_id=None)

    agg_sqls = [s for s in fake.statements if "activity" in s or "aggregated" in s]
    assert agg_sqls, "aggregation query not executed"
    sql = agg_sqls[0]
    assert "je.entity_id = :entity_id" not in sql

    assert result["entity_id"] == "consolidated"
    assert result["balanced"] is True  # zero grand totals => equal


def test_response_shape_complete():
    """Assert every required key is present in the response envelope."""
    result, _ = _run_trial_balance([])
    required_keys = {
        "as_of", "entity_label", "entity_id", "currency",
        "accounts", "grand_total_debit", "grand_total_credit", "balanced",
    }
    assert required_keys.issubset(result.keys())


def test_account_row_shape():
    """Each account row carries account_number, canonical_form, account_type,
    total_debit, total_credit, net_balance."""
    rows = [
        {
            "account_number": "4010",
            "canonical_form": "4010·Revenue · Marketplace",
            "account_type": "revenue",
            "total_debit": Decimal("50.0000"),
            "total_credit": Decimal("800.0000"),
        },
    ]
    result, _ = _run_trial_balance(rows)
    assert len(result["accounts"]) == 1
    acct = result["accounts"][0]
    for key in ("account_number", "canonical_form", "account_type",
                "total_debit", "total_credit", "net_balance"):
        assert key in acct, f"Missing key: {key}"
    assert acct["account_type"] == "revenue"
    assert Decimal(acct["net_balance"]) == Decimal("50") - Decimal("800")
