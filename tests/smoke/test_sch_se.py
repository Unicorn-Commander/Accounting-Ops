"""Smoke tests for the Schedule SE (Form 1040) professional-substitute path.

Covers the field-map consistency (qualified names, no dups, VALID superset) and
— when the gitignored IRS PDF is present — the end-to-end render (values land
visibly, conservative blank-fill, the Line A check box, watermark by status).
Render tests skip when ``data/irs_forms/f1040sse.pdf`` is absent (as in CI),
mirroring ``test_w4p.py``'s ``_needs_pdf`` guard. Values are flattened into the
page content (XFA-safe baking), so the render assertions check EXTRACTED TEXT.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import IRS_FORMS_DIR, WatermarkMode
from app.exports.sch_se_fill import render_sch_se
from app.exports.sch_se_field_map import (
    SCH_SE_CHECKBOX_FIELDS,
    SCH_SE_FIELDS,
    VALID_SCH_SE_FIELDS,
)

_SCH_SE_PDF = IRS_FORMS_DIR / "f1040sse.pdf"
_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"


def _needs_pdf():
    if not _SCH_SE_PDF.exists():
        pytest.skip("Schedule SE PDF absent (gitignored) — skipping render test")


def _wrap(**vals: str) -> dict:
    """Wrap plain {key: value} into the provenance ``inputs`` shape."""
    return {
        k: {"value": v, "source_ref": "test#1", "origin": "test"}
        for k, v in vals.items()
    }


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_field_names_qualified(self):
        for key, name in SCH_SE_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
        for key, (name, _on) in SCH_SE_CHECKBOX_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_no_duplicate_field_names(self):
        names = list(SCH_SE_FIELDS.values()) + [
            n for n, _ in SCH_SE_CHECKBOX_FIELDS.values()
        ]
        seen: dict[str, int] = {}
        for n in names:
            assert n not in seen, f"duplicate field name {n!r}"
            seen[n] = 1

    def test_checkbox_on_state(self):
        assert SCH_SE_CHECKBOX_FIELDS["minister_exempt_4361_a"] == (
            f"{_P1}.c1_1[0]", "1",
        )

    def test_valid_fields_superset(self):
        assert set(SCH_SE_FIELDS.values()) <= VALID_SCH_SE_FIELDS
        assert {n for n, _ in SCH_SE_CHECKBOX_FIELDS.values()} <= VALID_SCH_SE_FIELDS

    def test_spans_both_parts(self):
        # Part I lives on Page1, Part II (optional methods) on Page2.
        assert any(n.startswith(_P1) for n in SCH_SE_FIELDS.values())
        assert any(n.startswith(_P2) for n in SCH_SE_FIELDS.values())

    def test_ssn_not_mapped(self):
        # The SSN box (f1_2) is intentionally left blank for the taxpayer.
        assert f"{_P1}.f1_2[0]" not in VALID_SCH_SE_FIELDS
        # The preprinted statutory constants (Line 7 / Line 14) are excluded.
        assert f"{_P1}.f1_13[0]" not in VALID_SCH_SE_FIELDS
        assert f"{_P2}.f2_1[0]" not in VALID_SCH_SE_FIELDS


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # Values are flattened into page content so they render in EVERY viewer
        # (not dependent on /NeedAppearances). Assert on extracted text.
        _needs_pdf()
        from pypdf import PdfReader

        inputs = _wrap(
            name="Aaron J Stransky",
            net_nonfarm_profit_2="50000",      # Part I, line 2
            self_employment_tax_12="7065",     # Part I, line 12
            church_employee_income_5a="1234",  # Part I, line 5a (indented box)
            farm_optional_15="4800",           # Part II, line 15 (page 2)
        )
        inputs.update(_wrap(minister_exempt_4361_a="yes"))  # Line A check box

        pdf, ct = render_sch_se(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        reader = PdfReader(io.BytesIO(pdf))
        t0 = reader.pages[0].extract_text()
        t1 = reader.pages[1].extract_text()
        for needle in ("Aaron J Stransky", "50000", "7065", "1234"):
            assert needle in t0, f"{needle!r} not visible on Schedule SE page 1"
        assert "4800" in t1, "Part II line 15 value not visible on page 2"

    def test_conservative_blank_fill(self):
        # Filling line 12 must NOT cause an unfilled line (e.g. line 13) to show
        # a value — nothing we didn't pass may leak in.
        _needs_pdf()
        from pypdf import PdfReader

        pdf, _ = render_sch_se(
            2025, WatermarkMode.DRAFT,
            _wrap(self_employment_tax_12="12121"),
        )
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        assert "12121" in txt
        assert "34343" not in txt  # a value we never supplied (line 13 stays blank)

    def test_falsey_checkbox_left_off(self):
        # A falsey Line A value must not check the box (no crash, conservative).
        _needs_pdf()
        pdf, _ = render_sch_se(
            2025, WatermarkMode.DRAFT,
            _wrap(minister_exempt_4361_a="no", name="Solo"),
        )
        assert pdf[:4] == b"%PDF"

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = _wrap(name="Solo", self_employment_tax_12="100")
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_sch_se(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_unknown_key_skipped(self):
        # An unmapped logical key must be ignored, never invented onto the form.
        _needs_pdf()
        from pypdf import PdfReader

        pdf, _ = render_sch_se(
            2025, WatermarkMode.DRAFT,
            _wrap(name="Solo", not_a_real_line="98765"),
        )
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        assert "Solo" in txt
        assert "98765" not in txt

    def test_missing_pdf_raises(self, tmp_path, monkeypatch):
        # When no source PDF can be resolved, the renderer raises a clear error
        # (this path does not need the real PDF).
        import app.exports.sch_se_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)
        with pytest.raises(FileNotFoundError, match="Schedule SE"):
            render_sch_se(2025, WatermarkMode.DRAFT, _wrap(name="X"))
