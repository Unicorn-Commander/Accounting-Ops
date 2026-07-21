"""Unit tests for the Form W-9 renderer (``app.exports.fw9_fill``).

Ground truth: ``data/irs_forms/fw9.pdf`` (Rev. 3-2024). The renderer output is
FLATTENED by design (``_render_acroform_pdf`` bakes values into the page
content and drops the AcroForm), so the round-trip here is:

  - pypdf ``get_fields()`` against the SOURCE PDF pins every mapped
    fully-qualified name, its field type, and each checkbox on-state;
  - pypdf ``extract_text()`` + pymupdf ``bake()`` / ``get_text()`` against the
    RENDERED output prove every supplied value is visibly baked in — and
    pypdf ``get_fields()`` on the output proves the flattening invariant.

The missing-PDF test monkeypatches the module's forms-dir resolution to a
``tmp_path`` — it never renames or depends on the repo's real PDF.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import fitz  # pymupdf
import pytest
from pypdf import PdfReader

# Ensure app is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.exports.form_prefill import WatermarkMode
from app.exports.fw9_fill import (
    TAXPAYER_COMPLETED_KEYS,
    _fw9_form_path,
    render_w9,
)
from app.exports.fw9_field_map import (
    FW9_CHECKBOX_FIELDS,
    FW9_FIELDS,
    FW9_LLC_CLASSIFICATION,
    FW9_OTHER_DETAIL,
    FW9_RADIO_FIELDS,
    VALID_FW9_FIELDS,
)

SOURCE_PDF = Path(__file__).resolve().parent.parent.parent / "data" / "irs_forms" / "fw9.pdf"


def _inp(value: str) -> dict:
    return {"value": value, "source_ref": "test#1", "origin": "user_entered"}


def _page0_texts(pdf_bytes: bytes) -> tuple[str, str]:
    """Return (pypdf extract_text, pymupdf baked get_text) for page 0."""
    pypdf_text = PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    doc.bake()
    fitz_text = doc[0].get_text()
    doc.close()
    return pypdf_text, fitz_text


# ---------------------------------------------------------------------------
# 1. Field map pinned against the real PDF (names, types, on-states)
# ---------------------------------------------------------------------------

class TestFieldMapGroundTruth:
    def test_every_mapped_name_exists_in_source_pdf(self):
        fields = PdfReader(str(SOURCE_PDF)).get_fields()
        missing = sorted(n for n in VALID_FW9_FIELDS if n not in fields)
        assert not missing, f"Field-map names not present in fw9.pdf: {missing}"

    def test_text_fields_are_tx_and_unique(self):
        fields = PdfReader(str(SOURCE_PDF)).get_fields()
        for key, name in FW9_FIELDS.items():
            assert fields[name].get("/FT") == "/Tx", f"{key} -> {name} is not /Tx"
        for name in (FW9_LLC_CLASSIFICATION, FW9_OTHER_DETAIL):
            assert fields[name].get("/FT") == "/Tx"
        all_names = (
            list(FW9_FIELDS.values())
            + [v[0] for v in FW9_RADIO_FIELDS.values()]
            + [v[0] for v in FW9_CHECKBOX_FIELDS.values()]
            + [FW9_LLC_CLASSIFICATION, FW9_OTHER_DETAIL]
        )
        assert len(all_names) == len(set(all_names)), "duplicate AcroForm targets"

    def test_checkbox_on_states_match_pdf(self):
        """Each 3a/3b box is /Btn and our on-state is one of its /_States_."""
        fields = PdfReader(str(SOURCE_PDF)).get_fields()
        boxes = {**FW9_RADIO_FIELDS, **FW9_CHECKBOX_FIELDS}
        for key, (name, on_state) in boxes.items():
            f = fields[name]
            assert f.get("/FT") == "/Btn", f"{key} -> {name} is not /Btn"
            states = {str(s).lstrip("/") for s in (f.get("/_States_") or [])}
            assert on_state in states, (
                f"{key}: on-state {on_state!r} not in PDF states {states} for {name}"
            )

    def test_3a_on_states_are_distinct(self):
        """The seven 3a widgets carry distinct export values '1'..'7'."""
        states = [on for _, on in FW9_RADIO_FIELDS.values()]
        assert sorted(states) == [str(i) for i in range(1, 8)]


# ---------------------------------------------------------------------------
# 2. Path resolution (state-independent — never touches the repo PDF)
# ---------------------------------------------------------------------------

class TestFormPathResolution:
    def test_missing_pdf_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.exports.fw9_fill.IRS_FORMS_DIR", tmp_path)
        with pytest.raises(FileNotFoundError):
            render_w9(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs={"name": _inp("Test Person")},
            )

    def test_prefers_year_specific_pdf_then_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.exports.fw9_fill.IRS_FORMS_DIR", tmp_path)
        base = tmp_path / "fw9.pdf"
        base.write_bytes(b"%PDF-fake")
        assert _fw9_form_path(2025) == base  # no year file -> fallback
        year = tmp_path / "fw9_2025.pdf"
        year.write_bytes(b"%PDF-fake")
        assert _fw9_form_path(2025) == year  # year file wins
        assert _fw9_form_path(None) == base  # no year -> unversioned


# ---------------------------------------------------------------------------
# 3. Real render round-trips (require the repo PDF; they FAIL, never skip,
#    if it is absent — run scripts/download_irs_forms.sh)
# ---------------------------------------------------------------------------

REALISTIC_TEXT_INPUTS = {
    "name": "Jane Q. Contractor",
    "business_name": "Palmetto Web Studio LLC",
    "llc_classification": "P",
    "exempt_payee_code": "5",
    "fatca_exemption_code": "C",
    "address": "1234 Palmetto Ave., Apt. 5",
    "city_state_zip": "Columbia, SC 29201",
    # The requester box is ~186pt wide and the overlay draws ONE line — a
    # longer string would overflow the printed box (and the page edge).
    "requester_name_address": "Acme Payers, Charleston SC",
    "account_numbers": "VEND-88112",
}


class TestRenderW9:
    def test_full_fill_round_trip(self):
        """Every supplied value is readable back out of the rendered PDF via
        BOTH pypdf and pymupdf, and the output is flattened (no live form)."""
        inputs = {k: _inp(v) for k, v in REALISTIC_TEXT_INPUTS.items()}
        inputs["llc"] = _inp("true")
        pdf_bytes, content_type = render_w9(
            tax_year=2025, watermark_mode=WatermarkMode.NONE, inputs=inputs
        )
        assert content_type == "application/pdf"
        assert pdf_bytes.startswith(b"%PDF")

        pypdf_text, fitz_text = _page0_texts(pdf_bytes)
        for key, value in REALISTIC_TEXT_INPUTS.items():
            assert value in pypdf_text, f"{key}={value!r} missing (pypdf extract_text)"
            assert value in fitz_text, f"{key}={value!r} missing (pymupdf baked text)"

        # Flattening invariant: baked, print-ready output has NO live AcroForm.
        assert PdfReader(io.BytesIO(pdf_bytes)).get_fields() is None

    def test_checkbox_marks_are_baked(self):
        """Checking 3a-LLC + 3b adds exactly two X marks vs the same render
        without them (robust to X glyphs in the form's static text)."""
        base_inputs = {"name": _inp("Jane Q. Contractor")}
        checked_inputs = {
            "name": _inp("Jane Q. Contractor"),
            "llc": _inp("true"),
            "foreign_status": _inp("true"),
            "c_corporation": _inp("false"),  # falsy -> must NOT set the box
        }
        base_pdf, _ = render_w9(2025, WatermarkMode.NONE, base_inputs)
        checked_pdf, _ = render_w9(2025, WatermarkMode.NONE, checked_inputs)
        _, base_text = _page0_texts(base_pdf)
        _, checked_text = _page0_texts(checked_pdf)
        assert checked_text.count("X") == base_text.count("X") + 2

    def test_tin_is_never_auto_filled(self):
        """Part I TIN values (current + legacy keys) must not render even if
        supplied — the taxpayer enters their own identity items."""
        tin_values = {
            "ssn_part1": "771", "ssn_part2": "72", "ssn_part3": "7373",
            "ein_part1": "74", "ein_part2": "7575757",
            "tin_part1": "MXTIN1", "tin_part2": "MXTIN2",
        }
        assert set(tin_values) <= TAXPAYER_COMPLETED_KEYS
        inputs = {k: _inp(v) for k, v in tin_values.items()}
        inputs["name"] = _inp("Jane Q. Contractor")
        pdf_bytes, _ = render_w9(2025, WatermarkMode.NONE, inputs)
        pypdf_text, fitz_text = _page0_texts(pdf_bytes)
        assert "Jane Q. Contractor" in fitz_text  # render itself worked
        for key, value in tin_values.items():
            assert value not in pypdf_text, f"{key} leaked into output (pypdf)"
            assert value not in fitz_text, f"{key} leaked into output (pymupdf)"

    def test_unknown_and_blank_keys_are_skipped(self):
        """Unknown keys and blank values never reach the page; known values
        still render (conservative blank-fill)."""
        inputs = {
            "name": _inp("Jane Q. Contractor"),
            "business_name": _inp(""),  # blank -> left blank
            "not_a_w9_field": _inp("UNMAPPED-SENTINEL"),
            "malformed": "not-a-payload-dict",  # non-dict payload -> skipped
        }
        pdf_bytes, _ = render_w9(2025, WatermarkMode.NONE, inputs)
        pypdf_text, fitz_text = _page0_texts(pdf_bytes)
        assert "Jane Q. Contractor" in fitz_text
        assert "UNMAPPED-SENTINEL" not in fitz_text
        assert "UNMAPPED-SENTINEL" not in pypdf_text

    def test_watermark_modes(self):
        """DRAFT stamps the banner; NONE leaves the artifact unstamped."""
        inputs = {"name": _inp("Jane Q. Contractor")}
        draft_pdf, _ = render_w9(2025, WatermarkMode.DRAFT, inputs)
        clean_pdf, _ = render_w9(2025, WatermarkMode.NONE, inputs)
        _, draft_text = _page0_texts(draft_pdf)
        _, clean_text = _page0_texts(clean_pdf)
        assert "DRAFT" in draft_text
        assert "DRAFT" not in clean_text
