"""Smoke tests for IRS form pre-fill logic.

These tests exercise the field-mapping and data-extraction layers without
requiring the actual IRS PDF files to be present (no PDF I/O). They validate:

  1. _parse_line_from_reference — extracts line numbers from form_reference strings.
  2. _build_field_values_from_queue — maps queue items to line→value dicts.
  3. _build_field_values_from_package — maps cpa-export JSON to line→value dicts.
  4. _build_schedule_l_values_from_package — balance sheet mapping.
  5. build_prefill_context — merge + priority logic (queue overrides package).
  6. form_1120_field_map consistency — all mapped lines reference valid 1120 lines.

No real PDF is read or written; FileNotFoundError from generate_form_pdf is
expected when the IRS PDF templates are absent (as they are in CI).
"""

from __future__ import annotations

import pytest

from app.exports.form_prefill import (
    _build_field_values_from_package,
    _build_field_values_from_queue,
    _build_schedule_l_values_from_package,
    _cents_to_dollar_str,
    _parse_line_from_reference,
    build_prefill_context,
    generate_form_pdf,
)
from app.exports.form_1120_field_map import (
    FORM_1120_FIELDS,
    SCHEDULE_L_FIELDS,
    SCHEDULE_L_LINE_TO_FIELD,
)
from app.journal.tax_mapping_1120 import VALID_1120_LINES, VALID_SCHEDULE_L_LINES


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

class TestCentsToDollar:
    def test_positive(self):
        assert _cents_to_dollar_str(123456) == "1234"

    def test_negative(self):
        assert _cents_to_dollar_str(-50000) == "-500"

    def test_zero(self):
        assert _cents_to_dollar_str(0) == "0"

    def test_rounds_down(self):
        # 199 cents → $1 (integer division)
        assert _cents_to_dollar_str(199) == "1"


class TestParseLineFromReference:
    def test_simple(self):
        assert _parse_line_from_reference("Form 1120, Line 26") == "26"

    def test_lettered(self):
        assert _parse_line_from_reference("Form 1120, Line 1a") == "1a"

    def test_compound(self):
        assert _parse_line_from_reference("Form 1120, Line 26 / Form 1125-A") == "26"

    def test_schedule_l(self):
        assert _parse_line_from_reference("Schedule L, Line 10a") == "10a"

    def test_lowercase_line(self):
        assert _parse_line_from_reference("form 1120, line 5") == "5"

    def test_no_line(self):
        assert _parse_line_from_reference("Form 1125-E, Officer 1 Compensation") is None

    def test_empty(self):
        assert _parse_line_from_reference("") is None


# ---------------------------------------------------------------------------
# Queue → field values
# ---------------------------------------------------------------------------

class TestBuildFieldValuesFromQueue:
    def _item(self, form_ref: str, amount_cents: int | None) -> dict:
        content: dict = {}
        if amount_cents is not None:
            content["proposed_amount_cents"] = amount_cents
        return {
            "item_type": "form_line_proposal",
            "form_reference": form_ref,
            "content": content,
            "status": "pending",
        }

    def test_basic_mapping(self):
        items = [self._item("Form 1120, Line 26", 500000)]
        result = _build_field_values_from_queue(items)
        assert result == {"26": "5000"}

    def test_multiple_lines(self):
        items = [
            self._item("Form 1120, Line 1a", 1000000),
            self._item("Form 1120, Line 26", 200000),
            self._item("Form 1120, Line 5", 5000),
        ]
        result = _build_field_values_from_queue(items)
        assert result["1a"] == "10000"
        assert result["26"] == "2000"
        assert result["5"] == "50"

    def test_skips_non_proposal_type(self):
        item = {
            "item_type": "flagged_position",
            "form_reference": "Form 1120, Line 26",
            "content": {"proposed_amount_cents": 100000},
            "status": "pending",
        }
        result = _build_field_values_from_queue([item])
        assert result == {}

    def test_skips_missing_amount(self):
        items = [self._item("Form 1120, Line 26", None)]
        result = _build_field_values_from_queue(items)
        assert result == {}

    def test_skips_no_line_reference(self):
        items = [self._item("Form 1125-E, Officer compensation", 50000)]
        result = _build_field_values_from_queue(items)
        assert result == {}

    def test_empty_queue(self):
        assert _build_field_values_from_queue([]) == {}

    def test_negative_amount(self):
        items = [self._item("Form 1120, Line 18", -30000)]
        result = _build_field_values_from_queue(items)
        assert result["18"] == "-300"


# ---------------------------------------------------------------------------
# cpa-export package → field values
# ---------------------------------------------------------------------------

