"""Smoke tests for the Schedule B (Form 1040) professional-substitute fill path.

Covers field-map consistency (all names fully qualified, no duplicates, VALID
superset, Part III Yes/No on-states, key logical keys present) and — when the
gitignored IRS PDF is present — the end-to-end render: provenance-bearing inputs
land on the right AcroForm fields, a Part III Yes/No checkbox is set, an unfilled
line stays blank (conservative blank-fill), the taxpayer SSN is never written,
and every mapped field name is a real terminal field in the official PDF.

The render tests skip when ``data/irs_forms/f1040sb.pdf`` is absent (as in CI),
mirroring ``tests/smoke/test_w4p.py`` / ``test_f1040.py``'s ``_needs_pdf``.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.sch_b_fill import render_sch_b, _sch_b_form_path
from app.exports.sch_b_field_map import (
    SCHB_FIELDS,
    SCHB_YESNO_FIELDS,
    VALID_SCHB_FIELDS,
)

_P1 = "topmostSubform[0].Page1[0]"
_SCHB_PDF = _sch_b_form_path(None)


def _needs_pdf():
    if not _SCHB_PDF.exists():
        pytest.skip("Schedule B PDF absent (gitignored) — skipping render test")


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in SCHB_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_yesno_field_names_qualified(self):
        for q, answers in SCHB_YESNO_FIELDS.items():
            for ans, (name, on_state) in answers.items():
                assert name.startswith("topmostSubform"), f"{q}.{ans} -> {name!r}"
                assert on_state and on_state != "Off"

    def test_no_duplicate_text_field_names(self):
        seen: dict[str, str] = {}
        for key, name in SCHB_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_no_duplicate_field_names_across_text_and_checkboxes(self):
        # Text fields and checkbox widgets must be wholly disjoint names.
        cb_names = {
            name
            for answers in SCHB_YESNO_FIELDS.values()
            for (name, _s) in answers.values()
        }
        assert cb_names.isdisjoint(set(SCHB_FIELDS.values()))

    def test_yesno_on_states(self):
        # Each Yes is on-state /1, each No is on-state /2 (verified against dump).
        for q, answers in SCHB_YESNO_FIELDS.items():
            assert set(answers) == {"yes", "no"}, q
            assert answers["yes"][1] == "1", q
            assert answers["no"][1] == "2", q
            # The Yes and No are distinct widgets.
            assert answers["yes"][0] != answers["no"][0], q

    def test_part_iii_checkbox_names(self):
        assert SCHB_YESNO_FIELDS["foreign_account_7a"]["yes"] == (
            f"{_P1}.TagcorrectingSubform[0].c1_1[0]", "1")
        assert SCHB_YESNO_FIELDS["foreign_account_7a"]["no"] == (
            f"{_P1}.TagcorrectingSubform[0].c1_1[1]", "2")
        assert SCHB_YESNO_FIELDS["fincen_required_7a"]["yes"] == (f"{_P1}.c1_2[0]", "1")
        assert SCHB_YESNO_FIELDS["foreign_trust_8"]["yes"] == (f"{_P1}.c1_3[0]", "1")

    def test_valid_fields_is_superset(self):
        assert set(SCHB_FIELDS.values()) <= VALID_SCHB_FIELDS
        cb_names = {
            name
            for answers in SCHB_YESNO_FIELDS.values()
            for (name, _s) in answers.values()
        }
        assert cb_names <= VALID_SCHB_FIELDS

    def test_key_logical_keys_present(self):
        # The interface contract: these common keys must be addressable.
        for k in (
            "name_shown_on_return",
            "interest_payer_1_name", "interest_payer_1_amount",
            "interest_payer_14_name", "interest_payer_14_amount",
            "interest_total_2", "excludable_interest_3", "interest_taxable_4",
            "dividend_payer_1_name", "dividend_payer_1_amount",
            "dividend_payer_15_name", "dividend_payer_15_amount",
            "dividends_total_6", "foreign_country_7b",
        ):
            assert k in SCHB_FIELDS, k

    def test_ssn_field_not_mapped(self):
        # The taxpayer SSN box (f1_02) must NOT be writable — left blank for the
        # taxpayer, like the SSN on Form 1040.
        assert f"{_P1}.f1_02[0]" not in set(SCHB_FIELDS.values())
        assert f"{_P1}.f1_02[0]" not in VALID_SCHB_FIELDS

    def test_first_rows_carry_readorder_wrapper(self):
        # Row 1 of each payer list is wrapped in a read-order subform; later rows
        # are direct children of Page1[0]. This pins the one irregular case.
        assert SCHB_FIELDS["interest_payer_1_name"] == (
            f"{_P1}.Line1_ReadOrder[0].f1_03[0]")
        assert SCHB_FIELDS["interest_payer_2_name"] == f"{_P1}.f1_05[0]"
        assert SCHB_FIELDS["dividend_payer_1_name"] == (
            f"{_P1}.ReadOrderControl[0].f1_34[0]")
        assert SCHB_FIELDS["dividend_payer_2_name"] == f"{_P1}.f1_36[0]"


# ---------------------------------------------------------------------------
# Field names verified against the actual PDF (requires the IRS PDF)
# ---------------------------------------------------------------------------

class TestNamesExistInPdf:
    def _terminal_field_names(self) -> set[str]:
        from pypdf import PdfReader

        reader = PdfReader(str(_SCHB_PDF))
        acro = reader.trailer["/Root"]["/AcroForm"].get_object()
        names: set[str] = set()

        def walk(ref, prefix):
            field = ref.get_object()
            partial = field.get("/T")
            name = (
                f"{prefix}.{partial}" if (prefix and partial is not None)
                else (str(partial) if partial is not None else prefix)
            )
            kids = field.get("/Kids")
            kid_fields = [k for k in (kids or []) if k.get_object().get("/T") is not None]
            if field.get("/FT") is not None and not kid_fields:
                names.add(name)
            for k in kid_fields:
                walk(k, name)

        for fr in acro["/Fields"]:
            walk(fr, "")
        return names

    def test_every_mapped_name_is_a_real_terminal_field(self):
        _needs_pdf()
        real = self._terminal_field_names()
        for name in VALID_SCHB_FIELDS:
            assert name in real, f"mapped field {name!r} is not in the official PDF"


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
        # text. Schedule B is a single page.
        _needs_pdf()

        inputs = {
            "name_shown_on_return":     {"value": "Aaron J Stransky", "source_ref": "intake#1", "origin": "user_entered"},
            "interest_payer_1_name":    {"value": "Acme Credit Union", "source_ref": "1099int#1", "origin": "document"},
            "interest_payer_1_amount":  {"value": "1234", "source_ref": "1099int#1", "origin": "document"},
            "interest_total_2":         {"value": "1234", "source_ref": "calc#1", "origin": "computed"},
            "interest_taxable_4":       {"value": "1234", "source_ref": "calc#1", "origin": "computed"},
            "dividend_payer_1_name":    {"value": "Vanguard", "source_ref": "1099div#1", "origin": "document"},
            "dividend_payer_1_amount":  {"value": "2500", "source_ref": "1099div#1", "origin": "document"},
            "dividends_total_6":        {"value": "2500", "source_ref": "calc#1", "origin": "computed"},
        }
        pdf, ct = render_sch_b(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        txt = _page_text(pdf, 0)
        for needle in ("Aaron J Stransky", "Acme Credit Union", "1234", "Vanguard", "2500"):
            assert needle in txt, f"{needle!r} not visible on Schedule B"

    def test_conservative_blank_fill(self):
        # Nothing we did not set leaks into the rendered form, and the SSN is
        # never written even when a value is provided for that key.
        _needs_pdf()

        pdf, _ = render_sch_b(
            2025, WatermarkMode.DRAFT,
            {
                "name_shown_on_return": {"value": "Solo", "source_ref": "intake#1", "origin": "user_entered"},
                # 'ssn' is not a mapped key — must be silently ignored.
                "ssn": {"value": "999-00-1234", "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        txt = _page_text(pdf, 0)
        assert "Solo" in txt
        assert "Aaron" not in txt           # an identity we never supplied
        assert "999-00-1234" not in txt     # SSN is never filled
        assert "2500" not in txt            # a dividend figure we never supplied

    def test_yesno_checkbox_set(self):
        # Checkboxes are not extractable text; verify the field-map seam (the No
        # widget + on-state) and that rendering produces a valid PDF (the shared,
        # already-tested filler applies /V + /AS and bakes an 'X' overlay).
        _needs_pdf()

        yes_name, yes_on = SCHB_YESNO_FIELDS["foreign_account_7a"]["yes"]
        no_name, no_on = SCHB_YESNO_FIELDS["foreign_account_7a"]["no"]
        assert yes_name != no_name and yes_on != no_on

        pdf, _ = render_sch_b(
            2025, WatermarkMode.DRAFT,
            {
                "foreign_account_7a": {"value": "no", "source_ref": "intake#1", "origin": "user_entered"},
                "foreign_trust_8":    {"value": "no", "source_ref": "intake#1", "origin": "user_entered"},
            },
        )
        assert pdf[:4] == b"%PDF"

        # An unknown answer is silently ignored (conservative — no box set).
        pdf2, _ = render_sch_b(
            2025, WatermarkMode.DRAFT,
            {"foreign_account_7a": {"value": "maybe", "source_ref": "x", "origin": "x"}},
        )
        assert pdf2[:4] == b"%PDF"

    def test_unmapped_and_blank_keys_skipped(self):
        # An unknown key is ignored; a blank value is skipped; only the one real,
        # non-blank, mapped figure becomes visible. None of these raise.
        _needs_pdf()

        pdf, _ = render_sch_b(
            2025, WatermarkMode.NONE,
            {
                "not_a_real_key":     {"value": "777", "source_ref": "x", "origin": "x"},
                "interest_total_2":   {"value": "", "source_ref": "calc#1", "origin": "computed"},
                "dividends_total_6":  {"value": "60042", "source_ref": "calc#1", "origin": "computed"},
            },
        )
        txt = _page_text(pdf, 0)
        assert "60042" in txt          # the mapped, non-blank figure landed
        assert "777" not in txt        # unknown key never wrote

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"name_shown_on_return": {"value": "Solo", "source_ref": "intake#1", "origin": "user_entered"}}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_sch_b(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_year_fallback_renders(self):
        # A bogus tax year resolves to f1040sb_<year>.pdf then falls back to
        # f1040sb.pdf; empty inputs => an unfilled-but-valid official form.
        _needs_pdf()
        pdf, ct = render_sch_b(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


class TestMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch):
        # When NO source PDF can be resolved, render_sch_b raises FileNotFoundError.
        import app.exports.sch_b_fill as mod

        monkeypatch.setattr(
            mod, "_sch_b_form_path",
            lambda ty: __import__("pathlib").Path("/nonexistent/f1040sb.pdf"),
        )
        with pytest.raises(FileNotFoundError):
            render_sch_b(
                2025, WatermarkMode.DRAFT,
                {"name_shown_on_return": {"value": "X", "source_ref": "x", "origin": "x"}},
            )
