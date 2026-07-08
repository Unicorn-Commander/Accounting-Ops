"""Balance-sheet ``balanced`` flag regression test (fix #1).

The bug: ``str(Decimal("0.0000")) == "0"`` is False, so perfectly balanced
books were reported as unbalanced.  The fix uses Decimal comparison:
``drift == Decimal(0)``.

These tests are pure-Python (no DB) — the DB session is faked exactly as in
test_reports_1099_nec.py.
"""
from __future__ import annotations

import asyncio
import contextlib
from decimal import Decimal
from uuid import uuid4

from app.api import reports


# ---------------------------------------------------------------------------
# Fake session infrastructure (mirrors test_reports_1099_nec.py)
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
    """Returns canned row sets in order of calls."""

    def __init__(self, *row_sets):
        self._row_sets = list(row_sets)
        self._call = 0
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)
        rows = self._row_sets[self._call] if self._call < len(self._row_sets) else []
        self._call += 1
        return _FakeResult(rows)


@contextlib.asynccontextmanager
async def _fake_workspace_session(session):
    yield session


def _build_ws():
    return reports.WorkspaceContext(
        workspace_id=uuid4(),
        workspace_slug="test",
        role="owner",
        user_uc_uid="u",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )


def _run_balance_sheet(journal_rows, entity_label_row=None):
    """Invoke balance_sheet with a faked workspace session.

    entity_id=None is passed so _entity_label() returns early (no DB query).
    The single execute() call is therefore the main aggregation query.
    """
    # When entity_id=None, _entity_label returns 'Consolidated' without hitting
    # the DB.  The first (and only) execute() call in the session is the
    # WITH all_sides / aggregation query.
    fake = _FakeSession(journal_rows)
    original = reports.workspace_session
    reports.workspace_session = lambda *_a, **_k: _fake_workspace_session(fake)
    try:
        ws = _build_ws()
        result = asyncio.run(
            reports.balance_sheet(
                workspace_id=ws.workspace_id,
                as_of="2025-12-31",
                entity_id=None,
                workspace=ws,
            )
        )
    finally:
        reports.workspace_session = original
    return result


# ---------------------------------------------------------------------------
# Regression: four-decimal-place zero drift must yield balanced=True
# ---------------------------------------------------------------------------


def test_balanced_flag_true_when_drift_is_four_decimal_zero():
    """Core regression: drift=Decimal('0.0000') must set balanced=True.

    Synthetic journal: one asset debit ($100) fully offset by one liability
    credit ($100).  The aggregation returns two rows (asset and liability),
    each with amount=100.  The balance is exact; drift = 0.0000.
    """
    # Simulate what Postgres returns: two rows in all_sides aggregation.
    # account_number drives _account_type_prefix.
    rows = [
        {"account_number": "1010", "canonical_form": "1010·Checking", "sum_debit": Decimal("100.0000"), "sum_credit": Decimal("0.0000")},
        {"account_number": "2010", "canonical_form": "2010·Accounts Payable", "sum_debit": Decimal("0.0000"), "sum_credit": Decimal("100.0000")},
    ]
    result = _run_balance_sheet(rows)

    # The core assertion: balanced must be True even though str(drift) = "0.0000"
    assert result["balanced"] is True, (
        f"Expected balanced=True but got balanced={result['balanced']!r}; "
        f"drift={result['drift']!r}.  "
        "This is the regression test for the str(drift)==\"0\" bug."
    )
    # drift is str(Decimal); Decimal comparison covers "0", "0.0000", etc.
    assert Decimal(result["drift"]) == 0


def test_balanced_flag_false_when_genuinely_unbalanced():
    """Unbalanced books produce balanced=False with the correct drift."""
    rows = [
        # Assets: $100 debit
        {"account_number": "1010", "canonical_form": "1010·Checking", "sum_debit": Decimal("100.0000"), "sum_credit": Decimal("0.0000")},
        # Liability: $80 credit  (books are off by $20)
        {"account_number": "2010", "canonical_form": "2010·Accounts Payable", "sum_debit": Decimal("0.0000"), "sum_credit": Decimal("80.0000")},
    ]
    result = _run_balance_sheet(rows)
    assert result["balanced"] is False
    # drift = assets(100) - (liabilities(80) + equity(0)) = 20
    assert Decimal(result["drift"]) == Decimal("20")