class TestBuildFieldValuesFromPackage:
    def _pkg(self, income_lines=None, deduction_lines=None, taxable_before_nol=None):
        return {
            "income": {
                "lines": income_lines or [],
                "total_income_cents": 0,
            },
            "deductions": {
                "lines": deduction_lines or [],
                "total_deductions_cents": 0,
            },
            "taxable_income_before_nol_cents": taxable_before_nol,
        }

    def test_income_lines(self):
        pkg = self._pkg(
            income_lines=[{"line": "1a", "amount_cents": 5000000, "label": "Gross receipts"}]
        )
        result = _build_field_values_from_package(pkg)
        assert result["1a"] == "50000"

    def test_deduction_lines(self):
        pkg = self._pkg(
            deduction_lines=[{"line": "26", "amount_cents": 1234500, "label": "Other deductions"}]
        )
        result = _build_field_values_from_package(pkg)
        assert result["26"] == "12345"

    def test_taxable_income_line_28(self):
        pkg = self._pkg(taxable_before_nol=99900)
        result = _build_field_values_from_package(pkg)
        assert result["28"] == "999"

    def test_empty_package(self):
        result = _build_field_values_from_package(self._pkg())
        assert result == {}


# ---------------------------------------------------------------------------
# Schedule L → field values
# ---------------------------------------------------------------------------

class TestBuildScheduleLValues:
    def _pkg(self, assets=None, liabilities=None, total_assets=None, total_liab=None):
        return {
            "schedule_l": {
                "assets": assets or [],
                "liabilities_equity": liabilities or [],
                "total_assets_cents": total_assets,
                "total_liabilities_equity_cents": total_liab,
            }
        }

    def test_cash(self):
        pkg = self._pkg(assets=[{"line": "1", "label": "Cash", "amount_cents": 500000}])
        result = _build_schedule_l_values_from_package(pkg)
        assert result["1_eoy"] == "5000"

    def test_accounts_payable(self):
        pkg = self._pkg(liabilities=[{"line": "16", "label": "AP", "amount_cents": 200000}])
        result = _build_schedule_l_values_from_package(pkg)
        assert result["16_eoy"] == "2000"

    def test_total_assets(self):
        pkg = self._pkg(total_assets=1000000)
        result = _build_schedule_l_values_from_package(pkg)
        assert result["15_eoy"] == "10000"

    def test_total_liab_equity(self):
        pkg = self._pkg(total_liab=800000)
        result = _build_schedule_l_values_from_package(pkg)
        assert result["28_eoy"] == "8000"

    def test_unknown_line_ignored(self):
        # Line 99 is not in SCHEDULE_L_LINE_TO_FIELD — should be silently ignored.
        pkg = self._pkg(assets=[{"line": "99", "label": "Unknown", "amount_cents": 999}])
        result = _build_schedule_l_values_from_package(pkg)
        assert "99_eoy" not in result


# ---------------------------------------------------------------------------
# build_prefill_context — merge + priority
# ---------------------------------------------------------------------------

class TestBuildPrefillContext:
    def _queue_item(self, form_ref: str, amount_cents: int) -> dict:
        return {
            "item_type": "form_line_proposal",
            "form_reference": form_ref,
            "content": {"proposed_amount_cents": amount_cents},
            "status": "pending",
        }

    def test_queue_overrides_package(self):
        pkg = {
            "income": {"lines": [{"line": "1a", "amount_cents": 1000000}]},
            "deductions": {"lines": []},
            "taxable_income_before_nol_cents": None,
        }
        queue = [self._queue_item("Form 1120, Line 1a", 2000000)]
        ctx = build_prefill_context(
            tax_year=2023,
            entity_display_name="Acme Inc",
            ein=None,
            queue_items=queue,
            cpa_package=pkg,
        )
        # Queue value (20000) overrides package value (10000)
        assert ctx.p1_values["1a"] == "20000"

    def test_package_used_when_no_queue(self):
        pkg = {
            "income": {"lines": [{"line": "5", "amount_cents": 75000}]},
            "deductions": {"lines": []},
            "taxable_income_before_nol_cents": None,
        }
        ctx = build_prefill_context(
            tax_year=2023,
            entity_display_name="Acme Inc",
            ein=None,
            queue_items=[],
            cpa_package=pkg,
        )
        assert ctx.p1_values["5"] == "750"

    def test_no_data_means_blank(self):
        """Lines with no data from either source must NOT appear in p1_values."""
        ctx = build_prefill_context(
            tax_year=2023,
            entity_display_name="Acme Inc",
            ein=None,
            queue_items=[],
            cpa_package=None,
        )
        assert "1a" not in ctx.p1_values
        assert "26" not in ctx.p1_values

    def test_filled_and_blank_lines_tracked(self):
        pkg = {
            "income": {"lines": [{"line": "1a", "amount_cents": 500000}]},
            "deductions": {"lines": []},
            "taxable_income_before_nol_cents": None,
        }
        ctx = build_prefill_context(
            tax_year=2023,
            entity_display_name="Test Corp",
            ein=None,
            queue_items=[],
            cpa_package=pkg,
        )
        assert "1a" in ctx.filled_lines
        assert "26" in ctx.blank_lines
        # filled + blank should cover all 1120 lines
        all_lines = set(FORM_1120_FIELDS.keys())
        assert set(ctx.filled_lines) | set(ctx.blank_lines) == all_lines

    def test_entity_name_set(self):
        ctx = build_prefill_context(
            tax_year=2024,
            entity_display_name="Unicorn Commander LLC",
            ein="12-3456789",
            queue_items=[],
            cpa_package=None,
        )
        assert ctx.entity_display_name == "Unicorn Commander LLC"
        assert ctx.ein == "12-3456789"

    def test_officer_comp_from_queue(self):
        item = {
            "item_type": "form_line_proposal",
            "form_reference": "Form 1125-E, Line 3 (Net Officer Comp)",
            "content": {
                "proposed_amount_cents": 15000000,
                "officer1_name": "Jane Smith",
                "officer1_title": "CEO",
                "officer1_pct_time": "100",
            },
            "status": "pending",
        }
        ctx = build_prefill_context(
            tax_year=2023,
            entity_display_name="Acme Inc",
            ein=None,
            queue_items=[item],
            cpa_package=None,
        )
        assert ctx.officer_values.get("net_officer_comp") == "150000"
        assert ctx.officer_values.get("officer1_name") == "Jane Smith"
        assert ctx.officer_values.get("officer1_title") == "CEO"


