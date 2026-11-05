"""Unit tests for Form 9465 (Payment Plan Request) renderer.

Ground truth is the official Rev. 12-2026 fillable PDF at
``data/irs_forms/f9465.pdf`` (fetched by ``scripts/download_irs_forms.sh``).
The field map was pinned by dumping that PDF's AcroForm (names, /FT types,
checkbox /AP /N on-states, widget rects) and visually confirming a marker
render; ``TestSourcePdfGroundTruth`` re-asserts those pins against the live
PDF on every run, and fails on a revision change before any field assertion.

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

import hashlib
import io
import re

import pytest

from app.exports.f9465_field_map import (
    F9465_CHECKBOX_FIELDS,
    F9465_FIELDS,
    F9465_PINNED_FIELD_COUNT,
    F9465_PINNED_PAGE_COUNT,
    F9465_PINNED_REVISION,
    F9465_PINNED_SHA256,
    F9465_UNMAPPED_KEYS,
    TAXPAYER_COMPLETED_KEYS,
    VALID_F9465_FIELDS,
)
from app.exports.f9465_fill import render_9465, _f9465_form_path
from app.exports.form_prefill import WatermarkMode

_P1 = "topmostSubform[0].Page1[0]"


# ---------------------------------------------------------------------------
# Field map structure + ground-truth pins (pure python — no PDF needed)
# ---------------------------------------------------------------------------

class TestF9465FieldMap:
    def test_text_field_names_pinned(self):
        # Full pin, not a spot check. Rects were matched to captions on the
        # Rev. 12-2026 PDF: zero-padded f1_01..f1_09, ReadOrder parents on
        # the phone row, line 10 = monthly amount (f1_27), line 11 = pay
        # day (f1_28), line 12a/12b nested under RoutingNo / AccountNo.
        assert F9465_FIELDS == {
            "request_forms": f"{_P1}.f1_01[0]",
            "tax_periods": f"{_P1}.f1_02[0]",
            "first_name": f"{_P1}.f1_03[0]",
            "last_name": f"{_P1}.f1_04[0]",
            "ssn": f"{_P1}.f1_05[0]",
            "spouse_first_name": f"{_P1}.f1_06[0]",
            "spouse_last_name": f"{_P1}.f1_07[0]",
            "spouse_ssn": f"{_P1}.f1_08[0]",
            "address": f"{_P1}.f1_09[0]",
            "apt": f"{_P1}.f1_10[0]",
            "city_state_zip": f"{_P1}.f1_11[0]",
            "foreign_country": f"{_P1}.f1_12[0]",
            "foreign_province": f"{_P1}.f1_13[0]",
            "foreign_postal_code": f"{_P1}.f1_14[0]",
            "business_name_2": f"{_P1}.f1_15[0]",
            "business_ein_2": f"{_P1}.f1_16[0]",
            "home_phone_3": f"{_P1}.PhoneNumber_Ln3_ReadOrder[0].f1_17[0]",
            "best_time_call_3": f"{_P1}.BestTime_Ln3_ReadOrder[0].f1_18[0]",
            "work_phone_4": f"{_P1}.PhoneNumber_Ln4_ReadOrder[0].f1_19[0]",
            "work_ext_4": f"{_P1}.Ext_ReadOrder[0].f1_20[0]",
            "best_time_call_4": f"{_P1}.f1_21[0]",
            "total_owed_5": f"{_P1}.f1_22[0]",
            "additional_bal_6": f"{_P1}.f1_23[0]",
            "total_liability_7": f"{_P1}.f1_24[0]",
            "payment_with_request_8": f"{_P1}.f1_25[0]",
            "amount_owed_9": f"{_P1}.f1_26[0]",
            "monthly_payment_11a": f"{_P1}.f1_27[0]",
            "payment_day_12": f"{_P1}.f1_28[0]",
            "routing_number": f"{_P1}.RoutingNo[0].f1_29[0]",
            "account_number": f"{_P1}.AccountNo[0].f1_30[0]",
        }

    def test_checkbox_names_and_on_states_pinned(self):
        # Both boxes export /1 from /AP /N. c1_2 is line 12c (low-income),
        # a direct Page1 child — the Rev. 9-2020 BulletedList c1_2 is gone.
        assert F9465_CHECKBOX_FIELDS == {
            "address_new_1b": (f"{_P1}.c1_1[0]", "1"),
            "low_income_no_debit_13c": (f"{_P1}.c1_2[0]", "1"),
        }
        assert "BulletedList" not in F9465_CHECKBOX_FIELDS["low_income_no_debit_13c"][0]

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
        # Keys from the pre-audit map that had no target on Rev. 9-2020
        # and still have none on Rev. 12-2026.
        for gone in (
            "account_type", "city", "state", "zip", "name", "spouse_name",
            "phone", "household_status", "employment_status", "payment_method",
        ):
            assert gone not in F9465_FIELDS, gone
        for gone in ("address_changed", "related_return", "other_agreement", "consent"):
            assert gone not in F9465_CHECKBOX_FIELDS, gone

    def test_removed_lines_stay_unmapped(self):
        # Rev. 12-2026 deleted these lines. Mapping one of them onto a
        # surviving widget would write the wrong caption.
        assert F9465_UNMAPPED_KEYS == frozenset({
            "divided_by_72_10",
            "revised_monthly_payment_11b",
            "cannot_increase_11b_433f",
            "payroll_deduction_14",
            "county_15",
            "marital_single_16a",
            "marital_married_16a",
            "share_expenses_yes_16b",
            "share_expenses_no_16b",
            "dependents_17",
            "age_65_older_18",
            "paid_weekly_19",
            "paid_biweekly_19",
            "paid_monthly_19",
            "paid_twice_month_19",
            "net_income_20",
            "spouse_paid_weekly_21",
            "spouse_paid_biweekly_21",
            "spouse_paid_monthly_21",
            "spouse_paid_twice_month_21",
            "spouse_net_income_22",
            "vehicles_23",
            "car_payments_24",
            "health_insurance_yes_25a",
            "health_insurance_no_25a",
            "premiums_deducted_yes_25b",
            "premiums_deducted_no_25b",
            "health_premiums_25c",
            "court_payments_yes_26a",
            "court_payments_no_26a",
            "court_deducted_yes_26b",
            "court_deducted_no_26b",
            "court_payments_26c",
            "dependent_care_27",
        })
        assert len(F9465_UNMAPPED_KEYS) == 34
        for key in F9465_UNMAPPED_KEYS:
            assert key not in F9465_FIELDS, key
            assert key not in F9465_CHECKBOX_FIELDS, key


# ---------------------------------------------------------------------------
# Ground truth: every mapped name/type/on-state exists in the source PDF
# ---------------------------------------------------------------------------

def _printed_revision(pdf_bytes: bytes) -> str:
    """Footer string ``Form 9465 (Rev. M-YYYY)`` from the page text."""
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        text = "\n".join(doc[i].get_text() for i in range(len(doc)))
    finally:
        doc.close()
    match = re.search(r"Form 9465 \(Rev\. [^)]+\)", text)
    return match.group(0) if match else ""


def _widget_ap_on_states(pdf_path) -> dict[str, set[str]]:
    """Terminal widget qualified name -> /AP /N keys other than /Off."""
    from pypdf import PdfReader
    from pypdf.generic import IndirectObject

    def resolve(obj):
        return obj.get_object() if isinstance(obj, IndirectObject) else obj

    found: dict[str, set[str]] = {}
    reader = PdfReader(str(pdf_path))
    for page in reader.pages:
        annots = page.get("/Annots")
        if not annots:
            continue
        for ref in resolve(annots):
            annot = resolve(ref)
            if str(annot.get("/Subtype")) != "/Widget":
                continue
            parts: list[str] = []
            node = annot
            seen: set[int] = set()
            while node is not None and id(node) not in seen:
                seen.add(id(node))
                partial = node.get("/T")
                if partial:
                    parts.append(str(partial))
                parent = node.get("/Parent")
                node = resolve(parent) if parent is not None else None
            parts.reverse()
            ap = annot.get("/AP")
            if ap is None:
                continue
            normal = resolve(ap).get("/N")
            if normal is None:
                continue
            normal = resolve(normal)
            if not hasattr(normal, "keys"):
                continue
            on_states = {str(key) for key in normal.keys() if str(key) != "/Off"}
            found[".".join(parts)] = on_states
    return found


class TestSourcePdfGroundTruth:
    def test_map_matches_source_acroform(self):
        from pypdf import PdfReader

        src = _f9465_form_path(None)
        assert src.exists(), (
            f"{src} missing — run scripts/download_irs_forms.sh; the field map "
            "can only be validated against the official PDF"
        )
        pdf_bytes = src.read_bytes()
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        printed = _printed_revision(pdf_bytes)
        # Revision gate runs before any field assertion. Either the bytes we
        # verified, or the same printed revision, may proceed; a PDF that
        # matches neither is a new revision and must fail loudly.
        assert digest == F9465_PINNED_SHA256 or printed == F9465_PINNED_REVISION, (
            f"Form 9465 revision changed: map verified against "
            f"{F9465_PINNED_REVISION}, PDF is {printed or 'unrecognized'}"
        )
        if digest == F9465_PINNED_SHA256:
            assert printed == F9465_PINNED_REVISION, (
                f"pinned sha256 matched but printed revision was {printed!r}"
            )

        reader = PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == F9465_PINNED_PAGE_COUNT, (
            f"Form 9465 page count changed: map verified against "
            f"{F9465_PINNED_PAGE_COUNT}, PDF has {len(reader.pages)}"
        )
        fields = reader.get_fields()
        assert fields, "f9465.pdf has no AcroForm fields — wrong file?"
        assert len(fields) == F9465_PINNED_FIELD_COUNT, (
            f"Form 9465 field count changed: map verified against "
            f"{F9465_PINNED_FIELD_COUNT}, PDF has {len(fields)}"
        )

        for key, name in F9465_FIELDS.items():
            assert name in fields, f"text key {key!r}: {name} not in PDF"
            assert fields[name].get("/FT") == "/Tx", f"{name} is not a text field"

        ap_on = _widget_ap_on_states(src)
        for key, (name, on_state) in F9465_CHECKBOX_FIELDS.items():
            assert name in fields, f"checkbox key {key!r}: {name} not in PDF"
            assert fields[name].get("/FT") == "/Btn", f"{name} is not a button"
            states = fields[name].get("/_States_") or []
            assert f"/{on_state}" in [str(s) for s in states], (
                f"{key!r}: on-state /{on_state} not among {states} for {name}"
            )
            # The value we write is the widget's /AP /N export, and that
            # appearance is the only non-Off state (the baked mark is one X).
            assert ap_on.get(name) == {f"/{on_state}"}, (
                f"{key!r}: /AP /N on-states {ap_on.get(name)} != /{on_state}"
            )

    def test_registry_title_matches_printed_title(self):
        """Catalog title is the title printed on the pinned revision."""
        import fitz

        from app.exports.form_registry import get_spec

        src = _f9465_form_path(None)
        pdf_bytes = src.read_bytes()
        assert _printed_revision(pdf_bytes) == F9465_PINNED_REVISION
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            lines = [ln.strip() for ln in doc[0].get_text().splitlines() if ln.strip()]
        finally:
            doc.close()
        # Header order: "Form 9465", "(Rev. December 2026)", printed title,
        # then "Department of the Treasury".
        treasury = lines.index("Department of the Treasury")
        printed_title = lines[treasury - 1]
        spec = get_spec("9465")
        assert spec is not None and spec.kind == "9465"
        assert spec.title == f"Form 9465 — {printed_title}"


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
    "foreign_country": "Freedonia",
    "foreign_province": "Eastmarch",
    "foreign_postal_code": "FX-4401",
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
    "monthly_payment_11a": "425",
    "payment_day_12": "16",
}
# Short numeric counts whose digits also occur in the form's printed text —
# these are verified by exact-word COUNT DELTA against a blank render instead
# of raw substring search. "2024" is an example year printed on the form.
_WORD_DELTA_KEYS = {
    "tax_periods", "additional_bal_6", "work_ext_4",
    "monthly_payment_11a", "payment_day_12",
}


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

    def test_the_only_page_carries_identity_and_payment_lines(self, filled_pdf):
        # Rev. 12-2026 is one page. Part II (county, spouse net income) is gone.
        pages = _fitz_pages_text(filled_pdf)
        assert len(pages) == 1
        assert "Quimby" in pages[0] and "1420 Palmetto Ave" in pages[0]
        assert "Freedonia" in pages[0] and "1,050" in pages[0]
        assert "425" in pages[0]  # printed line 10, monthly_payment_11a

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
                "best_time_call_3": {"value": "   ", "source_ref": "intake#1", "origin": "user_entered"},
                # Removed lines must not land on a surviving widget.
                "county_15": {"value": "Charleston", "source_ref": "intake#1", "origin": "user_entered"},
                "divided_by_72_10": {"value": "249.31", "source_ref": "calc#1", "origin": "computed"},
                "revised_monthly_payment_11b": {
                    "value": "455", "source_ref": "intake#1", "origin": "user_entered",
                },
                "not_a_real_key": {"value": "31337", "source_ref": "x", "origin": "x"},
                "payroll_deduction_14": {"value": "true", "source_ref": "x", "origin": "x"},
                "marital_single_16a": {"value": "yes", "source_ref": "x", "origin": "x"},
                "bad_payload":    "not-a-dict",
            },
        )
        text = _pypdf_text(pdf)
        fitz_text = "\n".join(_fitz_pages_text(pdf))
        both = text + "\n" + fitz_text
        assert "18,400" in text and "18,400" in fitz_text
        assert "31337" not in both
        for dropped in ("Charleston", "249.31", "455"):
            assert dropped not in both, dropped
        # Truthy values on removed checkbox keys must not bake an X.
        assert _fitz_words(pdf).count("X") == _fitz_words(blank_pdf).count("X")

    @pytest.mark.parametrize(
        "key,page_index",
        [
            ("address_new_1b", 0),
            ("low_income_no_debit_13c", 0),  # printed line 12c, was 13c / c1_3
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
