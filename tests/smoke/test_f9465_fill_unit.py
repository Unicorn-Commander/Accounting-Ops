"""Unit tests for Form 9465 (Installment Agreement Request) renderer.

Ground truth is the official Rev. 9-2020 fillable PDF at
``data/irs_forms/f9465.pdf`` (fetched by ``scripts/download_irs_forms.sh``).
The field map was pinned by dumping that PDF's AcroForm (names, /FT types,
checkbox on-states, widget rects) and visually confirming a marker render;
``TestSourcePdfGroundTruth`` re-asserts those pins against the live PDF on
every run.

Render tests do a REAL round-trip through the module's public
``render_9465``: values are read back from the produced PDF with BOTH pypdf
(``extract_text``) and pymupdf (``bake()`` + ``get_text``). The renderer
flattens the AcroForm (values are baked into page content so they print
everywhere), so ``pypdf.get_fields()`` on the OUTPUT must be empty — that is
asserted too, and it is why the value read-back is text-based.

Missing-PDF behavior is tested by monkeypatching the module's forms dir to a
``tmp_path`` — those tests never depend on whether the repo has the PDF.
"""

from __future__ import annotations

import io
import re

import pytest

from app.exports.f9465_field_map import (
    F9465_FIELDS,
    F9465_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
    VALID_F9465_FIELDS,
)
from app.exports.f9465_fill import render_9465, _f9465_form_path
from app.exports.form_prefill import WatermarkMode

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"


# ---------------------------------------------------------------------------
# Field map structure + ground-truth pins (pure python — no PDF needed)
# ---------------------------------------------------------------------------

class TestF9465FieldMap:
    def test_text_field_names_pinned(self):
        # Spot-pin the rows most easily mis-mapped, verified against the
        # Rev. 9-2020 widget rects: name row 1a, address block, the line 5-12
        # money column, and Part II.
        assert F9465_FIELDS["request_forms"] == f"{_P1}.f1_1[0]"
        assert F9465_FIELDS["tax_periods"] == f"{_P1}.f1_2[0]"
        assert F9465_FIELDS["first_name"] == f"{_P1}.f1_3[0]"
        assert F9465_FIELDS["last_name"] == f"{_P1}.f1_4[0]"
        assert F9465_FIELDS["ssn"] == f"{_P1}.f1_5[0]"
        assert F9465_FIELDS["spouse_ssn"] == f"{_P1}.f1_8[0]"
        assert F9465_FIELDS["address"] == f"{_P1}.f1_9[0]"
        assert F9465_FIELDS["apt"] == f"{_P1}.f1_10[0]"
        assert F9465_FIELDS["city_state_zip"] == f"{_P1}.f1_11[0]"
        assert F9465_FIELDS["total_owed_5"] == f"{_P1}.f1_22[0]"
        assert F9465_FIELDS["payment_with_request_8"] == f"{_P1}.f1_25[0]"
        assert F9465_FIELDS["monthly_payment_11a"] == f"{_P1}.f1_28[0]"
        assert F9465_FIELDS["payment_day_12"] == f"{_P1}.f1_30[0]"
        assert F9465_FIELDS["routing_number"] == f"{_P1}.f1_31[0]"
        assert F9465_FIELDS["account_number"] == f"{_P1}.f1_32[0]"
        assert F9465_FIELDS["county_15"] == f"{_P2}.f2_1[0]"
        assert F9465_FIELDS["dependent_care_27"] == f"{_P2}.f2_10[0]"

    def test_checkbox_names_and_on_states_pinned(self):
        assert F9465_CHECKBOX_FIELDS["address_new_1b"] == (f"{_P1}.c1_1[0]", "1")
        # The 433-F bullet checkbox nests under BulletedList[0].Bullet1[0] —
        # a bare Page1[0].c1_2[0] name does NOT match the PDF's /T chain.
        assert F9465_CHECKBOX_FIELDS["cannot_increase_11b_433f"] == (
            f"{_P1}.BulletedList[0].Bullet1[0].c1_2[0]", "1",
        )
        assert F9465_CHECKBOX_FIELDS["low_income_no_debit_13c"] == (f"{_P1}.c1_3[0]", "1")
        assert F9465_CHECKBOX_FIELDS["payroll_deduction_14"] == (f"{_P1}.c1_4[0]", "1")
        # Radio-style groups: each XFA kid is its own field with its own
        # on-state (/1../4) — e.g. marital status and pay frequency.
        assert F9465_CHECKBOX_FIELDS["marital_single_16a"] == (f"{_P2}.c2_1[0]", "1")
        assert F9465_CHECKBOX_FIELDS["marital_married_16a"] == (f"{_P2}.c2_1[1]", "2")
        assert F9465_CHECKBOX_FIELDS["paid_weekly_19"] == (f"{_P2}.c2_3[0]", "1")
        assert F9465_CHECKBOX_FIELDS["paid_biweekly_19"] == (f"{_P2}.c2_3[1]", "2")
        assert F9465_CHECKBOX_FIELDS["paid_monthly_19"] == (f"{_P2}.c2_3[2]", "3")
        assert F9465_CHECKBOX_FIELDS["paid_twice_month_19"] == (f"{_P2}.c2_3[3]", "4")
        assert F9465_CHECKBOX_FIELDS["court_deducted_no_26b"] == (f"{_P2}.c2_8[1]", "2")

    def test_checkbox_values_are_name_onstate_tuples(self):
        for key, val in F9465_CHECKBOX_FIELDS.items():
            assert isinstance(key, str)
            assert isinstance(val, tuple) and len(val) == 2
            name, on_state = val
            assert name.startswith("topmostSubform[0].Page")
            assert on_state in {"1", "2", "3", "4"}

    def test_no_duplicate_acroform_targets(self):
        # Two logical keys must never write the same widget.
        names = list(F9465_FIELDS.values()) + [v[0] for v in F9465_CHECKBOX_FIELDS.values()]
        assert len(names) == len(set(names))

    def test_taxpayer_completed_keys(self):
        assert TAXPAYER_COMPLETED_KEYS == frozenset(
            {"ssn", "spouse_ssn", "routing_number", "account_number"}
        )
        # They stay mapped (documented positions) but are gated off at render.
        for k in TAXPAYER_COMPLETED_KEYS:
            assert k in F9465_FIELDS

    def test_valid_fields_contains_all_map_values(self):
        assert set(F9465_FIELDS.values()) <= VALID_F9465_FIELDS
        assert {v[0] for v in F9465_CHECKBOX_FIELDS.values()} <= VALID_F9465_FIELDS
        assert len(VALID_F9465_FIELDS) == (
            len(F9465_FIELDS) + len(F9465_CHECKBOX_FIELDS)
        )

    def test_legacy_misconceived_keys_dropped(self):
        # Keys from the pre-audit map that have NO target on Rev. 9-2020.
        for gone in (
            "account_type", "city", "state", "zip", "name", "spouse_name",
            "phone", "household_status", "employment_status", "payment_method",
        ):
            assert gone not in F9465_FIELDS, gone
        for gone in ("address_changed", "related_return", "other_agreement", "consent"):
            assert gone not in F9465_CHECKBOX_FIELDS, gone


