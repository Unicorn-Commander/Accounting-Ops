"""Smoke tests for the Schedule D (Form 1040) professional-substitute path.

Covers the field-map consistency (qualified names, no dups, VALID superset,
well-formed Yes/No choice map) and — when the gitignored IRS PDF is present —
the end-to-end render (values land as flattened/visible text, conservative
blank-fill, an unfilled line stays blank). Render tests skip when
data/irs_forms/f1040sd.pdf is absent (as in CI), mirroring test_w4p.py.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import IRS_FORMS_DIR, WatermarkMode
from app.exports.sch_d_fill import render_sch_d
from app.exports.sch_d_field_map import (
    SCH_D_CHOICE_FIELDS,
    SCH_D_FIELDS,
    VALID_SCH_D_FIELDS,
)

_SCH_D_PDF = IRS_FORMS_DIR / "f1040sd.pdf"


def _needs_pdf():
    if not _SCH_D_PDF.exists():
        pytest.skip("Schedule D PDF absent (gitignored) — skipping render test")


def _prov(value: str) -> dict:
    """Wrap a value in the provenance-validated inputs record shape."""
    return {"value": value, "source_ref": "Form 8949", "origin": "user_entered"}


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_field_names_qualified(self):
        for key, name in SCH_D_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_no_duplicate_field_names(self):
        seen: dict[str, str] = {}
        for key, name in SCH_D_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_choice_fields_well_formed(self):
        all_choice_names: list[str] = []
        for question, choices in SCH_D_CHOICE_FIELDS.items():
            assert set(choices) == {"yes", "no"}, question
            for choice, pair in choices.items():
                name, on_state = pair
                assert name.startswith("topmostSubform"), f"{question}/{choice}"
                assert on_state in ("1", "2"), f"{question}/{choice} -> {on_state!r}"
                all_choice_names.append(name)
        # Each independent Yes/No widget is a distinct field.
        assert len(all_choice_names) == len(set(all_choice_names))

    def test_valid_fields_superset(self):
        names = set(SCH_D_FIELDS.values())
        names |= {
            pair[0]
            for choices in SCH_D_CHOICE_FIELDS.values()
            for pair in choices.values()
        }
        assert names == set(VALID_SCH_D_FIELDS)

    def test_ssn_not_mapped(self):
        # SSN (f1_2[0]) is intentionally left blank for the taxpayer.
        assert all(not n.endswith(".f1_2[0]") for n in VALID_SCH_D_FIELDS)


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # Values are flattened into page content so they render in EVERY viewer
        # (not dependent on /NeedAppearances). Assert on extracted text.
        _needs_pdf()
        from pypdf import PdfReader

        inputs = {
            "taxpayer_name": _prov("Aaron J Stransky"),
            "line1a_proceeds": _prov("13571"),
            "line1a_cost": _prov("24682"),
            "line7_net_short_term": _prov("35793"),   # page 1
            "line15_net_long_term": _prov("46802"),   # page 1
            "line16_total": _prov("82595"),           # page 2
            "qof_disposed": _prov("no"),              # checkbox
            "line22_qualified_dividends": _prov("yes"),  # checkbox
        }
        pdf, ct = render_sch_d(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        reader = PdfReader(io.BytesIO(pdf))
        txt = "\n".join(p.extract_text() for p in reader.pages)
        for needle in ("Aaron J Stransky", "13571", "24682", "35793", "46802", "82595"):
            assert needle in txt, f"{needle!r} not visible in rendered Schedule D"

    def test_conservative_blank_fill(self):
        # Only the one field we set appears; a value we never wrote does not leak.
        _needs_pdf()
        from pypdf import PdfReader

        inputs = {"line7_net_short_term": _prov("44556")}
        pdf, _ = render_sch_d(2025, WatermarkMode.DRAFT, inputs)
        txt = "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        assert "44556" in txt                # the line we filled is visible
        assert "97531" not in txt            # a sentinel we never set stays blank

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"line16_total": _prov("1000")}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_sch_d(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_empty_inputs_renders_blank_form(self):
        _needs_pdf()
        pdf, ct = render_sch_d(2025, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
