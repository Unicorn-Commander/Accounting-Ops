"""Smoke tests for the Schedule E (Form 1040) professional-substitute fill path.

Covers field-map consistency (all names fully qualified, no duplicates, VALID
superset, checkbox/yes-no on-states) and — when the gitignored IRS PDF is
present — that EVERY mapped name is a real terminal field of the right type on
the official PDF, plus the end-to-end render: provenance-bearing inputs land on
the right AcroForm fields across both pages, checkboxes/yes-no answers set,
``name_shown`` stamps both page headers, an unfilled line stays blank
(conservative blank-fill), and an unknown key is ignored.

The render tests skip when ``data/irs_forms/f1040se.pdf`` is absent (as in CI),
mirroring ``tests/smoke/test_w4p.py`` / ``test_f1040.py``'s ``_needs_pdf``.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.sch_e_fill import render_sch_e, _sch_e_form_path
from app.exports.sch_e_field_map import (
    SCHEDULE_E_FIELDS,
    SCHEDULE_E_CHECKBOX_FIELDS,
    SCHEDULE_E_YESNO_FIELDS,
    VALID_SCHEDULE_E_FIELDS,
)

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"
_SCH_E_PDF = _sch_e_form_path(None)


def _needs_pdf():
    if not _SCH_E_PDF.exists():
        pytest.skip("Schedule E PDF absent (gitignored) — skipping render test")


def _all_checkbox_names() -> list[str]:
    return [v[0] for v in SCHEDULE_E_CHECKBOX_FIELDS.values()]


def _all_yesno_names() -> list[str]:
    return [opt[0] for q in SCHEDULE_E_YESNO_FIELDS.values() for opt in q.values()]


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in SCHEDULE_E_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_names_qualified_with_on_states(self):
        for key, (name, on_state) in SCHEDULE_E_CHECKBOX_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
            assert on_state and on_state != "Off"

    def test_all_yesno_names_qualified_with_on_states(self):
        for key, opts in SCHEDULE_E_YESNO_FIELDS.items():
            assert set(opts) == {"yes", "no"}, f"{key} must have yes+no"
            for choice, (name, on_state) in opts.items():
                assert name.startswith("topmostSubform"), f"{key}.{choice} -> {name!r}"
                assert on_state and on_state != "Off"
            # Yes and No must be distinct boxes with distinct on-states.
            assert opts["yes"][0] != opts["no"][0]
            assert opts["yes"][1] != opts["no"][1]

    def test_no_duplicate_field_names(self):
        # No name is reused across text, checkbox, or yes/no maps.
        seen: dict[str, str] = {}
        for key, name in SCHEDULE_E_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key
        for key, (name, _) in SCHEDULE_E_CHECKBOX_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key
        for key, opts in SCHEDULE_E_YESNO_FIELDS.items():
            for choice, (name, _) in opts.items():
                tag = f"{key}.{choice}"
                assert name not in seen, f"dup {name} ({seen.get(name)} & {tag})"
                seen[name] = tag

    def test_valid_fields_is_superset(self):
        assert set(SCHEDULE_E_FIELDS.values()) <= VALID_SCHEDULE_E_FIELDS
        assert set(_all_checkbox_names()) <= VALID_SCHEDULE_E_FIELDS
        assert set(_all_yesno_names()) <= VALID_SCHEDULE_E_FIELDS
        # And VALID is exactly the union (no stray names).
        assert VALID_SCHEDULE_E_FIELDS == frozenset(
            list(SCHEDULE_E_FIELDS.values()) + _all_checkbox_names() + _all_yesno_names()
        )

    def test_key_logical_keys_present(self):
        # The interface contract: these common Schedule E lines/keys must be
        # addressable. Part I rental + Part II passthrough + Part V summary.
        for k in (
            "name_shown", "name_shown_p2",
            "property_a_address", "property_a_type",
            "property_a_fair_rental_days", "property_a_personal_use_days",
            "rents_3_a", "royalties_4_a",
            "advertising_5_a", "insurance_9_a", "mortgage_interest_12_a",
            "repairs_14_a", "taxes_16_a", "depreciation_18_a",
            "total_expenses_20_a", "income_loss_21_a", "deductible_loss_22_a",
            "line_23a_total_rents", "line_24_total_income", "line_25_total_losses",
            "line_26_total_rre_royalty",
            "partnership_a_name", "partnership_a_ein",
            "partnership_a_passive_income_h", "partnership_a_nonpassive_income_k",
            "line_30_partnership_income", "line_32_partnership_total",
            "estate_a_name", "line_37_estate_total",
            "line_39_remic_total", "line_40_farm_rental", "line_41_total_income_loss",
        ):
            assert k in SCHEDULE_E_FIELDS, k
        for k in ("property_a_qjv", "partnership_a_not_at_risk"):
            assert k in SCHEDULE_E_CHECKBOX_FIELDS, k
        for k in ("line_a_1099_required", "line_b_1099_filed", "line_27_prior_year_loss"):
            assert k in SCHEDULE_E_YESNO_FIELDS, k

    def test_three_property_columns_mapped(self):
        # Part I is a three-column table; every per-property line must exist for
        # columns a, b and c (and be distinct names).
        for base in (
            "property_{c}_address", "property_{c}_type",
            "rents_3_{c}", "royalties_4_{c}", "mortgage_interest_12_{c}",
            "deductible_loss_22_{c}",
        ):
            names = {SCHEDULE_E_FIELDS[base.format(c=c)] for c in ("a", "b", "c")}
            assert len(names) == 3, base


# ---------------------------------------------------------------------------
# Every mapped name is a real terminal field of the right type (requires PDF)
# ---------------------------------------------------------------------------

def _real_terminal_fields() -> dict[str, str]:
    """Walk the official PDF's /Fields tree to terminal fields -> /FT string."""
    from pypdf import PdfReader

    reader = PdfReader(str(_SCH_E_PDF))
    root = reader.trailer["/Root"]["/AcroForm"].get_object()
    out: dict[str, str] = {}

    def walk(ref, prefix):
        field = ref.get_object()
        partial = field.get("/T")
        name = (
            f"{prefix}.{partial}" if (prefix and partial is not None)
            else (str(partial) if partial is not None else prefix)
        )
        kids = field.get("/Kids")
        kid_fields = [k for k in (kids or []) if k.get_object().get("/T") is not None]
        ft = field.get("/FT")
        if ft is not None and not kid_fields:
            out[name] = str(ft)
        for k in kid_fields:
            walk(k, name)

    for fr in root["/Fields"]:
        walk(fr, "")
    return out


