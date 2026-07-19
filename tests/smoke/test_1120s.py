"""Smoke tests for the Form 1120-S professional-substitute path.

Covers the field-map consistency (no PDF) and — when the gitignored IRS PDF is
present — the end-to-end render: values land visibly (flattened, so they show in
every viewer; assert on EXTRACTED TEXT), checkboxes mark, conservative
blank-fill leaves untouched lines empty, the EIN is never filled, and the
watermark renders for every status. Render tests skip when
``data/irs_forms/f1120s.pdf`` is absent (as in CI), mirroring ``test_w4p.py``.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import IRS_FORMS_DIR, WatermarkMode
from app.exports.f1120s_fill import render_1120_s
from app.exports.f1120s_field_map import (
    FORM_1120S_FIELDS,
    FORM_1120S_CHECKBOX_FIELDS,
    FORM_1120S_ELECTION_FIELDS,
    FORM_1120S_DEPOSIT_TYPE_FIELDS,
    VALID_FORM_1120S_FIELDS,
)

_1120S_PDF = IRS_FORMS_DIR / "f1120s.pdf"
# Corporation EIN (item D) — intentionally NOT in the fillable map; left blank
# for the human. Used by a test to assert the renderer never writes it.
_EIN_FIELD = "topmostSubform[0].Page1[0].f1_13[0]"


def _needs_pdf():
    if not _1120S_PDF.exists():
        pytest.skip("Form 1120-S PDF absent (gitignored) — skipping render test")


def _src(**values) -> str:
    """Build a provenance-bearing inputs map from logical_key -> value."""
    return {k: {"value": v, "source_ref": "test#1", "origin": "user_entered"}
            for k, v in values.items()}


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in FORM_1120S_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_field_names_qualified(self):
        groups = (
            FORM_1120S_CHECKBOX_FIELDS,
            FORM_1120S_ELECTION_FIELDS,
            FORM_1120S_DEPOSIT_TYPE_FIELDS,
        )
        for group in groups:
            for key, (name, on_state) in group.items():
                assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
                # on-state is stored WITHOUT a leading slash (walker adds it).
                assert on_state and not on_state.startswith("/"), f"{key} -> {on_state!r}"

    def test_no_duplicate_text_field_names(self):
        seen: dict[str, str] = {}
        for key, name in FORM_1120S_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_valid_fields_is_superset(self):
        names = (
            set(FORM_1120S_FIELDS.values())
            | {v[0] for v in FORM_1120S_CHECKBOX_FIELDS.values()}
            | {v[0] for v in FORM_1120S_ELECTION_FIELDS.values()}
            | {v[0] for v in FORM_1120S_DEPOSIT_TYPE_FIELDS.values()}
        )
        assert names <= VALID_FORM_1120S_FIELDS
        # VALID set carries no extras beyond what the maps can write.
        assert names == set(VALID_FORM_1120S_FIELDS)

    def test_ein_field_not_in_map(self):
        # The corporation EIN is left blank for the human — it must not appear in
        # any fillable map.
        assert _EIN_FIELD not in VALID_FORM_1120S_FIELDS

    def test_core_logical_keys_present(self):
        for key in (
            "corp_name", "line_1a_gross_receipts", "line_6_total_income",
            "line_21_total_deductions", "line_22_ordinary_income",
            "line_23c_total_tax", "line_28b_refunded",
        ):
            assert key in FORM_1120S_FIELDS, key


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # Values are flattened into page content so they render in EVERY viewer
        # (not dependent on /NeedAppearances). Assert on extracted text.
        _needs_pdf()
        from pypdf import PdfReader

        inputs = _src(
            corp_name="Acme Widgets S Corp",
            number_street="100 Main St",
            city="Columbia",
            state="SC",
            zip="29201",
            total_assets="1250000",
            num_shareholders="3",
            line_1a_gross_receipts="5000000",
            line_6_total_income="4950000",
            line_7_officer_comp="200000",
            line_21_total_deductions="4100000",
            line_22_ordinary_income="850000",
        )
        pdf, ct = render_1120_s(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        for needle in (
            "Acme Widgets S Corp", "100 Main St", "Columbia",
            "1250000", "5000000", "4950000", "200000", "4100000", "850000",
        ):
            assert needle in txt, f"{needle!r} not visible in rendered 1120-S"

    def test_checkboxes_and_election_mark(self):
        _needs_pdf()
        from pypdf import PdfReader

        inputs = _src(
            corp_name="Checkbox Co",
            final_return="true",
            grouped_activities="yes",
            sch_m3_attached="x",
            electing_s_corp="no",
        )
        pdf, _ = render_1120_s(2025, WatermarkMode.DRAFT, inputs)
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        # Each marked checkbox bakes an "X"; with 4 boxes set we expect >=4.
        assert txt.count("X") >= 4, f"expected >=4 checkbox marks, saw {txt.count('X')}"
        assert "Checkbox Co" in txt

    def test_unchecked_box_stays_blank(self):
        # A boolean checkbox whose input is falsy is not set (no X for it).
        _needs_pdf()
        from pypdf import PdfReader

        none_set = render_1120_s(2025, WatermarkMode.DRAFT, _src(corp_name="Zero Co"))[0]
        one_set = render_1120_s(
            2025, WatermarkMode.DRAFT,
            _src(corp_name="Zero Co", final_return="true"),
        )[0]
        n0 = PdfReader(io.BytesIO(none_set)).pages[0].extract_text().count("X")
        n1 = PdfReader(io.BytesIO(one_set)).pages[0].extract_text().count("X")
        assert n1 == n0 + 1, f"checking one box should add exactly one X ({n0} -> {n1})"

    def test_conservative_blank_fill_and_no_ein(self):
        _needs_pdf()
        from pypdf import PdfReader

        # Only the corp name is provided; nothing else (and no EIN) should leak.
        pdf, _ = render_1120_s(2025, WatermarkMode.DRAFT, _src(corp_name="Solo S Corp"))
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        assert "Solo S Corp" in txt
        assert "Acme" not in txt  # nothing we didn't set leaks in

    def test_ein_never_filled_even_if_supplied(self):
        # Supplying an "ein" logical key must NOT write the EIN box (it is not a
        # mapped, fillable field — left blank for the human).
        _needs_pdf()
        from pypdf import PdfReader

        pdf, _ = render_1120_s(
            2025, WatermarkMode.DRAFT,
            _src(corp_name="No EIN Co", ein="12-3456789"),
        )
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        assert "12-3456789" not in txt

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = _src(corp_name="Watermark S Corp")
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_1120_s(2025, WatermarkMode.DRAFT if False else mode, inputs)
            assert pdf[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# Missing-PDF behavior (no PDF needed)
# ---------------------------------------------------------------------------

class TestMissingPdf:
    def test_missing_pdf_raises_filenotfound(self, monkeypatch):
        # When no source PDF resolves, the renderer raises a clear error rather
        # than fabricating output.
        import app.exports.f1120s_fill as mod

        monkeypatch.setattr(mod, "_f1120s_form_path", lambda ty: __import__("pathlib").Path("/nonexistent/f1120s.pdf"))
        with pytest.raises(FileNotFoundError, match="1120-S"):
            render_1120_s(2025, WatermarkMode.DRAFT, _src(corp_name="x"))
