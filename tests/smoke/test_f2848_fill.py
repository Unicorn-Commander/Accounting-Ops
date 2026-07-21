"""Tests for the Form 2848 (Power of Attorney) renderer.

State-independence rules
------------------------
- The missing-PDF test monkeypatches the module's forms-dir resolution to a
  ``tmp_path`` — it never depends on whether the repo has the PDF.
- The success tests use the real ``data/irs_forms/f2848.pdf`` and assert a
  REAL round-trip (no skips): pypdf ``get_fields()`` values at the pre-flatten
  stage, plus baked-text readback of the final artifact via BOTH pymupdf
  (``bake()`` + ``get_text()``) and pypdf (``extract_text()``). The final
  artifact is deliberately FLATTENED by the house renderer
  (``_render_acroform_pdf``), so ``get_fields()`` on the output must be empty
  — that flattening is itself asserted as an invariant.
- Checkbox placement is asserted geometrically: the baked "X" for each checked
  box must land inside that box's widget /Rect (ground truth from the PDF
  dump), and boxes that were not checked must stay empty.
"""

from __future__ import annotations

import io

import fitz  # PyMuPDF
import pytest
from pypdf import PdfReader, PdfWriter

import app.exports.f2848_fill as f2848_fill
from app.exports.f2848_field_map import (
    F2848_CHECKBOX_FIELDS,
    F2848_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
    VALID_F2848_FIELDS,
)
from app.exports.f2848_fill import _f2848_form_path, render_2848
from app.exports.form_prefill import WatermarkMode, _set_acroform_values

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# Widgets that exist in the shipped PDF but are DELIBERATELY unwritable: the
# PDF's own error-check helper button, the line-7 taxpayer signature block,
# and the Part II representative signature/date columns.
_DELIBERATELY_UNMAPPED = frozenset(
    [
        f"{_P1}.CheckForm[0]",
        f"{_P2}.Title[0]",
        f"{_P2}.PrintName[0]",
        f"{_P2}.PrintNameTaxpayer[0]",
    ]
    + [f"{_P2}.Table_PartII[0].BodyRow{n}[0].Signature{n}[0]" for n in range(1, 5)]
    + [f"{_P2}.Table_PartII[0].BodyRow{n}[0].Date{n}[0]" for n in range(1, 5)]
)


def _sr(value: str) -> dict:
    return {"value": value, "source_ref": "test#1", "origin": "user_entered"}


# ---------------------------------------------------------------------------
# Field map consistency (pure — no PDF needed)
# ---------------------------------------------------------------------------
class TestF2848FieldMap:
    def test_full_shape(self):
        # 61 logical text keys over 59 physical /Tx boxes: taxpayer_city_state_zip
        # shares the single address box, and taxpayer_ssn_ein defaults to the
        # SSN sub-box. 21 checkboxes. No key collisions across the two maps.
        assert len(F2848_FIELDS) == 61
        assert len(set(F2848_FIELDS.values())) == 59
        assert F2848_FIELDS["taxpayer_city_state_zip"] == F2848_FIELDS["taxpayer_address"]
        assert F2848_FIELDS["taxpayer_ssn_ein"] == F2848_FIELDS["taxpayer_ssn"]
        assert len(F2848_CHECKBOX_FIELDS) == 21
        assert not set(F2848_FIELDS) & set(F2848_CHECKBOX_FIELDS)
        assert VALID_F2848_FIELDS == frozenset(
            list(F2848_FIELDS.values())
            + [v[0] for v in F2848_CHECKBOX_FIELDS.values()]
        )

    def test_all_checkboxes_use_on_state_1(self):
        # Every checkbox on this revision is an independent /Btn with states
        # ["/1", "/Off"] — the renderer must write on-state "1".
        assert all(on == "1" for _, on in F2848_CHECKBOX_FIELDS.values())

    def test_only_reps_1_and_2_have_sent_copies_boxes(self):
        # The printed form sends notice copies to at most two representatives;
        # the shipped PDF has no SentCopies3/4 widget.
        assert "rep1_sent_copies" in F2848_CHECKBOX_FIELDS
        assert "rep2_sent_copies" in F2848_CHECKBOX_FIELDS
        assert "rep3_sent_copies" not in F2848_CHECKBOX_FIELDS
        assert "rep4_sent_copies" not in F2848_CHECKBOX_FIELDS

    def test_taxpayer_completed_keys_are_unwritable(self):
        """Signature-block keys must not overlap any writable logical key, and
        the signature widgets themselves must be absent from the writable
        AcroForm-name set."""
        writable_keys = set(F2848_FIELDS) | set(F2848_CHECKBOX_FIELDS)
        assert not TAXPAYER_COMPLETED_KEYS & writable_keys
        assert not _DELIBERATELY_UNMAPPED & VALID_F2848_FIELDS
        # The keys the platform intake uses for the signature block are present.
        for key in (
            "signature_taxpayer",
            "signature_date",
            "signature_print_name",
            "signature_representative",
        ):
            assert key in TAXPAYER_COMPLETED_KEYS


