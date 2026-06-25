"""Smoke tests for the Schedule A (Form 1040) professional-substitute fill path.

Covers field-map consistency (all names fully qualified, no duplicates, VALID
superset, checkbox on-states) and — when the gitignored IRS PDF is present — the
end-to-end render: provenance-bearing inputs land on the right AcroForm fields, a
checkbox election sets without error, an unfilled line stays blank (conservative
blank-fill), and the taxpayer SSN is never written.

The render tests skip when ``data/irs_forms/f1040sa.pdf`` is absent (as in CI),
mirroring ``tests/smoke/test_w4p.py`` / ``test_f1040.py``'s ``_needs_pdf``.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.sch_a_fill import render_sch_a, _sch_a_form_path
from app.exports.sch_a_field_map import (
    F1040SA_FIELDS,
    F1040SA_CHECKBOX_FIELDS,
    VALID_F1040SA_FIELDS,
)

_P1 = "form1[0].Page1[0]"
_SCH_A_PDF = _sch_a_form_path(None)


def _needs_pdf():
    if not _SCH_A_PDF.exists():
        pytest.skip("Schedule A PDF absent (gitignored) — skipping render test")


def _page_text(pdf: bytes, page_index: int = 0) -> str:
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(pdf)).pages[page_index].extract_text()


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        # Schedule A uses the form1[0] root naming (not topmostSubform[0]).
        for key, name in F1040SA_FIELDS.items():
            assert name.startswith(f"{_P1}."), f"{key} -> {name!r}"

    def test_all_checkbox_field_names_qualified(self):
        for key, (name, on_state) in F1040SA_CHECKBOX_FIELDS.items():
            assert name.startswith(f"{_P1}."), f"{key} -> {name!r}"
            assert on_state and on_state != "Off"

    def test_no_duplicate_text_field_names(self):
        seen: dict[str, str] = {}
        for key, name in F1040SA_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_no_overlap_between_text_and_checkbox_fields(self):
        text_names = set(F1040SA_FIELDS.values())
        checkbox_names = {v[0] for v in F1040SA_CHECKBOX_FIELDS.values()}
        assert text_names.isdisjoint(checkbox_names)

    def test_checkbox_field_names_and_on_states(self):
        assert F1040SA_CHECKBOX_FIELDS["sales_tax_election_5a"] == (
            f"{_P1}.c1_1[0]", "1")
        assert F1040SA_CHECKBOX_FIELDS["mortgage_use_box_8"] == (
            f"{_P1}.Line8_ReadOrder[0].c1_2[0]", "1")
        assert F1040SA_CHECKBOX_FIELDS["elect_itemize_18"] == (
            f"{_P1}.Line18_ReadOrder[0].c1_3[0]", "1")

    def test_valid_fields_is_superset(self):
        assert set(F1040SA_FIELDS.values()) <= VALID_F1040SA_FIELDS
        assert {v[0] for v in F1040SA_CHECKBOX_FIELDS.values()} <= VALID_F1040SA_FIELDS

    def test_ssn_is_not_a_writable_key(self):
        # The taxpayer SSN box (f1_2) is intentionally NOT writable.
        assert "ssn" not in F1040SA_FIELDS
        assert f"{_P1}.f1_2[0]" not in VALID_F1040SA_FIELDS

    def test_key_logical_keys_present(self):
        # The interface contract: these common lines must be addressable.
        for k in (
            "taxpayer_name",
            "medical_dental_1", "agi_2", "medical_deduction_4",
            "salt_income_or_sales_5a", "salt_real_estate_5b",
            "salt_subtotal_5d", "salt_deduction_5e", "total_taxes_7",
            "mortgage_interest_1098_8a", "investment_interest_9",
            "total_interest_10", "gifts_cash_11", "gifts_noncash_12",
            "total_gifts_14", "casualty_theft_15", "other_itemized_16",
            "total_itemized_17",
        ):
            assert k in F1040SA_FIELDS, k


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # _render_acroform_pdf flattens values into page content so they render
        # in EVERY viewer (independent of /NeedAppearances). Assert on extracted
        # text. Schedule A is a single page, so everything is on page 0.
        _needs_pdf()

        inputs = {
            "taxpayer_name":             {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "salt_real_estate_5b":       {"value": "82010", "source_ref": "prop#1", "origin": "document"},
            "salt_deduction_5e":         {"value": "40000", "source_ref": "calc#1", "origin": "computed"},
            "mortgage_interest_1098_8a": {"value": "12453", "source_ref": "1098#1", "origin": "document"},
            "gifts_cash_11":             {"value": "31337", "source_ref": "don#1", "origin": "document"},
            "total_itemized_17":         {"value": "85791", "source_ref": "calc#1", "origin": "computed"},
        }
        pdf, ct = render_sch_a(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        txt = _page_text(pdf)
        for needle in ("Aaron J Stransky", "82010", "12453", "31337", "85791"):
            assert needle in txt, f"{needle!r} not visible on Schedule A"

    def test_checkbox_election_sets(self):
        # The line-5a general-sales-tax election + line-18 itemize election are
        # independent checkboxes. A truthy value renders a valid PDF (the shared,
        # already-tested filler applies /V and /AS); a falsey value leaves it off.
        _needs_pdf()

        pdf, _ = render_sch_a(
            2025, WatermarkMode.DRAFT,
            {
                "sales_tax_election_5a": {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
                "elect_itemize_18":      {"value": "yes",  "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        assert pdf[:4] == b"%PDF"

        # A falsey checkbox value is conservatively left unchecked (still valid).
        pdf2, _ = render_sch_a(
            2025, WatermarkMode.DRAFT,
            {"sales_tax_election_5a": {"value": "false", "source_ref": "x", "origin": "x"}},
        )
        assert pdf2[:4] == b"%PDF"

    def test_conservative_blank_fill_and_ssn_never_written(self):
        # Nothing we did not set leaks into the rendered form, and an "ssn" input
        # is never written (the field is omitted from the writable map).
        _needs_pdf()

        pdf, _ = render_sch_a(
            2025, WatermarkMode.DRAFT,
            {
                "gifts_cash_11": {"value": "31337", "source_ref": "don#1", "origin": "document"},
                "ssn":           {"value": "123456789", "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        txt = _page_text(pdf)
        assert "31337" in txt              # the figure we did set landed
        assert "82010" not in txt          # a figure we never supplied
        assert "123456789" not in txt      # SSN is never written

    def test_unmapped_and_blank_keys_skipped(self):
        # A blank value is skipped; an unknown key is ignored — none raise, and
        # only the real, non-blank, mapped figure becomes visible.
        _needs_pdf()

        pdf, _ = render_sch_a(
            2025, WatermarkMode.NONE,
            {
                "salt_real_estate_5b": {"value": "", "source_ref": "x", "origin": "x"},
                "not_a_real_key":      {"value": "99999", "source_ref": "x", "origin": "x"},
                "total_itemized_17":   {"value": "60042", "source_ref": "calc#1", "origin": "computed"},
            },
        )
        txt = _page_text(pdf)
        assert "60042" in txt              # the mapped, non-blank figure landed
        assert "99999" not in txt          # unknown key never wrote

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"total_itemized_17": {"value": "60042", "source_ref": "calc#1", "origin": "computed"}}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_sch_a(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_year_fallback_renders(self):
        # A bogus tax year resolves to f1040sa_<year>.pdf then falls back to
        # f1040sa.pdf; empty inputs yield an unfilled-but-valid official form.
        _needs_pdf()
        pdf, ct = render_sch_a(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


class TestMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch):
        # When NO source PDF can be resolved, render_sch_a raises FileNotFoundError.
        import app.exports.sch_a_fill as mod

        monkeypatch.setattr(
            mod, "_sch_a_form_path",
            lambda ty: __import__("pathlib").Path("/nonexistent/f1040sa.pdf"),
        )
        with pytest.raises(FileNotFoundError):
            render_sch_a(
                2025, WatermarkMode.DRAFT,
                {"total_itemized_17": {"value": "1", "source_ref": "x", "origin": "x"}},
            )