# ---------------------------------------------------------------------------
# Ground truth: every mapped name/type/on-state exists in the source PDF
# ---------------------------------------------------------------------------

class TestSourcePdfGroundTruth:
    def test_map_matches_source_acroform(self):
        from pypdf import PdfReader

        src = _f9465_form_path(None)
        assert src.exists(), (
            f"{src} missing — run scripts/download_irs_forms.sh; the field map "
            "can only be validated against the official PDF"
        )
        fields = PdfReader(str(src)).get_fields()
        assert fields, "f9465.pdf has no AcroForm fields — wrong file?"

        for key, name in F9465_FIELDS.items():
            assert name in fields, f"text key {key!r}: {name} not in PDF"
            assert fields[name].get("/FT") == "/Tx", f"{name} is not a text field"

        for key, (name, on_state) in F9465_CHECKBOX_FIELDS.items():
            assert name in fields, f"checkbox key {key!r}: {name} not in PDF"
            assert fields[name].get("/FT") == "/Btn", f"{name} is not a button"
            states = fields[name].get("/_States_") or []
            assert f"/{on_state}" in [str(s) for s in states], (
                f"{key!r}: on-state /{on_state} not among {states} for {name}"
            )


# ---------------------------------------------------------------------------
# Render round-trip (requires the repo's official PDF; never skips)
# ---------------------------------------------------------------------------