# ---------------------------------------------------------------------------
# Field-map consistency checks
# ---------------------------------------------------------------------------

class TestFieldMapConsistency:
    def test_all_1120_lines_are_valid(self):
        """Every FORM_1120_FIELDS key must be in VALID_1120_LINES."""
        for line in FORM_1120_FIELDS:
            assert line in VALID_1120_LINES, (
                f"FORM_1120_FIELDS key '{line}' is not in VALID_1120_LINES. "
                "Update VALID_1120_LINES or fix the mapping."
            )

    def test_schedule_l_line_to_field_keys_are_valid(self):
        """Every SCHEDULE_L_LINE_TO_FIELD key must be in VALID_SCHEDULE_L_LINES."""
        for line in SCHEDULE_L_LINE_TO_FIELD:
            assert line in VALID_SCHEDULE_L_LINES, (
                f"SCHEDULE_L_LINE_TO_FIELD key '{line}' is not in "
                "VALID_SCHEDULE_L_LINES."
            )

    def test_schedule_l_fields_values_reference_line_to_field(self):
        """Every value in SCHEDULE_L_LINE_TO_FIELD must be a key in SCHEDULE_L_FIELDS."""
        for line, field_key in SCHEDULE_L_LINE_TO_FIELD.items():
            assert field_key in SCHEDULE_L_FIELDS, (
                f"SCHEDULE_L_LINE_TO_FIELD['{line}'] = '{field_key}' "
                "but that key is not in SCHEDULE_L_FIELDS."
            )

    def test_form_1120_fields_values_are_strings(self):
        for line, field_name in FORM_1120_FIELDS.items():
            assert isinstance(field_name, str) and field_name.startswith(
                "topmostSubform"
            ), f"Field name for line '{line}' looks wrong: {field_name!r}"

    def test_no_duplicate_field_names_in_1120(self):
        """No two lines should map to the same AcroForm field name."""
        seen: dict[str, str] = {}
        for line, field_name in FORM_1120_FIELDS.items():
            assert field_name not in seen, (
                f"Duplicate AcroForm field '{field_name}' for lines "
                f"'{seen[field_name]}' and '{line}'."
            )
            seen[field_name] = line


# ---------------------------------------------------------------------------
# generate_form_pdf — FileNotFoundError when PDFs are absent (expected in CI)
# ---------------------------------------------------------------------------

class TestGenerateFormPdfMissingPdf:
    def test_raises_file_not_found_when_pdf_absent(self):
        """Without the IRS PDFs downloaded, FileNotFoundError should be raised."""
        from pathlib import Path
        from app.exports.form_prefill import IRS_FORMS_DIR

        # Only run this assertion if the form PDF is actually absent
        form_path = IRS_FORMS_DIR / "f1120_2024.pdf"
        if form_path.exists():
            pytest.skip("IRS PDF present — skipping absence check")

        with pytest.raises(FileNotFoundError, match="f1120_2024.pdf"):
            generate_form_pdf(
                form_name="1120",
                tax_year=2024,
                entity_display_name="Test Corp",
                ein=None,
                queue_items=[],
                cpa_package=None,
            )

    def test_unknown_form_name_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown form_name"):
            generate_form_pdf(
                form_name="9999-x",
                tax_year=2024,
                entity_display_name="Test Corp",
                ein=None,
                queue_items=[],
                cpa_package=None,
            )
