"""Smoke tests for the Schedule C (Form 1040) professional-substitute fill path.

Covers field-map consistency (all names fully qualified, no duplicates, VALID
superset, checkbox/radio on-states) and — when the gitignored IRS PDF is present
— the end-to-end render: provenance-bearing inputs land on the right AcroForm
fields, a radio/Yes-No checkbox is set, an unfilled line stays blank
(conservative blank-fill), and the proprietor SSN / business EIN are never
written.

The render tests skip when ``data/irs_forms/f1040sc.pdf`` is absent (as in CI),
mirroring ``tests/smoke/test_w4p.py`` / ``test_f1040.py``'s ``_needs_pdf``.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.sch_c_fill import render_sch_c, _sch_c_form_path
from app.exports.sch_c_field_map import (
    SCH_C_FIELDS,
    SCH_C_ACCOUNTING_METHOD_FIELDS,
    SCH_C_COGS_METHOD_FIELDS,
    SCH_C_AT_RISK_FIELDS,
    SCH_C_CHECKBOX_FIELDS,
    SCH_C_YESNO_FIELDS,
    VALID_SCH_C_FIELDS,
    _all_checkbox_field_names,
)

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"
_SCH_C_PDF = _sch_c_form_path(None)


def _needs_pdf():
    if not _SCH_C_PDF.exists():
        pytest.skip("Schedule C PDF absent (gitignored) — skipping render test")


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in SCH_C_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_field_names_qualified_with_on_states(self):
        groups = [
            SCH_C_ACCOUNTING_METHOD_FIELDS,
            SCH_C_COGS_METHOD_FIELDS,
            SCH_C_AT_RISK_FIELDS,
            SCH_C_CHECKBOX_FIELDS,
        ]
        for grp in groups:
            for key, (name, on_state) in grp.items():
                assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
                assert on_state and on_state != "Off"
        for gk, grp in SCH_C_YESNO_FIELDS.items():
            for ans, (name, on_state) in grp.items():
                assert name.startswith("topmostSubform"), f"{gk}/{ans} -> {name!r}"
                assert on_state and on_state != "Off"
                assert ans in ("yes", "no")

    def test_no_duplicate_field_names(self):
        all_names = list(SCH_C_FIELDS.values()) + _all_checkbox_field_names()
        seen: dict[str, int] = {}
        for name in all_names:
            seen[name] = seen.get(name, 0) + 1
        dups = {n for n, c in seen.items() if c > 1}
        assert not dups, f"duplicate field names: {dups}"

    def test_valid_fields_is_superset(self):
        assert set(SCH_C_FIELDS.values()) <= VALID_SCH_C_FIELDS
        assert set(_all_checkbox_field_names()) <= VALID_SCH_C_FIELDS
        # VALID is exactly the union (no stray names).
        assert VALID_SCH_C_FIELDS == frozenset(
            list(SCH_C_FIELDS.values()) + _all_checkbox_field_names()
        )

    def test_ssn_and_ein_are_not_mapped(self):
        # Identity boxes left for the human must never appear in the writable set.
        ssn = f"{_P1}.f1_2[0]"
        ein = f"{_P1}.DComb[0].f1_6[0]"
        assert ssn not in VALID_SCH_C_FIELDS
        assert ein not in VALID_SCH_C_FIELDS

    def test_known_on_states(self):
        # Header Yes/No use /Yes,/No; page-2 questions use /1,/2; accounting
        # method radio uses /1,/2,/3. Pinned positionally + by widget /AP.
        assert SCH_C_YESNO_FIELDS["material_participation"]["yes"] == (f"{_P1}.c1_2[0]", "Yes")
        assert SCH_C_YESNO_FIELDS["vehicle_personal_use"]["no"] == (f"{_P2}.c2_5[1]", "2")
        assert SCH_C_ACCOUNTING_METHOD_FIELDS["accrual"] == (f"{_P1}.c1_1[1]", "2")
        assert SCH_C_AT_RISK_FIELDS["some"] == (f"{_P1}.c1_7[1]", "2")

    def test_key_logical_keys_present(self):
        # Interface contract: these common lines must be addressable.
        for k in (
            "name_of_proprietor", "principal_business", "business_code",
            "line1_gross_receipts", "line3_net_receipts", "line4_cogs",
            "line7_gross_income", "line8_advertising", "line9_car_truck",
            "line13_depreciation", "line16a_mortgage_interest",
            "line24a_travel", "line24b_meals", "line27b_other_expenses",
            "line28_total_expenses", "line29_tentative_profit",
            "line31_net_profit", "cogs_line42_cogs",
        ):
            assert k in SCH_C_FIELDS, k


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

def _page_text(pdf: bytes, page_index: int) -> str:
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(pdf)).pages[page_index].extract_text()


class TestRender:
    def test_values_baked_in_visible(self):
        # _render_acroform_pdf flattens values into page content so they render
        # in EVERY viewer (independent of /NeedAppearances). Assert on extracted
        # text. Identity + income/expenses are on page 1; COGS is on page 2.
        _needs_pdf()

        inputs = {
            "name_of_proprietor":   {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "principal_business":   {"value": "Software consulting", "source_ref": "intake#1", "origin": "user_entered"},
            "line1_gross_receipts": {"value": "120000", "source_ref": "books#1", "origin": "document"},
            "line8_advertising":    {"value": "3590", "source_ref": "books#1", "origin": "document"},
            "line31_net_profit":    {"value": "84210", "source_ref": "calc#1", "origin": "computed"},
            "cogs_line42_cogs":     {"value": "15750", "source_ref": "books#1", "origin": "document"},
            "accounting_method":    {"value": "cash", "source_ref": "intake#1", "origin": "user_entered"},
            "material_participation": {"value": "yes", "source_ref": "intake#1", "origin": "user_entered"},
        }
        pdf, ct = render_sch_c(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        p1 = _page_text(pdf, 0)
        for needle in ("Aaron J Stransky", "Software consulting", "120000", "3590", "84210"):
            assert needle in p1, f"{needle!r} not visible on Schedule C page 1"

        p2 = _page_text(pdf, 1)
        assert "15750" in p2, "COGS figure not visible on Schedule C page 2"

    def test_checkbox_groups_render(self):
        # Checkboxes are not extractable text, so verify the seam: each grouped
        # key maps to a distinct field + on-state, and rendering with them
        # produces a valid PDF (the shared, already-tested filler applies /V+/AS).
        _needs_pdf()

        cash_name, cash_on = SCH_C_ACCOUNTING_METHOD_FIELDS["cash"]
        accrual_name, accrual_on = SCH_C_ACCOUNTING_METHOD_FIELDS["accrual"]
        assert cash_name != accrual_name and cash_on != accrual_on

        pdf, _ = render_sch_c(
            2025, WatermarkMode.DRAFT,
            {
                "accounting_method":     {"value": "accrual", "source_ref": "intake#1", "origin": "user_entered"},
                "cogs_method":           {"value": "cost", "source_ref": "intake#1", "origin": "user_entered"},
                "at_risk":               {"value": "all", "source_ref": "intake#1", "origin": "user_entered"},
                "made_1099_payments":    {"value": "no", "source_ref": "intake#1", "origin": "user_entered"},
                "started_business":      {"value": "yes", "source_ref": "intake#1", "origin": "user_entered"},
                "vehicle_evidence":      {"value": "yes", "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        assert pdf[:4] == b"%PDF"

        # Unknown radio/Yes-No values are silently ignored (conservative).
        pdf2, _ = render_sch_c(
            2025, WatermarkMode.DRAFT,
            {
                "accounting_method": {"value": "not_a_method", "source_ref": "x", "origin": "x"},
                "material_participation": {"value": "maybe", "source_ref": "x", "origin": "x"},
                "started_business": {"value": "no", "source_ref": "x", "origin": "x"},  # falsy -> unchecked
            },
        )
        assert pdf2[:4] == b"%PDF"

    def test_conservative_blank_fill(self):
        # Nothing we did not set leaks into the rendered form; an unfilled line
        # (line 1) stays blank when only line 31 is supplied.
        _needs_pdf()

        pdf, _ = render_sch_c(
            2025, WatermarkMode.DRAFT,
            {"line31_net_profit": {"value": "54321", "source_ref": "calc#1", "origin": "computed"}},
        )
        doc = _page_text(pdf, 0) + _page_text(pdf, 1)
        assert "54321" in doc            # the one figure we supplied landed
        assert "120000" not in doc       # a gross-receipts figure we never supplied
        assert "Aaron" not in doc        # an identity we never supplied

    def test_unmapped_and_blank_keys_skipped(self):
        # A blank value is skipped; an unknown key is ignored; only the one real,
        # non-blank, mapped figure becomes visible — none of these raise.
        _needs_pdf()

        pdf, _ = render_sch_c(
            2025, WatermarkMode.NONE,
            {
                "line1_gross_receipts": {"value": "", "source_ref": "books#1", "origin": "document"},
                "not_a_real_key":       {"value": "999", "source_ref": "x", "origin": "x"},
                "line7_gross_income":   {"value": "70042", "source_ref": "calc#1", "origin": "computed"},
            },
        )
        doc = _page_text(pdf, 0) + _page_text(pdf, 1)
        assert "70042" in doc            # the mapped, non-blank figure landed
        assert "999" not in doc          # unknown key never wrote

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"name_of_proprietor": {"value": "Solo Prop", "source_ref": "intake#1", "origin": "user_entered"}}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_sch_c(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_year_fallback_renders(self):
        # A bogus tax year resolves to f1040sc_<year>.pdf then falls back to
        # f1040sc.pdf; empty inputs => an unfilled-but-valid official form.
        _needs_pdf()
        pdf, ct = render_sch_c(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


class TestMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch):
        # When NO source PDF can be resolved, render_sch_c raises FileNotFoundError.
        import app.exports.sch_c_fill as mod
        from pathlib import Path

        monkeypatch.setattr(mod, "_sch_c_form_path", lambda ty: Path("/nonexistent/f1040sc.pdf"))
        with pytest.raises(FileNotFoundError):
            render_sch_c(2025, WatermarkMode.DRAFT,
                         {"name_of_proprietor": {"value": "X", "source_ref": "x", "origin": "x"}})