# Realistic synthetic request: every value is unique and none of them occur in
# a blank render of the form (guarded below), so "value in extracted text" is
# a meaningful placement check, not a substring accident.
_TEXT_INPUTS: dict[str, str] = {
    "request_forms": "1040-SR",
    "tax_periods": "2024",
    "first_name": "Avery R",
    "last_name": "Quimby",
    "spouse_first_name": "Jordan L",
    "spouse_last_name": "Quimby-Reyes",
    "address": "1420 Palmetto Ave",
    "apt": "12B",
    "city_state_zip": "Folly Beach, SC 29439",
    "business_name_2": "Quimby Surf LLC",
    "business_ein_2": "81-4455667",
    "home_phone_3": "843-555-0141",
    "best_time_call_3": "9am-11am",
    "work_phone_4": "843-555-0198",
    "work_ext_4": "204",
    "best_time_call_4": "1pm-3pm",
    "total_owed_5": "18,400",
    "additional_bal_6": "600",
    "total_liability_7": "19,000",
    "payment_with_request_8": "1,050",
    "amount_owed_9": "17,950",
    "divided_by_72_10": "249.31",
    "monthly_payment_11a": "425",
    "revised_monthly_payment_11b": "455",
    "payment_day_12": "16",
    "county_15": "Charleston",
    "dependents_17": "2",
    "age_65_older_18": "0",
    "net_income_20": "2,850",
    "spouse_net_income_22": "1,975",
    "vehicles_23": "3",
    "car_payments_24": "1",
    "health_premiums_25c": "310",
    "court_payments_26c": "215",
    "dependent_care_27": "480",
}
# Short numeric counts whose digits also occur in the form's printed text —
# these are verified by exact-word COUNT DELTA against a blank render instead
# of raw substring search.
_WORD_DELTA_KEYS = {"tax_periods", "additional_bal_6", "work_ext_4",
                    "monthly_payment_11a", "revised_monthly_payment_11b",
                    "payment_day_12", "dependents_17", "age_65_older_18",
                    "vehicles_23", "car_payments_24", "health_premiums_25c",
                    "court_payments_26c", "dependent_care_27"}


def _inputs(pairs: dict[str, str]) -> dict:
    return {
        k: {"value": v, "source_ref": f"intake#{k}", "origin": "user_entered"}
        for k, v in pairs.items()
    }


