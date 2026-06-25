"""Smoke tests for the Form 4868 (Automatic Extension) professional-substitute path.

Covers field-map consistency (all names fully qualified, no duplicates, VALID
superset, checkbox on-states) and — when the gitignored IRS PDF is present — the
end-to-end render: provenance-bearing inputs land on the right AcroForm fields,
the checkboxes/unknown/blank keys behave, an unfilled line stays blank
(conservative blank-fill), and the taxpayer SSN is never written.

The render tests skip when ``data/irs_forms/f4868.pdf`` is absent (as in CI),
mirroring ``tests/smoke/test_w4p.py``'s ``_needs_pdf`` pattern.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.f4868_fill import render_4868, _f4868_form_path
from app.exports.f4868_field_map import (
    F4868_FIELDS,
    F4868_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
    VALID_F4868_FIELDS,
)

_P1 = "topmostSubform[0].Page1[0]"
_F4868_PDF = _f4868_form_path(None)


def _needs_pdf():
    if not _F4868_PDF.exists():
        pytest.skip("Form 4868 PDF absent (gitignored) — skipping render test")


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in F4868_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_names_qualified(self):
        for key, (name, on_state) in F4868_CHECKBOX_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
            assert on_state and on_state != "Off"

    def test_no_duplicate_field_names(self):
        seen: dict[str, str] = {}
        for key, name in F4868_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key
        # Checkbox names must also be distinct from each other and the text map.
        cb_names = [v[0] for v in F4868_CHECKBOX_FIELDS.values()]
        assert len(set(cb_names)) == len(cb_names)
        assert not (set(cb_names) & set(F4868_FIELDS.values()))

    def test_checkbox_field_names_and_on_states(self):
        assert F4868_CHECKBOX_FIELDS["out_of_country_8"] == (f"{_P1}.c1_1[0]", "1")
        assert F4868_CHECKBOX_FIELDS["form_1040nr_9"] == (f"{_P1}.c1_2[0]", "1")

    def test_valid_fields_is_superset(self):
        assert set(F4868_FIELDS.values()) <= VALID_F4868_FIELDS
        assert {v[0] for v in F4868_CHECKBOX_FIELDS.values()} <= VALID_F4868_FIELDS

    def test_key_logical_keys_present(self):
        # The interface contract: identity + the four Part II dollar lines must
        # be addressable.
        for k in (
            "name", "address", "city_town", "state", "zip_code",
            "tax_liability_4", "payments_5", "balance_due_6", "amount_paying_7",
        ):
            assert k in F4868_FIELDS, k

    def test_ssn_keys_mapped_but_taxpayer_completed(self):
        # SSN lines exist in the positional map but are flagged taxpayer-completed
        # so the renderer never auto-fills them.
        assert "your_ssn" in F4868_FIELDS and "spouse_ssn" in F4868_FIELDS
        assert TAXPAYER_COMPLETED_KEYS == frozenset({"your_ssn", "spouse_ssn"})


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

def _page_text(pdf: bytes, page_index: int) -> str:
    from pypdf import PdfReader

    return PdfReader(io.BytesIO(pdf)).pages[page_index].extract_text()


def _all_text(pdf: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


class TestRender:
    def test_values_baked_in_visible(self):
        # _render_acroform_pdf flattens values into page content so they render
        # in EVERY viewer (independent of /NeedAppearances). The voucher (and all
        # its fields) is on page 1; assert on the extracted text.
        _needs_pdf()

        inputs = {
            "name":            {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "address":         {"value": "123 Main St", "source_ref": "intake#1", "origin": "user_entered"},
            "city_town":       {"value": "Columbia", "source_ref": "intake#1", "origin": "user_entered"},
            "state":           {"value": "SC", "source_ref": "intake#1", "origin": "user_entered"},
            "zip_code":        {"value": "29201", "source_ref": "intake#1", "origin": "user_entered"},
            "tax_liability_4": {"value": "12345", "source_ref": "calc#1", "origin": "computed"},
            "amount_paying_7": {"value": "3000", "source_ref": "intake#1", "origin": "user_entered"},
        }
        pdf, ct = render_4868(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        p1 = _page_text(pdf, 0)
        for needle in ("Aaron J Stransky", "123 Main St", "Columbia", "29201", "12345", "3000"):
            assert needle in p1, f"{needle!r} not visible on Form 4868 page 1"

    def test_ssn_never_filled(self):
        # The taxpayer's SSN/spouse SSN are identity items left for the taxpayer.
        _needs_pdf()

        pdf, _ = render_4868(
            2025, WatermarkMode.DRAFT,
            {
                "name":       {"value": "Jane Filer", "source_ref": "intake#1", "origin": "user_entered"},
                "your_ssn":   {"value": "123456789", "source_ref": "intake#1", "origin": "user_entered"},
                "spouse_ssn": {"value": "987654321", "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        doc = _all_text(pdf)
        assert "Jane Filer" in doc
        assert "123456789" not in doc   # taxpayer SSN is never auto-filled
        assert "987654321" not in doc

    def test_conservative_blank_fill(self):
        # Nothing we did not set leaks into the rendered form.
        _needs_pdf()

        pdf, _ = render_4868(
            2025, WatermarkMode.DRAFT,
            {"name": {"value": "Solo Filer", "source_ref": "intake#1", "origin": "user_entered"}},
        )
        doc = _all_text(pdf)
        assert "Solo Filer" in doc
        assert "12345" not in doc   # a figure we never supplied

    def test_checkbox_truthy_and_unknown_keys(self):
        # A truthy checkbox value sets the box; a falsey one is left off; an
        # unknown key is ignored; a blank value is skipped. None of these raise,
        # and only the real, non-blank, mapped figure becomes visible.
        _needs_pdf()

        pdf, _ = render_4868(
            2025, WatermarkMode.NONE,
            {
                "out_of_country_8": {"value": "true", "source_ref": "intake#1", "origin": "user_entered"},
                "form_1040nr_9":    {"value": "false", "source_ref": "intake#1", "origin": "user_entered"},
                "tax_liability_4":  {"value": "", "source_ref": "calc#1", "origin": "computed"},
                "not_a_real_key":   {"value": "777", "source_ref": "x", "origin": "x"},
                "amount_paying_7":  {"value": "4200", "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        doc = _all_text(pdf)
        assert "4200" in doc       # the mapped, non-blank figure landed
        assert "777" not in doc    # unknown key never wrote

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"name": {"value": "Solo Filer", "source_ref": "intake#1", "origin": "user_entered"}}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_4868(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_year_fallback_renders(self):
        # A bogus tax year resolves to f4868_<year>.pdf then falls back to
        # f4868.pdf; with the unversioned PDF present, empty inputs return a
        # valid (unfilled) official form.
        _needs_pdf()
        pdf, ct = render_4868(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


class TestMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch):
        # When NO source PDF can be resolved, render_4868 raises FileNotFoundError.
        import app.exports.f4868_fill as mod

        monkeypatch.setattr(
            mod, "_f4868_form_path",
            lambda ty: __import__("pathlib").Path("/nonexistent/f4868.pdf"),
        )
        with pytest.raises(FileNotFoundError):
            render_4868(
                2025, WatermarkMode.DRAFT,
                {"name": {"value": "X", "source_ref": "x", "origin": "x"}},
            )