def test_balanced_flag_true_with_retained_earnings():
    """Balanced sheet that uses the retained-earnings computed line."""
    # Asset: $500 debit
    # Revenue: $300 credit (goes into retained earnings)
    # Equity: $200 credit (explicit equity)
    # assets(500) == liab(0) + equity(200) + retained(300) = 500 -> balanced
    rows = [
        {"account_number": "1010", "canonical_form": "1010·Checking", "sum_debit": Decimal("500.0000"), "sum_credit": Decimal("0.0000")},
        {"account_number": "3010", "canonical_form": "3010·Common Stock", "sum_debit": Decimal("0.0000"), "sum_credit": Decimal("200.0000")},
        {"account_number": "4010", "canonical_form": "4010·Revenue", "sum_debit": Decimal("0.0000"), "sum_credit": Decimal("300.0000")},
    ]
    result = _run_balance_sheet(rows)
    assert result["balanced"] is True
    assert Decimal(result["drift"]) == 0


# ---------------------------------------------------------------------------
# Cash-flow v2 classifier unit tests
# ---------------------------------------------------------------------------
#
# _classify_cash_flow_v2 is a pure function; test each bucket rule directly.


def test_v2_classifies_fixed_asset_as_investing():
    """counter type='asset' + account starting with '15' -> investing."""
    assert reports._classify_cash_flow_v2("1500", "asset") == "investing"
    assert reports._classify_cash_flow_v2("1550", "asset") == "investing"
    # A future 15xx intangible also classifies as investing
    assert reports._classify_cash_flow_v2("1520", "asset") == "investing"


def test_v2_classifies_short_term_asset_as_operating():
    """counter type='asset' NOT starting with '15' -> operating."""
    # Accounts receivable (1200 range) is NOT investing
    assert reports._classify_cash_flow_v2("1200", "asset") == "operating"
    assert reports._classify_cash_flow_v2("1010", "asset") == "operating"


def test_v2_classifies_equity_as_financing():
    """counter type='equity' (3xxx) -> financing."""
    assert reports._classify_cash_flow_v2("3010", "equity") == "financing"
    assert reports._classify_cash_flow_v2("3099", "equity") == "financing"


def test_v2_classifies_notes_payable_as_financing():
    """counter type='liability' AND number='2400' -> financing."""
    assert reports._classify_cash_flow_v2("2400", "liability") == "financing"


def test_v2_classifies_other_liability_as_operating():
    """counter type='liability' but NOT 2400 -> operating."""
    assert reports._classify_cash_flow_v2("2010", "liability") == "operating"


def test_v2_classifies_inter_entity_transfers_as_financing():
    """counter number 6900 or 7900 -> financing regardless of type."""
    assert reports._classify_cash_flow_v2("6900", "expense") == "financing"
    assert reports._classify_cash_flow_v2("7900", "other_income") == "financing"


def test_v2_classifies_expense_as_operating():
    """Standard expense (6xxx) -> operating."""
    assert reports._classify_cash_flow_v2("6210", "expense") == "operating"
    assert reports._classify_cash_flow_v2("6300", "expense") == "operating"


def test_v2_falls_back_to_v1_when_no_type():
    """When counter_type is None (COA JOIN miss) v1 logic is used.

    v1: equity prefix (3xxx) -> financing; 1500/1550 -> investing; else operating.
    """
    # v1: 3xxx -> financing
    assert reports._classify_cash_flow_v2("3010", None) == "financing"
    # v1: 2400 -> financing  (explicit number check in v1 fallback)
    assert reports._classify_cash_flow_v2("2400", None) == "financing"
    # v1: 1500 -> investing
    assert reports._classify_cash_flow_v2("1500", None) == "investing"
    assert reports._classify_cash_flow_v2("1550", None) == "investing"
    # v1: expense -> operating
    assert reports._classify_cash_flow_v2("6210", None) == "operating"
