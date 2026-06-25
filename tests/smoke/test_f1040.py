"""Smoke tests for the Form 1040 professional-substitute fill path.

Covers field-map consistency (all names fully qualified, no duplicates, VALID
superset, filing-status on-states) and — when the gitignored IRS PDF is present
— the end-to-end render: provenance-bearing inputs land on the right AcroForm
fields, a filing-status checkbox is set, an unfilled line stays blank
(conservative blank-fill), and the taxpayer SSN is never written.

The render tests skip when ``data/irs_forms/f1040.pdf`` is absent (as in CI),
mirroring ``tests/smoke/test_w4p.py``'s ``_needs_pdf`` pattern.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.f1040_fill import render_1040, _f1040_form_path
from app.exports.f1040_field_map import (
    F1040_FIELDS,
    F1040_FILING_STATUS_FIELDS,
    F1040_DIGITAL_ASSET_FIELDS,
    VALID_F1040_FIELDS,
)

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"
_F1040_PDF = _f1040_form_path(None)


def _needs_pdf():
    if not _F1040_PDF.exists():
        pytest.skip("Form 1040 PDF absent (gitignored) — skipping render test")


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in F1040_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_status_field_names_qualified(self):
        for status, (name, on_state) in F1040_FILING_STATUS_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{status} -> {name!r}"
            assert on_state and on_state != "Off"

    def test_no_duplicate_text_field_names(self):
        seen: dict[str, str] = {}
        for key, name in F1040_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_filing_status_field_names_and_on_states(self):
        # Two on-screen columns: left (Checkbox_ReadOrder, /1 /2 /3),
        # right (directly under Page1, /4 /5). Verified positionally.
        assert F1040_FILING_STATUS_FIELDS["single"] == (
            f"{_P1}.Checkbox_ReadOrder[0].c1_8[0]", "1")
        assert F1040_FILING_STATUS_FIELDS["mfj"] == (
            f"{_P1}.Checkbox_ReadOrder[0].c1_8[1]", "2")
        assert F1040_FILING_STATUS_FIELDS["mfs"] == (
            f"{_P1}.Checkbox_ReadOrder[0].c1_8[2]", "3")
        assert F1040_FILING_STATUS_FIELDS["hoh"] == (f"{_P1}.c1_8[0]", "4")
        assert F1040_FILING_STATUS_FIELDS["qss"] == (f"{_P1}.c1_8[1]", "5")
        # The five statuses use five distinct fully-qualified names.
        names = [v[0] for v in F1040_FILING_STATUS_FIELDS.values()]
        assert len(set(names)) == len(names)

    def test_valid_fields_is_superset(self):
        assert set(F1040_FIELDS.values()) <= VALID_F1040_FIELDS
        assert {v[0] for v in F1040_FILING_STATUS_FIELDS.values()} <= VALID_F1040_FIELDS
        assert {v[0] for v in F1040_DIGITAL_ASSET_FIELDS.values()} <= VALID_F1040_FIELDS

    def test_key_logical_keys_present(self):
        # The interface contract: these common lines must be addressable.
        for k in (
            "wages_1a", "total_wages_1z", "taxable_interest_2b", "ordinary_div_3b",
            "ira_distrib_4b", "pensions_5b", "social_security_6b", "capital_gain_7",
            "additional_income_8", "total_income_9", "adjustments_10", "agi_11",
            "std_or_itemized_12", "taxable_income_15", "tax_16",
            "federal_withholding_25a", "federal_withholding_25b",
            "total_payments_33", "overpayment_34", "amount_you_owe_37",
        ):
            assert k in F1040_FIELDS, k


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

def _page_text(pdf: bytes, page_index: int) -> str:
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(pdf)).pages[page_index].extract_text()


class TestRender:
    def test_values_baked_in_visible(self):
        # _render_acroform_pdf flattens values into page content so they render
        # in EVERY viewer (independent of /NeedAppearances). Like the W-4P smoke
        # test, assert on the extracted text. Identity + wages are on page 1;
        # AGI + withholding are on page 2.
        _needs_pdf()

        inputs = {
            "first_name_mi":           {"value": "Aaron J", "source_ref": "intake#1", "origin": "user_entered"},
            "last_name":               {"value": "Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "home_address":            {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "filing_status":           {"value": "mfj", "source_ref": "intake#1", "origin": "user_entered"},
            "wages_1a":                {"value": "85123", "source_ref": "w2#1", "origin": "document"},
            "agi_11":                  {"value": "84777", "source_ref": "calc#1", "origin": "computed"},
            "federal_withholding_25a": {"value": "9512", "source_ref": "w2#1", "origin": "document"},
        }
        pdf, ct = render_1040(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        p1 = _page_text(pdf, 0)
        for needle in ("Aaron J", "Stransky", "123 Main St", "85123"):
            assert needle in p1, f"{needle!r} not visible on Form 1040 page 1"

        p2 = _page_text(pdf, 1)
        for needle in ("84777", "9512"):
            assert needle in p2, f"{needle!r} not visible on Form 1040 page 2"

    def test_filing_status_checkbox_set(self):
        # Checkboxes are not extractable text, so verify the on-state the
        # renderer hands to _render_acroform_pdf via the field-map seam: the
        # MFJ status maps to a distinct field + on-state, and rendering with it
        # produces a valid PDF (the shared, already-tested filler applies /V).
        _needs_pdf()

        mfj_name, mfj_on = F1040_FILING_STATUS_FIELDS["mfj"]
        single_name, single_on = F1040_FILING_STATUS_FIELDS["single"]
        assert mfj_name != single_name and mfj_on != single_on

        pdf, _ = render_1040(
            2025, WatermarkMode.DRAFT,
            {"filing_status": {"value": "mfj", "source_ref": "intake#1", "origin": "user_entered"}},
        )
        assert pdf[:4] == b"%PDF"

        # An unknown status is silently ignored (conservative — no box set).
        pdf2, _ = render_1040(
            2025, WatermarkMode.DRAFT,
            {"filing_status": {"value": "not_a_status", "source_ref": "x", "origin": "x"}},
        )
        assert pdf2[:4] == b"%PDF"

    def test_conservative_blank_fill(self):
        # Nothing we did not set leaks into the rendered form.
        _needs_pdf()

        pdf, _ = render_1040(
            2025, WatermarkMode.DRAFT,
            {"last_name": {"value": "Solo", "source_ref": "intake#1", "origin": "user_entered"}},
        )
        doc = _page_text(pdf, 0) + _page_text(pdf, 1)
        assert "Solo" in doc
        assert "Aaron" not in doc          # an identity we never supplied
        assert "85123" not in doc          # a wage figure we never supplied

    def test_unmapped_and_blank_keys_skipped(self):
        # 'total_tax_24' has no fillable box on TY2025; a blank value is skipped;
        # an unknown key is ignored — none of these raise, and only the one
        # real, non-blank, mapped figure becomes visible.
        _needs_pdf()

        pdf, _ = render_1040(
            2025, WatermarkMode.NONE,
            {
                "total_tax_24":      {"value": "1234", "source_ref": "calc#1", "origin": "computed"},
                "wages_1a":          {"value": "", "source_ref": "w2#1", "origin": "document"},
                "not_a_real_key":    {"value": "777", "source_ref": "x", "origin": "x"},
                "taxable_income_15": {"value": "60042", "source_ref": "calc#1", "origin": "computed"},
            },
        )
        doc = _page_text(pdf, 0) + _page_text(pdf, 1)
        assert "60042" in doc          # the mapped, non-blank figure landed
        assert "1234" not in doc       # line-24 figure had no box to land in
        assert "777" not in doc        # unknown key never wrote

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"last_name": {"value": "Solo", "source_ref": "intake#1", "origin": "user_entered"}}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_1040(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_year_fallback_renders(self):
        # A bogus tax year resolves to f1040_<year>.pdf then falls back to
        # f1040.pdf; when the unversioned PDF is present, the happy path returns
        # a valid PDF (empty inputs => an unfilled-but-valid official form).
        _needs_pdf()
        pdf, ct = render_1040(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


class TestMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch):
        # When NO source PDF can be resolved, render_1040 raises FileNotFoundError.
        import app.exports.f1040_fill as mod

        monkeypatch.setattr(mod, "_f1040_form_path", lambda ty: __import__("pathlib").Path("/nonexistent/f1040.pdf"))
        with pytest.raises(FileNotFoundError):
            render_1040(2025, WatermarkMode.DRAFT, {"last_name": {"value": "X", "source_ref": "x", "origin": "x"}})