# ---------------------------------------------------------------------------
# Ground truth: the map must match the shipped PDF exactly
# ---------------------------------------------------------------------------
class TestF2848MapMatchesPdf:
    def test_map_covers_all_pdf_terminals_except_documented_exclusions(self):
        source = _f2848_form_path(None)
        assert source.exists(), (
            f"{source} missing — run scripts/download_irs_forms.sh"
        )
        reader = PdfReader(str(source))
        fields = reader.get_fields() or {}
        terminals = {n for n, f in fields.items() if f.get("/FT") is not None}
        # Everything the map writes exists in the PDF …
        assert set(VALID_F2848_FIELDS) <= terminals
        # … and what it does not write is exactly the documented exclusion set.
        assert terminals - set(VALID_F2848_FIELDS) == set(_DELIBERATELY_UNMAPPED)

    def test_field_types_and_checkbox_on_states_match_pdf(self):
        source = _f2848_form_path(None)
        assert source.exists()
        fields = PdfReader(str(source)).get_fields() or {}
        for acro_name in set(F2848_FIELDS.values()):
            assert fields[acro_name].get("/FT") == "/Tx", acro_name
        for acro_name, on_state in F2848_CHECKBOX_FIELDS.values():
            f = fields[acro_name]
            assert f.get("/FT") == "/Btn", acro_name
            assert f"/{on_state}" in (f.get("/_States_") or []), acro_name


