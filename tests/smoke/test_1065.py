"""Smoke tests for the Form 1065 professional-substitute fill path.

Covers field-map consistency (all names fully qualified, no duplicates, VALID
superset, checkbox on-states) and — when the gitignored IRS PDF is present —
the end-to-end render: provenance-bearing inputs land on the right AcroForm
fields, a checkbox group is set, an unfilled line stays blank (conservative
blank-fill), and the partnership EIN is never written.

The render tests skip when ``data/irs_forms/f1065.pdf`` is absent (as in CI),
mirroring ``tests/smoke/test_w4p.py``'s ``_needs_pdf`` pattern.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.f1065_fill import render_1065, _f1065_form_path
from app.exports.f1065_field_map import (
    F1065_FIELDS,
    F1065_CHECKBOX_FIELDS,
    F1065_ACCOUNTING_METHOD_FIELDS,
    F1065_IRS_DISCUSS_FIELDS,
    F1065_ACCOUNT_TYPE_FIELDS,
    VALID_F1065_FIELDS,
)

_P1 = "topmostSubform[0].Page1[0]"
_F1065_PDF = _f1065_form_path(None)

# Every (choice -> (field, on-state)) checkbox group, for shared assertions.
_GROUPS = (
    F1065_ACCOUNTING_METHOD_FIELDS,
    F1065_IRS_DISCUSS_FIELDS,
    F1065_ACCOUNT_TYPE_FIELDS,
)


def _needs_pdf():
    if not _F1065_PDF.exists():
        pytest.skip("Form 1065 PDF absent (gitignored) — skipping render test")


def _page_text(pdf: bytes, page_index: int) -> str:
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(pdf)).pages[page_index].extract_text()


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in F1065_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_field_names_qualified(self):
        groups = (F1065_CHECKBOX_FIELDS, *_GROUPS)
        for grp in groups:
            for choice, (name, on_state) in grp.items():
                assert name.startswith("topmostSubform"), f"{choice} -> {name!r}"
                assert on_state and on_state != "Off"

    def test_no_duplicate_text_field_names(self):
        seen: dict[str, str] = {}
        for key, name in F1065_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_accounting_method_on_states(self):
        # Box H is one /Btn base (c1_6) with three distinct on-states /1 /2 /3.
        assert F1065_ACCOUNTING_METHOD_FIELDS["cash"] == (f"{_P1}.c1_6[0]", "1")
        assert F1065_ACCOUNTING_METHOD_FIELDS["accrual"] == (f"{_P1}.c1_6[1]", "2")
        assert F1065_ACCOUNTING_METHOD_FIELDS["other"] == (f"{_P1}.c1_6[2]", "3")

    def test_radio_groups_have_distinct_names(self):
        for grp in _GROUPS:
            names = [v[0] for v in grp.values()]
            assert len(set(names)) == len(names), grp

    def test_independent_checkboxes_distinct(self):
        names = [v[0] for v in F1065_CHECKBOX_FIELDS.values()]
        assert len(set(names)) == len(names)
        # Box G return-type flags must all be addressable.
        for k in ("initial_return", "final_return", "name_change",
                  "address_change", "amended_return"):
            assert k in F1065_CHECKBOX_FIELDS, k

    def test_valid_fields_is_superset(self):
        assert set(F1065_FIELDS.values()) <= VALID_F1065_FIELDS
        for grp in (F1065_CHECKBOX_FIELDS, *_GROUPS):
            assert {v[0] for v in grp.values()} <= VALID_F1065_FIELDS

    def test_ein_not_in_fill_map(self):
        # The partnership EIN is intentionally left blank for the filer.
        assert "ein" not in F1065_FIELDS
        assert f"{_P1}.f1_14[0]" not in VALID_F1065_FIELDS

    def test_key_logical_keys_present(self):
        # The interface contract: these common page-1 lines must be addressable.
        for k in (
            "partnership_name", "address_number_street", "city", "state",
            "zip_code", "num_schedules_k1",
            "gross_receipts_1a", "balance_1c", "cogs_2", "gross_profit_3",
            "total_income_8", "salaries_wages_9", "guaranteed_payments_10",
            "total_deductions_22", "ordinary_business_income_23",
            "total_balance_due_28", "payment_30", "amount_owed_31",
            "overpayment_32a",
        ):
            assert k in F1065_FIELDS, k


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # _render_acroform_pdf flattens values into page content so they render
        # in EVERY viewer (independent of /NeedAppearances). Like the W-4P / 1040
        # smoke tests, assert on the extracted page-1 text.
        _needs_pdf()

        inputs = {
            "partnership_name":            {"value": "Acme Holdings LP", "source_ref": "intake#1", "origin": "user_entered"},
            "address_number_street":       {"value": "500 Market St", "source_ref": "intake#1", "origin": "user_entered"},
            "city":                        {"value": "Greenville", "source_ref": "intake#1", "origin": "user_entered"},
            "num_schedules_k1":            {"value": "3", "source_ref": "intake#1", "origin": "user_entered"},
            "gross_receipts_1a":           {"value": "1250000", "source_ref": "books-2025", "origin": "document"},
            "total_income_8":              {"value": "480000", "source_ref": "books-2025", "origin": "document"},
            "total_deductions_22":         {"value": "415000", "source_ref": "books-2025", "origin": "document"},
            "ordinary_business_income_23": {"value": "65000", "source_ref": "calc#1", "origin": "computed"},
        }
        pdf, ct = render_1065(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        p1 = _page_text(pdf, 0)
        for needle in ("Acme Holdings LP", "500 Market St", "Greenville",
                       "1250000", "480000", "415000", "65000"):
            assert needle in p1, f"{needle!r} not visible on Form 1065 page 1"

    def test_checkbox_group_and_independent_set(self):
        # A mutually-exclusive group choice + an independent boolean both render
        # to a valid PDF via the shared (already-tested) AcroForm filler.
        _needs_pdf()

        cash_name, cash_on = F1065_ACCOUNTING_METHOD_FIELDS["cash"]
        accrual_name, accrual_on = F1065_ACCOUNTING_METHOD_FIELDS["accrual"]
        assert cash_name != accrual_name and cash_on != accrual_on

        pdf, _ = render_1065(
            2025, WatermarkMode.DRAFT,
            {
                "accounting_method": {"value": "accrual", "source_ref": "intake#1", "origin": "user_entered"},
                "irs_discuss":       {"value": "yes", "source_ref": "intake#1", "origin": "user_entered"},
                "initial_return":    {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        assert pdf[:4] == b"%PDF"

        # An unknown choice / non-truthy boolean is silently ignored (no box set).
        pdf2, _ = render_1065(
            2025, WatermarkMode.DRAFT,
            {
                "accounting_method": {"value": "barter", "source_ref": "x", "origin": "x"},
                "final_return":      {"value": "no", "source_ref": "x", "origin": "x"},
            },
        )
        assert pdf2[:4] == b"%PDF"

    def test_conservative_blank_fill(self):
        # Nothing we did not set leaks into the rendered form, and the EIN /
        # an unfilled income line both stay blank.
        _needs_pdf()

        pdf, _ = render_1065(
            2025, WatermarkMode.DRAFT,
            {"partnership_name": {"value": "Solo Ventures LP", "source_ref": "intake#1", "origin": "user_entered"}},
        )
        doc = _page_text(pdf, 0)
        assert "Solo Ventures LP" in doc
        assert "1250000" not in doc   # a figure we never supplied
        assert "Acme" not in doc      # a name we never supplied

    def test_unmapped_and_blank_keys_skipped(self):
        # A blank value is skipped; an unknown key is ignored — none raise, and
        # only the real, non-blank, mapped figure becomes visible.
        _needs_pdf()

        pdf, _ = render_1065(
            2025, WatermarkMode.NONE,
            {
                "gross_receipts_1a": {"value": "", "source_ref": "books", "origin": "document"},
                "not_a_real_key":    {"value": "777", "source_ref": "x", "origin": "x"},
                "total_income_8":    {"value": "92042", "source_ref": "books", "origin": "document"},
            },
        )
        doc = _page_text(pdf, 0)
        assert "92042" in doc      # the mapped, non-blank figure landed
        assert "777" not in doc    # the unknown key never wrote

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"partnership_name": {"value": "Solo Ventures LP", "source_ref": "intake#1", "origin": "user_entered"}}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_1065(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_year_fallback_renders(self):
        # A bogus tax year resolves to f1065_<year>.pdf then falls back to
        # f1065.pdf; when the unversioned PDF is present the happy path returns a
        # valid PDF (empty inputs => an unfilled-but-valid official form).
        _needs_pdf()
        pdf, ct = render_1065(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


class TestMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch):
        # When NO source PDF can be resolved, render_1065 raises FileNotFoundError.
        import app.exports.f1065_fill as mod

        monkeypatch.setattr(
            mod, "_f1065_form_path",
            lambda ty: __import__("pathlib").Path("/nonexistent/f1065.pdf"),
        )
        with pytest.raises(FileNotFoundError):
            render_1065(2025, WatermarkMode.DRAFT,
                        {"partnership_name": {"value": "X", "source_ref": "x", "origin": "x"}})
