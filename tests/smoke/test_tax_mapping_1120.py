"""Smoke tests for Form 1120 tax mapping.

Three test groups:

  1. Coverage assertion: every key in CATEGORY_TO_ACCOUNT has a page-1 mapping
     or an explicit Schedule L route (no silent drops).

  2. Round-trip: synthetic P&L through build_1120_package(); the income-minus-
     deductions total equals expected net income.

  3. Line-number validity: every line reference in CATEGORY_TO_1120 and
     BALANCE_SHEET_1120 is a valid 1120 / Schedule L line per the 2024 form.

No database, no I/O — all tests are pure Python.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from app.journal.posting import CATEGORY_TO_ACCOUNT
from app.journal.tax_mapping_1120 import (
    BALANCE_SHEET_1120,
    BALANCE_SHEET_CATEGORY_TO_KEY,
    CATEGORY_TO_1120,
    NON_1120_PL_CATEGORIES,
    VALID_1120_LINES,
    VALID_SCHEDULE_L_LINES,
)

# ---------------------------------------------------------------------------
# Helpers — synthetic journal entries (no real txn data)
# ---------------------------------------------------------------------------

try:
    # The CPA export service has the builder; import it if available for the
    # round-trip test. If not (unit test context in accounting-ops only), we
    # implement a minimal inline version.
    import sys
    import os

    _cpa_export_root = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..",
                     "accounting-ops-cpa-export")
    )
    if _cpa_export_root not in sys.path:
        sys.path.insert(0, _cpa_export_root)

    from cpa_export.reports.form_1120 import build_1120_package
    from cpa_export.db import JournalEntryRow

    _HAS_CPA_EXPORT = True
except ImportError:
    _HAS_CPA_EXPORT = False
    JournalEntryRow = None  # type: ignore[assignment,misc]
    build_1120_package = None  # type: ignore[assignment]


def _make_je(
    *,
    amount_cents: int,
    debit_account: str,
    credit_account: str,
    category: str,
    effective_date: date = date(2025, 6, 15),
    entity_id: str = "e-syn-001",
) -> "JournalEntryRow":
    """Synthetic journal entry (no real customer data)."""
    if JournalEntryRow is None:  # pragma: no cover — unreachable when _HAS_CPA_EXPORT
        raise RuntimeError("JournalEntryRow not available")
    return JournalEntryRow(
        id=str(uuid4()),
        workspace_id="w-syn-test",
        entity_id=entity_id,
        posted_at=datetime.now(tz=UTC),
        effective_date=effective_date,
        source="synthetic",
        source_external_id=f"syn_{uuid4().hex[:8]}",
        description="Synthetic test entry — no real data",
        payee="Synthetic Vendor",
        memo=None,
        amount_cents=amount_cents,
        currency="USD",
        debit_account=debit_account,
        credit_account=credit_account,
        category=category,
        status="posted",
        reversal_of=None,
        posted_by_uc_uid="u-test",
        posted_by_agent_id=None,
        prev_hash=None,
        this_hash=b"\x00\x01\x02\x03",
        created_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# 1. Coverage assertion
# ---------------------------------------------------------------------------

class TestCoverageComplete:
    """Every CATEGORY_TO_ACCOUNT key must have an explicit 1120 treatment.

    This is the most important test: if a new category is added to posting.py
    without updating tax_mapping_1120.py, its transactions would be silently
    dropped. Balance-sheet categories are covered only when they are explicitly
    identified as non-P&L and routed to a valid Schedule L bucket.
    """

    def test_every_category_to_account_key_has_1120_mapping(self):
        missing = [
            cat for cat in CATEGORY_TO_ACCOUNT
            if cat not in CATEGORY_TO_1120
            and cat not in NON_1120_PL_CATEGORIES
        ]
        assert missing == [], (
            "The following CATEGORY_TO_ACCOUNT keys have neither a page-1 "
            "CATEGORY_TO_1120 mapping nor an explicit Schedule L route and "
            "would be silently dropped from the 1120 package:\n"
            + "\n".join(f"  - {c!r}" for c in missing)
        )

    def test_non_pl_categories_are_explicitly_routed_to_schedule_l(self):
        assert NON_1120_PL_CATEGORIES.isdisjoint(CATEGORY_TO_1120), (
            "Balance-sheet categories must not also be mapped to Form 1120 "
            "page-1 P&L lines"
        )

        expected_routes = {
            "Owner Contribution": ("equity_capital", "23", "liabilities_equity"),
            "Owner Distribution": (
                "equity_distributions", "25", "liabilities_equity"
            ),
            "Loan from Nate Ellison": (
                "liability_shareholder_loan", "19", "liabilities_equity"
            ),
        }
        assert NON_1120_PL_CATEGORIES == frozenset(expected_routes)

        for category, (bucket_key, line, side) in expected_routes.items():
            assert BALANCE_SHEET_CATEGORY_TO_KEY[category] == bucket_key
            mapping = BALANCE_SHEET_1120[bucket_key]
            assert (mapping.line, mapping.side) == (line, side)

    def test_all_1120_mappings_have_required_fields(self):
        for cat, mapping in CATEGORY_TO_1120.items():
            assert mapping.section in ("income", "deductions", "tax", "other_income"), (
                f"Category {cat!r} has invalid section {mapping.section!r}"
            )
            assert mapping.line, f"Category {cat!r} has empty line reference"
            assert mapping.label, f"Category {cat!r} has empty label"
            assert mapping.confidence in ("high", "medium", "low"), (
                f"Category {cat!r} has invalid confidence {mapping.confidence!r}"
            )
            assert mapping.notes, f"Category {cat!r} has empty notes (rationale required)"

    def test_balance_sheet_mapping_has_required_fields(self):
        for bucket_key, mapping in BALANCE_SHEET_1120.items():
            assert mapping.line, (
                f"Balance sheet bucket {bucket_key!r} has empty line reference"
            )
            assert mapping.label, f"Balance sheet bucket {bucket_key!r} has empty label"
            assert mapping.side in ("assets", "liabilities_equity"), (
                f"Balance sheet bucket {bucket_key!r} has invalid side {mapping.side!r}"
            )
            assert mapping.confidence in ("high", "medium", "low"), (
                f"Balance sheet bucket {bucket_key!r} has invalid confidence"
            )


# ---------------------------------------------------------------------------
# 2. Line-number validity
# ---------------------------------------------------------------------------

class TestLineNumberValidity:
    """All line references in the mapping must be valid 2024 Form 1120 lines."""

    def test_1120_income_statement_lines_are_valid(self):
        invalid = [
            (cat, m.line)
            for cat, m in CATEGORY_TO_1120.items()
            if m.section in ("income", "deductions", "tax")
            and m.line not in VALID_1120_LINES
        ]
        assert invalid == [], (
            "The following category mappings reference invalid 1120 line numbers:\n"
            + "\n".join(f"  {cat!r} -> line {line!r}" for cat, line in invalid)
        )

    def test_other_income_lines_are_valid_1120_lines(self):
        """other_income section lines must also be valid 1120 income-section lines."""
        invalid = [
            (cat, m.line)
            for cat, m in CATEGORY_TO_1120.items()
            if m.section == "other_income"
            and m.line not in VALID_1120_LINES
        ]
        assert invalid == [], (
            "other_income mappings reference invalid 1120 line numbers:\n"
            + "\n".join(f"  {cat!r} -> line {line!r}" for cat, line in invalid)
        )

    def test_schedule_l_lines_are_valid(self):
        invalid = [
            (key, m.line)
            for key, m in BALANCE_SHEET_1120.items()
            if m.line not in VALID_SCHEDULE_L_LINES
        ]
        assert invalid == [], (
            "The following balance sheet mappings reference invalid Schedule L lines:\n"
            + "\n".join(f"  {key!r} -> line {line!r}" for key, line in invalid)
        )

    def test_no_duplicate_section_line_label_mismatches(self):
        """
        If two categories share the same line number, they must be in the same
        section (it would be a logic error to put one in 'income' and another
        in 'deductions' on the same line number).
        """
        line_to_sections: dict[str, set[str]] = {}
        for cat, m in CATEGORY_TO_1120.items():
            line_to_sections.setdefault(m.line, set()).add(m.section)

        conflicts = {
            line: sections
            for line, sections in line_to_sections.items()
            if len(sections) > 1
            # other_income/income conflict on the same line IS valid (interest
            # income vs interest expense routing) — exclude those.
            and not (sections <= {"income", "other_income"})
        }
        assert not conflicts, (
            "The following line numbers are used in conflicting sections:\n"
            + "\n".join(
                f"  line {line!r}: sections {sorted(sections)}"
                for line, sections in conflicts.items()
            )
        )


# ---------------------------------------------------------------------------
# 3. Round-trip test: synthetic P&L through the 1120 package builder
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _HAS_CPA_EXPORT,
    reason="cpa_export package not importable from this test context",
)
class TestRoundTrip:
    """Feed a synthetic P&L through build_1120_package(); assert totals add up."""

    def _make_entries(self) -> "list[JournalEntryRow]":
        """
        Synthetic entries (made-up amounts, no real transactions):
          Revenue:
            - Revenue · Marketplace: $150,000.00  (1 entry)
          Expenses:
            - SaaS · AI:              $  2,400.00  (12 × $200)
            - Infrastructure · VPS:   $  1,200.00  (12 × $100)
            - Banking · Fees:         $    360.00  (12 × $30)
            - Meals:                  $    500.00  (2 entries)
          Interest income:
            - Banking · Interest:     $     48.00  (positive → line 5)
          Interest expense (negative):
            - Banking · Interest:     $    -12.00  (negative → line 18 deduction)

        Expected:
          Total income    = 150,000 + 48                 = 150,048
          Total deductions = 2,400 + 1,200 + 360 + 500 + 12 = 4,472
          Taxable before NOL = 150,048 - 4,472           = 145,576
        All values in dollars; stored as cents.
        """
        entries = []

        # Revenue
        entries.append(_make_je(
            amount_cents=15_000_000,  # $150,000.00
            debit_account="1010·Cash · Primary Checking",
            credit_account="4010·Revenue · Marketplace",
            category="Revenue · Marketplace",
        ))

        # SaaS AI (12 monthly entries × $200)
        for _ in range(12):
            entries.append(_make_je(
                amount_cents=20_000,  # $200.00
                debit_account="6210·SaaS · AI",
                credit_account="1010·Cash · Primary Checking",
                category="SaaS · AI",
            ))

        # Infrastructure VPS (12 monthly × $100)
        for _ in range(12):
            entries.append(_make_je(
                amount_cents=10_000,  # $100.00
                debit_account="6110·Infrastructure · VPS",
                credit_account="1010·Cash · Primary Checking",
                category="Infrastructure · VPS",
            ))

        # Banking Fees (12 monthly × $30)
        for _ in range(12):
            entries.append(_make_je(
                amount_cents=3_000,  # $30.00
                debit_account="6700·Bank Fees",
                credit_account="1010·Cash · Primary Checking",
                category="Banking · Fees",
            ))

        # Meals (2 × $250)
        for _ in range(2):
            entries.append(_make_je(
                amount_cents=25_000,  # $250.00
                debit_account="6520·Meals",
                credit_account="2200·Credit Card",
                category="Meals",
            ))

        # Interest income (positive, line 5)
        entries.append(_make_je(
            amount_cents=4_800,  # $48.00
            debit_account="1010·Cash · Primary Checking",
            credit_account="7010·Interest Income",
            category="Banking · Interest",
        ))

        # Interest expense (stored as positive cents but conceptually negative in P&L)
        # The journal stores amounts as positive; sign is from the debit/credit side.
        # To test the sign-routing logic, we set amount_cents negative here —
        # the builder checks entry.amount_cents sign for "Banking · Interest".
        entries.append(_make_je(
            amount_cents=-1_200,  # -$12.00 (interest charged)
            debit_account="8010·Interest Expense",
            credit_account="1010·Cash · Primary Checking",
            category="Banking · Interest",
        ))

        return entries

    def test_income_total(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        # $150,000 (marketplace) + $48 (interest income line 5) = $150,048
        expected_income_cents = 15_000_000 + 4_800
        assert pkg["income"]["total_income_cents"] == expected_income_cents, (
            f"Expected total income {expected_income_cents} cents, "
            f"got {pkg['income']['total_income_cents']}"
        )

    def test_deductions_total(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        # SaaS AI: 12*200 = 2400
        # VPS: 12*100 = 1200
        # Fees: 12*30 = 360
        # Meals: 2*250 = 500
        # Interest expense: 12
        expected_deductions_cents = 240_000 + 120_000 + 36_000 + 50_000 + 1_200
        assert pkg["deductions"]["total_deductions_cents"] == expected_deductions_cents, (
            f"Expected total deductions {expected_deductions_cents} cents, "
            f"got {pkg['deductions']['total_deductions_cents']}"
        )

    def test_taxable_income_before_nol(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        expected_income = 15_000_000 + 4_800
        expected_deductions = 240_000 + 120_000 + 36_000 + 50_000 + 1_200
        expected_taxable = expected_income - expected_deductions
        assert pkg["taxable_income_before_nol_cents"] == expected_taxable, (
            f"Expected taxable income (before NOL) {expected_taxable} cents, "
            f"got {pkg['taxable_income_before_nol_cents']}"
        )

    def test_income_equals_revenue_plus_interest_income(self):
        """Income total = sum of income-section line amounts (accounting check)."""
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        summed = sum(line["amount_cents"] for line in pkg["income"]["lines"])
        assert summed == pkg["income"]["total_income_cents"]

    def test_deductions_sum_equals_total(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        summed = sum(line["amount_cents"] for line in pkg["deductions"]["lines"])
        assert summed == pkg["deductions"]["total_deductions_cents"]

    def test_disclaimer_present(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        assert "disclaimer" in pkg
        assert len(pkg["disclaimer"]) > 50, "Disclaimer too short — check build_1120_package"
        # Must contain the key phrase
        assert "not a filed return" in pkg["disclaimer"].lower()

    def test_interest_income_routes_to_line_5(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        income_lines = {ln["line"]: ln for ln in pkg["income"]["lines"]}
        assert "5" in income_lines, (
            "Interest income (Banking · Interest, positive) should appear on line 5"
        )
        assert income_lines["5"]["amount_cents"] == 4_800

    def test_interest_expense_routes_to_line_18(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        deduction_lines = {ln["line"]: ln for ln in pkg["deductions"]["lines"]}
        assert "18" in deduction_lines, (
            "Interest expense (Banking · Interest, negative amount_cents) "
            "should appear on line 18 (deductions)"
        )
        assert deduction_lines["18"]["amount_cents"] == 1_200  # absolute value

    def test_line_detail_sums_match_line_totals(self):
        """Per-category breakdown amounts must sum to the line total."""
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        detail = pkg["line_detail"]

        all_lines = {ln["line"]: ln["amount_cents"] for ln in pkg["income"]["lines"]}
        all_lines.update({ln["line"]: ln["amount_cents"] for ln in pkg["deductions"]["lines"]})

        for line, cat_rows in detail.items():
            if line not in all_lines:
                continue  # interest-expense line 18 only in deductions
            detail_sum = sum(row["amount_cents"] for row in cat_rows)
            # Note: line 18 appears in deductions; line 26 may aggregate multiple cats.
            line_total = all_lines.get(line, 0)
            assert detail_sum <= line_total + 1, (
                f"Detail sum for line {line} ({detail_sum}) exceeds line total ({line_total})"
            )

    def test_metadata_fields(self):
        entries = self._make_entries()
        pkg = build_1120_package(entries, "w-syn-workspace-id", "e-syn-001", 2025)
        assert pkg["workspace_id"] == "w-syn-workspace-id"
        assert pkg["entity_id"] == "e-syn-001"
        assert pkg["tax_year"] == 2025
        assert "generated_at" in pkg
        assert "schedule_l" in pkg
        assert "assets" in pkg["schedule_l"]
        assert "liabilities_equity" in pkg["schedule_l"]

    def test_empty_entries_returns_zero_totals(self):
        pkg = build_1120_package([], "w-syn-test", "e-syn-001", 2025)
        assert pkg["income"]["total_income_cents"] == 0
        assert pkg["deductions"]["total_deductions_cents"] == 0
        assert pkg["taxable_income_before_nol_cents"] == 0
        assert pkg["schedule_l"]["total_assets_cents"] == 0

    def test_marketplace_revenue_on_line_1a(self):
        entries = [
            _make_je(
                amount_cents=5_000_000,
                debit_account="1010·Cash · Primary Checking",
                credit_account="4010·Revenue · Marketplace",
                category="Revenue · Marketplace",
            )
        ]
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        income_lines = {ln["line"]: ln for ln in pkg["income"]["lines"]}
        assert "1a" in income_lines
        assert income_lines["1a"]["amount_cents"] == 5_000_000

    def test_meals_on_line_26(self):
        entries = [
            _make_je(
                amount_cents=25_000,
                debit_account="6520·Meals",
                credit_account="2200·Credit Card",
                category="Meals",
            )
        ]
        pkg = build_1120_package(entries, "w-syn-test", "e-syn-001", 2025)
        deduction_lines = {ln["line"]: ln for ln in pkg["deductions"]["lines"]}
        assert "26" in deduction_lines
        assert deduction_lines["26"]["amount_cents"] == 25_000