class TestNamesAreReal:
    def test_every_mapped_name_real(self):
        # Auto-verify the WHOLE map against the distributed PDF: every text name
        # is a real /Tx terminal field and every checkbox/yes-no name is a real
        # /Btn terminal field. This catches any drift or typo in the map.
        _needs_pdf()
        real = _real_terminal_fields()

        for key, name in SCHEDULE_E_FIELDS.items():
            assert name in real, f"text {key} -> {name} not in PDF"
            assert real[name] == "/Tx", f"text {key} -> {name} is {real[name]}"

        for key, (name, _) in SCHEDULE_E_CHECKBOX_FIELDS.items():
            assert name in real, f"checkbox {key} -> {name} not in PDF"
            assert real[name] == "/Btn", f"checkbox {key} -> {name} is {real[name]}"

        for key, opts in SCHEDULE_E_YESNO_FIELDS.items():
            for choice, (name, _) in opts.items():
                assert name in real, f"yesno {key}.{choice} -> {name} not in PDF"
                assert real[name] == "/Btn", f"yesno {key}.{choice} -> {name} is {real[name]}"


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
        # text. Part I (identity + rental) is on page 1; the Part V total and the
        # auto-copied page-2 name are on page 2.
        _needs_pdf()

        inputs = {
            "name_shown":             {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "property_a_address":     {"value": "123 Rental Way", "source_ref": "intake#1", "origin": "user_entered"},
            "rents_3_a":              {"value": "24000", "source_ref": "lease#1", "origin": "document"},
            "mortgage_interest_12_a": {"value": "8200", "source_ref": "1098#1", "origin": "document"},
            "line_26_total_rre_royalty": {"value": "15800", "source_ref": "calc#1", "origin": "computed"},
            "line_41_total_income_loss": {"value": "15800", "source_ref": "calc#1", "origin": "computed"},
        }
        pdf, ct = render_sch_e(2024, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        p1 = _page_text(pdf, 0)
        for needle in ("Aaron J Stransky", "123 Rental Way", "24000", "8200", "15800"):
            assert needle in p1, f"{needle!r} not visible on Schedule E page 1"

        p2 = _page_text(pdf, 1)
        # name_shown is auto-stamped on the page-2 header; line 41 lives on p2.
        for needle in ("Aaron J Stransky", "15800"):
            assert needle in p2, f"{needle!r} not visible on Schedule E page 2"

    def test_name_shown_p2_override(self):
        # An explicit name_shown_p2 overrides the auto-copy on page 2.
        _needs_pdf()
        pdf, _ = render_sch_e(
            2024, WatermarkMode.NONE,
            {
                "name_shown":    {"value": "Pat Owner", "source_ref": "x", "origin": "user_entered"},
                "name_shown_p2": {"value": "Pat & Sam Owner", "source_ref": "x", "origin": "user_entered"},
            },
        )
        assert "Pat Owner" in _page_text(pdf, 0)
        p2 = _page_text(pdf, 1)
        assert "Pat & Sam Owner" in p2

    def test_checkbox_and_yesno_render(self):
        # A truthy single checkbox and a yes/no answer both produce a valid PDF
        # (checkboxes are not extractable text; the shared filler applies /V+/AS).
        _needs_pdf()
        pdf, _ = render_sch_e(
            2024, WatermarkMode.DRAFT,
            {
                "property_a_qjv":        {"value": "true", "source_ref": "x", "origin": "user_entered"},
                "partnership_a_not_at_risk": {"value": "x", "source_ref": "x", "origin": "user_entered"},
                "line_a_1099_required": {"value": "no", "source_ref": "x", "origin": "user_entered"},
                "line_27_prior_year_loss": {"value": "yes", "source_ref": "x", "origin": "user_entered"},
            },
        )
        assert pdf[:4] == b"%PDF"

        # A falsey checkbox value and an unknown yes/no answer are ignored
        # (conservative — no box set), and still render a valid PDF.
        pdf2, _ = render_sch_e(
            2024, WatermarkMode.DRAFT,
            {
                "property_a_qjv":        {"value": "false", "source_ref": "x", "origin": "user_entered"},
                "line_a_1099_required": {"value": "maybe", "source_ref": "x", "origin": "user_entered"},
            },
        )
        assert pdf2[:4] == b"%PDF"

    def test_conservative_blank_fill_and_unknown_key(self):
        # Nothing we did not set leaks in; a blank value and an unknown key are
        # both skipped without error.
        _needs_pdf()
        pdf, _ = render_sch_e(
            2024, WatermarkMode.NONE,
            {
                "rents_3_a":      {"value": "31337", "source_ref": "lease#1", "origin": "document"},
                "royalties_4_a":  {"value": "", "source_ref": "x", "origin": "document"},
                "not_a_real_key": {"value": "999777", "source_ref": "x", "origin": "x"},
            },
        )
        doc = _page_text(pdf, 0) + _page_text(pdf, 1)
        assert "31337" in doc          # the mapped, non-blank figure landed
        assert "999777" not in doc     # unknown key never wrote
        assert "24000" not in doc      # a figure we never supplied

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"name_shown": {"value": "Solo Owner", "source_ref": "x", "origin": "user_entered"}}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_sch_e(2024, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_year_fallback_renders(self):
        # A bogus tax year resolves to f1040se_<year>.pdf then falls back to
        # f1040se.pdf; with the unversioned PDF present, empty inputs yield a
        # valid (unfilled) official form.
        _needs_pdf()
        pdf, ct = render_sch_e(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


class TestMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch):
        # When NO source PDF can be resolved, render_sch_e raises FileNotFoundError.
        import app.exports.sch_e_fill as mod
        from pathlib import Path

        monkeypatch.setattr(mod, "_sch_e_form_path", lambda ty: Path("/nonexistent/f1040se.pdf"))
        with pytest.raises(FileNotFoundError):
            render_sch_e(2024, WatermarkMode.DRAFT,
                         {"name_shown": {"value": "X", "source_ref": "x", "origin": "x"}})