# ---------------------------------------------------------------------------
# Renderer behavior
# ---------------------------------------------------------------------------
class TestF2848Render:
    @pytest.fixture
    def sample_inputs(self):
        return {
            # Line 1 — the combined TIN key carries an SSN-formatted value.
            "taxpayer_name": _sr("Avery T. Quimby"),
            "taxpayer_address": _sr("1204 Sassafras Ln"),
            "taxpayer_city_state_zip": _sr("Folly Beach, SC 29439"),
            "taxpayer_ssn_ein": _sr("123-45-6789"),
            "taxpayer_telephone": _sr("843-555-0142"),
            # Line 2 — two representatives.
            "rep1_name": _sr("Rosa L. Ferreira, CPA"),
            "rep1_address": _sr("88 Meeting St, Charleston, SC 29401"),
            "rep1_caf": _sr("3102-44821R"),
            "rep1_ptin": _sr("P04412087"),
            "rep1_telephone": _sr("843-555-0177"),
            "rep1_fax": _sr("843-555-0178"),
            "rep2_name": _sr("Dev N. Batra, EA"),
            "rep2_caf": _sr("2209-71034R"),
            "rep2_ptin": _sr("P02998141"),
            # Line 3 — acts authorized.
            "acts_description_1": _sr("Income tax examination"),
            "acts_form_number_1": _sr("1040-SR"),
            "acts_years_1": _sr("2023 2024"),
            "acts_description_2": _sr("Employment tax matters"),
            "acts_form_number_2": _sr("941-X"),
            "acts_years_2": _sr("2024 Q1-Q4"),
            # Line 5a/5b free text.
            "additional_acts_1": _sr("See authority rider A"),
            "other_acts_description_1": _sr("Represent before IRS Appeals"),
            "specific_deletions_1": _sr("No authority to endorse refunds"),
            # Checkboxes: two truthy spellings, one explicit "1", one "x", and
            # one FALSY value that must leave its box off.
            "access_records": _sr("true"),
            "authorize_disclosure": _sr("yes"),
            "rep1_sent_copies": _sr("1"),
            "retention_revocation": _sr("x"),
            "substitute_or_add": _sr("false"),  # falsy -> box stays /Off
            # Part II declaration (signature/date columns stay handwritten).
            "rep1_designation": _sr("b"),
            "rep1_jurisdiction": _sr("South Carolina"),
            "rep1_bar_number": _sr("104729"),
            # Signature-block identity items — supplied but NEVER rendered.
            "signature_taxpayer": _sr("HANDWRITE-SIG"),
            "signature_date": _sr("HANDWRITE-DATE"),
            "signature_print_name": _sr("HANDWRITE-PN"),
            "signature_representative": _sr("HANDWRITE-REPSIG"),
            "rep1_signature": _sr("HANDWRITE-R1SIG"),
            # Unknown key — silently skipped, never rendered.
            "not_a_real_key": _sr("SHOULD-NOT-APPEAR"),
        }

    # Values that must be VISIBLE in the baked text. (Deliberately excludes
    # the merged-address parts — asserted as a merged string — and single-char
    # / caption-colliding values like the "b" designation, which are covered
    # exactly by the pre-flatten get_fields round-trip instead.)
    _EXPECTED_VISIBLE = [
        "Avery T. Quimby",
        "1204 Sassafras Ln, Folly Beach, SC 29439",  # merged into the one box
        "123-45-6789",
        "843-555-0142",
        "Rosa L. Ferreira, CPA",
        "88 Meeting St, Charleston, SC 29401",
        "3102-44821R",
        "P04412087",
        "843-555-0177",
        "843-555-0178",
        "Dev N. Batra, EA",
        "2209-71034R",
        "P02998141",
        "Income tax examination",
        "1040-SR",
        "2023 2024",
        "Employment tax matters",
        "941-X",
        "2024 Q1-Q4",
        "See authority rider A",
        "Represent before IRS Appeals",
        "No authority to endorse refunds",
        "South Carolina",
        "104729",
    ]
    _NEVER_VISIBLE = [
        "HANDWRITE-SIG",
        "HANDWRITE-DATE",
        "HANDWRITE-PN",
        "HANDWRITE-REPSIG",
        "HANDWRITE-R1SIG",
        "SHOULD-NOT-APPEAR",
    ]

    def test_missing_pdf_raises_regardless_of_repo_state(self, tmp_path, monkeypatch):
        """Resolution is redirected to an empty tmp dir, so this passes whether
        or not the repo currently has the real PDF."""
        monkeypatch.setattr(f2848_fill, "IRS_FORMS_DIR", tmp_path)
        with pytest.raises(FileNotFoundError, match="2848"):
            render_2848(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs={"taxpayer_name": _sr("Nobody")},
            )

    def test_year_specific_path_preferred_then_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(f2848_fill, "IRS_FORMS_DIR", tmp_path)
        fallback = tmp_path / "f2848.pdf"
        fallback.write_bytes(b"%PDF-1.4 placeholder")
        assert _f2848_form_path(2025) == fallback  # no year file -> fallback
        year_file = tmp_path / "f2848_2025.pdf"
        year_file.write_bytes(b"%PDF-1.4 placeholder")
        assert _f2848_form_path(2025) == year_file  # year file wins
        assert _f2848_form_path(None) == fallback  # no year -> unversioned

    def test_acroform_get_fields_roundtrip_of_mapped_values(self):
        """Prove the map's fully-qualified names really address the PDF's field
        tree: set /V through the house walker on a clone of the source PDF
        (pre-flatten stage) and read every value back via pypdf get_fields().
        Covers a representative of every region: line 1, all four rep blocks,
        the line-3 table, 5a/5b text, Part II, and checkboxes."""
        source = _f2848_form_path(None)
        assert source.exists()

        expected_text = {
            F2848_FIELDS["taxpayer_name"]: "Avery T. Quimby",
            F2848_FIELDS["taxpayer_address"]: "1204 Sassafras Ln, Folly Beach, SC 29439",
            F2848_FIELDS["taxpayer_ssn"]: "123-45-6789",
            F2848_FIELDS["taxpayer_itin"]: "900-70-0001",
            F2848_FIELDS["taxpayer_ein"]: "12-3456789",
            F2848_FIELDS["taxpayer_telephone"]: "843-555-0142",
            F2848_FIELDS["taxpayer_plan_number"]: "001",
            F2848_FIELDS["rep1_name"]: "Rosa L. Ferreira, CPA",
            F2848_FIELDS["rep2_address"]: "42 Broad St, Charleston, SC 29401",
            F2848_FIELDS["rep3_caf"]: "5501-90210R",
            F2848_FIELDS["rep4_ptin"]: "P09990001",
            F2848_FIELDS["acts_description_1"]: "Income tax examination",
            F2848_FIELDS["acts_form_number_2"]: "941-X",
            F2848_FIELDS["acts_years_3"]: "2022",
            F2848_FIELDS["additional_acts_2"]: "Rider line two",
            F2848_FIELDS["other_acts_description_2"]: "Appeals conferences",
            F2848_FIELDS["specific_deletions_2"]: "Deletions line two",
            F2848_FIELDS["rep1_designation"]: "b",
            F2848_FIELDS["rep2_jurisdiction"]: "South Carolina",
            F2848_FIELDS["rep4_bar_number"]: "104729",
        }
        expected_boxes = {
            F2848_CHECKBOX_FIELDS["access_records"][0]: "1",
            F2848_CHECKBOX_FIELDS["rep1_sent_copies"][0]: "1",
            F2848_CHECKBOX_FIELDS["rep4_new_fax"][0]: "1",
            F2848_CHECKBOX_FIELDS["retention_revocation"][0]: "1",
        }

        writer = PdfWriter(clone_from=io.BytesIO(source.read_bytes()))
        set_count = _set_acroform_values(
            writer, {**expected_text, **expected_boxes}
        )
        assert set_count == len(expected_text) + len(expected_boxes)

        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        readback = PdfReader(buf).get_fields()
        for acro_name, value in expected_text.items():
            assert str(readback[acro_name].get("/V")) == value, acro_name
        for acro_name, on_state in expected_boxes.items():
            assert str(readback[acro_name].get("/V")) == f"/{on_state}", acro_name

    @staticmethod
    def _checkbox_rect(logical_key: str) -> tuple[int, fitz.Rect]:
        """(page_index, widget /Rect in fitz top-left coords) for a checkbox —
        ground truth from the shipped PDF's own widget dump."""
        pdf_rects = {  # PDF coords (origin bottom-left), page height 792
            "access_records": (0, (230.4, 134.0, 238.4, 142.0)),
            "authorize_disclosure": (0, (64.8, 122.0, 72.8, 130.0)),
            "rep1_sent_copies": (0, (273.6, 530.0, 281.6, 538.0)),
            "substitute_or_add": (0, (230.4, 122.0, 238.4, 130.0)),
            "sign_return": (0, (381.6, 122.0, 389.6, 130.0)),
            "retention_revocation": (1, (568.0, 651.0, 576.0, 659.0)),
        }
        page_idx, (x0, y0, x1, y1) = pdf_rects[logical_key]
        return page_idx, fitz.Rect(x0, 792.0 - y1, x1, 792.0 - y0)

    def _assert_x_state(self, doc: fitz.Document, logical_key: str, checked: bool):
        page_idx, rect = self._checkbox_rect(logical_key)
        words = doc[page_idx].get_text("words")
        pad = 2.0
        hits = [
            w for w in words
            if w[4] == "X"
            and rect.x0 - pad <= w[0] and w[2] <= rect.x1 + pad
            and rect.y0 - pad <= w[1] and w[3] <= rect.y1 + pad
        ]
        assert bool(hits) == checked, (
            f"{logical_key}: expected checked={checked}, X-hits={hits}"
        )

    def test_render_success_real_roundtrip(self, sample_inputs):
        pdf_bytes, content_type = render_2848(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs=sample_inputs,
        )
        assert content_type == "application/pdf"
        assert pdf_bytes.startswith(b"%PDF")
        assert b"DRAFT" in pdf_bytes  # watermark path intact

        # --- pymupdf readback: bake, then extract text over BOTH pages ---
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        assert doc.page_count == 2
        fitz_text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        for value in self._EXPECTED_VISIBLE:
            assert value in fitz_text, f"{value!r} missing from baked text"
        for value in self._NEVER_VISIBLE:
            assert value not in fitz_text, f"{value!r} must never render"
        assert "DRAFT" in fitz_text

        # Checked boxes bake an X inside their own widget rect; unchecked and
        # falsy boxes stay empty.
        for key in ("access_records", "authorize_disclosure",
                    "rep1_sent_copies", "retention_revocation"):
            self._assert_x_state(doc, key, checked=True)
        for key in ("substitute_or_add", "sign_return"):
            self._assert_x_state(doc, key, checked=False)

        # The combined SSN-formatted TIN lands in the SSN sub-box (x~338-418),
        # not the EIN sub-box (x~500-576).
        tin_words = [w for w in doc[0].get_text("words") if w[4] == "123-45-6789"]
        assert tin_words and 330.0 <= tin_words[0][0] <= 420.0

        # --- pypdf readback of the same final artifact ---
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pypdf_text = "\n".join(p.extract_text() for p in reader.pages)
        for value in self._EXPECTED_VISIBLE:
            assert value in pypdf_text
        for value in self._NEVER_VISIBLE:
            assert value not in pypdf_text
        # House invariant: the output is FLATTENED (values are baked page
        # content, not a fragile fillable layer) — so no form fields remain.
        assert not reader.get_fields()

    def test_combined_tin_routes_ein_format_to_ein_box(self):
        pdf_bytes, _ = render_2848(
            tax_year=None,
            watermark_mode=WatermarkMode.NONE,
            inputs={"taxpayer_ssn_ein": _sr("12-3456789")},
        )
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        words = [w for w in doc[0].get_text("words") if w[4] == "12-3456789"]
        assert words and 495.0 <= words[0][0] <= 576.0  # EIN sub-box

    def test_explicit_tin_keys_win_over_combined_key(self):
        pdf_bytes, _ = render_2848(
            tax_year=None,
            watermark_mode=WatermarkMode.NONE,
            inputs={
                "taxpayer_ssn_ein": _sr("111-22-3333"),
                "taxpayer_ssn": _sr("999-88-7777"),
            },
        )
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        text = doc[0].get_text()
        assert "999-88-7777" in text
        assert "111-22-3333" not in text

    def test_watermark_none_and_city_state_zip_alone(self):
        """NONE leaves the PDF unstamped, and a city/state/ZIP supplied without
        a street line still lands in the (single) address box."""
        pdf_bytes, content_type = render_2848(
            tax_year=2025,
            watermark_mode=WatermarkMode.NONE,
            inputs={
                "taxpayer_name": _sr("Avery T. Quimby"),
                "taxpayer_city_state_zip": _sr("Folly Beach, SC 29439"),
            },
        )
        assert content_type == "application/pdf"
        assert b"DRAFT" not in pdf_bytes
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        text = doc[0].get_text()
        assert "DRAFT" not in text
        assert "Folly Beach, SC 29439" in text

    def test_unknown_keys_blanks_and_malformed_payloads_are_skipped(self):
        pdf_bytes, _ = render_2848(
            tax_year=2025,
            watermark_mode=WatermarkMode.NONE,
            inputs={
                "taxpayer_name": _sr("Solo Filer"),
                "not_a_real_key": _sr("SHOULD-NOT-APPEAR"),
                "rep1_caf": _sr(""),  # blank -> skipped
                "rep1_ptin": _sr("   "),  # whitespace-only -> skipped
                "acts_years_1": {"malformed": True},  # non-conforming payload
                "rep2_name": "not-a-dict",  # non-dict payload
                "access_records": _sr("no"),  # falsy -> box stays off
            },
        )
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        assert "Solo Filer" in text
        assert "SHOULD-NOT-APPEAR" not in text
        self._assert_x_state(doc, "access_records", checked=False)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------
class TestF2848Registry:
    def test_registered_and_dispatches(self):
        from app.exports.form_registry import get_spec, render_form

        spec = get_spec("2848")
        assert spec is not None
        assert spec.mode == "fill"
        assert spec.module == "app.exports.f2848_fill"
        assert spec.func == "render_2848"
        assert spec.jurisdiction == "federal"

        pdf_bytes, content_type = render_form(
            "2848",
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={"taxpayer_name": _sr("Via Registry")},
        )
        assert content_type == "application/pdf"
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        assert "Via Registry" in doc[0].get_text()