def _pypdf_text(pdf: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def _fitz_pages_text(pdf: bytes) -> list[str]:
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    doc.bake()
    out = [doc[i].get_text() for i in range(len(doc))]
    doc.close()
    return out


def _fitz_words(pdf: bytes) -> list[str]:
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    doc.bake()
    words: list[str] = []
    for i in range(len(doc)):
        words.extend(w[4] for w in doc[i].get_text("words"))
    doc.close()
    return words


class TestF9465Render:
    @pytest.fixture(scope="class")
    def blank_pdf(self) -> bytes:
        pdf, ct = render_9465(2025, WatermarkMode.NONE, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        return pdf

    @pytest.fixture(scope="class")
    def filled_pdf(self) -> bytes:
        pdf, ct = render_9465(2025, WatermarkMode.NONE, _inputs(_TEXT_INPUTS))
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        return pdf

    def test_every_value_round_trips_via_pypdf_and_fitz(self, blank_pdf, filled_pdf):
        values = list(_TEXT_INPUTS.values())
        assert len(values) == len(set(values)), "test values must be unique"

        blank_pypdf = _pypdf_text(blank_pdf)
        blank_fitz = "\n".join(_fitz_pages_text(blank_pdf))
        filled_pypdf = _pypdf_text(filled_pdf)
        filled_fitz = "\n".join(_fitz_pages_text(filled_pdf))
        blank_words = _fitz_words(blank_pdf)
        filled_words = _fitz_words(filled_pdf)

        for key, value in _TEXT_INPUTS.items():
            if key in _WORD_DELTA_KEYS:
                # Exact-word count must increase by exactly one — meaningful
                # even when the digits appear inside the printed form text.
                b, f = blank_words.count(value), filled_words.count(value)
                assert f == b + 1, f"{key}={value!r}: word count {b} -> {f}"
            else:
                assert value not in blank_pypdf, (
                    f"{key}={value!r} already in blank render — weak needle"
                )
                assert value not in blank_fitz, f"{key}={value!r} weak fitz needle"
                assert value in filled_pypdf, f"{key}={value!r} missing (pypdf)"
                assert value in filled_fitz, f"{key}={value!r} missing (fitz)"

    def test_page1_and_page2_both_carry_values(self, filled_pdf):
        pages = _fitz_pages_text(filled_pdf)
        assert len(pages) == 2
        assert "Quimby" in pages[0] and "1420 Palmetto Ave" in pages[0]
        assert "Charleston" in pages[1] and "1,975" in pages[1]

    def test_output_is_flattened_no_editable_fields(self, filled_pdf):
        # House invariant: values are BAKED and the AcroForm is dropped, so
        # the output prints everywhere and nothing double-renders. This is
        # why the value round-trip above is text-based.
        from pypdf import PdfReader

        assert not PdfReader(io.BytesIO(filled_pdf)).get_fields()

    def test_taxpayer_completed_keys_never_filled(self, blank_pdf):
        pdf, _ = render_9465(
            2025, WatermarkMode.NONE,
            _inputs({
                "first_name": "Avery R",
                "ssn": "123-45-6789",
                "spouse_ssn": "987-65-4321",
                "routing_number": "053100300",
                "account_number": "004415887766",
            }),
        )
        both = _pypdf_text(pdf) + "\n".join(_fitz_pages_text(pdf))
        assert "Avery R" in both
        for leak in ("123-45-6789", "987-65-4321", "053100300", "004415887766"):
            assert leak not in both

    def test_blank_whitespace_unknown_and_falsey_are_skipped(self, blank_pdf):
        pdf, _ = render_9465(
            2025, WatermarkMode.NONE,
            {
                "total_owed_5":   {"value": "18,400", "source_ref": "calc#1", "origin": "computed"},
                "additional_bal_6": {"value": "", "source_ref": "calc#1", "origin": "computed"},
                "county_15":      {"value": "   ", "source_ref": "intake#1", "origin": "user_entered"},
                "not_a_real_key": {"value": "31337", "source_ref": "x", "origin": "x"},
                "payroll_deduction_14": {"value": "false", "source_ref": "x", "origin": "x"},
                "marital_single_16a": {"value": "no", "source_ref": "x", "origin": "x"},
                "bad_payload":    "not-a-dict",
            },
        )
        text = _pypdf_text(pdf)
        fitz_text = "\n".join(_fitz_pages_text(pdf))
        assert "18,400" in text and "18,400" in fitz_text
        assert "31337" not in text and "31337" not in fitz_text
        # No checkbox was legitimately set -> no baked X anywhere.
        assert _fitz_words(pdf).count("X") == _fitz_words(blank_pdf).count("X")

    @pytest.mark.parametrize(
        "key,page_index",
        [
            ("address_new_1b", 0),
            ("cannot_increase_11b_433f", 0),   # regression: nested BulletedList name
            ("low_income_no_debit_13c", 0),
            ("payroll_deduction_14", 0),
            ("marital_married_16a", 1),
            ("paid_monthly_19", 1),
            ("spouse_paid_twice_month_21", 1),
            ("health_insurance_no_25a", 1),
            ("court_deducted_yes_26b", 1),
        ],
    )
    def test_each_checkbox_bakes_exactly_one_x(self, blank_pdf, key, page_index):
        import fitz

        pdf, _ = render_9465(
            2025, WatermarkMode.NONE,
            _inputs({key: "true"}),
        )

        def page_x_words(raw: bytes, idx: int) -> int:
            doc = fitz.open(stream=raw, filetype="pdf")
            doc.bake()
            n = sum(1 for w in doc[idx].get_text("words") if w[4] == "X")
            other = sum(
                1
                for i in range(len(doc))
                if i != idx
                for w in doc[i].get_text("words")
                if w[4] == "X"
            )
            doc.close()
            return n, other

        blank_n, blank_other = page_x_words(blank_pdf, page_index)
        set_n, set_other = page_x_words(pdf, page_index)
        assert set_n == blank_n + 1, f"{key}: expected one new X on page {page_index}"
        assert set_other == blank_other, f"{key}: X leaked onto the other page"

    def test_all_watermark_modes_render(self):
        inputs = _inputs({"first_name": "Avery R"})
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, ct = render_9465(2025, mode, inputs)
            assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        draft_text = "\n".join(_fitz_pages_text(render_9465(2025, WatermarkMode.DRAFT, inputs)[0]))
        assert re.search(r"DRAFT", draft_text), "DRAFT watermark must be visible"


# ---------------------------------------------------------------------------
# Path resolution + missing PDF (state-independent: never touches the repo dir)
# ---------------------------------------------------------------------------

class TestPathResolutionAndMissingPdf:
    def test_missing_pdf_raises_filenotfound(self, monkeypatch, tmp_path):
        import app.exports.f9465_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)  # empty dir
        with pytest.raises(FileNotFoundError) as exc:
            render_9465(
                2025, WatermarkMode.DRAFT,
                _inputs({"first_name": "Nobody"}),
            )
        assert "download_irs_forms.sh" in str(exc.value)

    def test_year_specific_pdf_preferred(self, monkeypatch, tmp_path):
        import app.exports.f9465_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)
        base = tmp_path / "f9465.pdf"
        yearly = tmp_path / "f9465_2025.pdf"
        base.touch()
        yearly.touch()
        assert _f9465_form_path(2025) == yearly
        assert _f9465_form_path(2024) == base   # no f9465_2024.pdf -> fallback
        assert _f9465_form_path(None) == base

    def test_unresolved_path_is_base_candidate(self, monkeypatch, tmp_path):
        import app.exports.f9465_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)
        # Nothing exists: resolution still returns the deterministic fallback
        # (the unversioned name) for the error message.
        assert _f9465_form_path(2025) == tmp_path / "f9465.pdf"
