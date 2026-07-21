"""Tests for Form 8821 (Tax Information Authorization) renderer.

Ground truth: ``data/irs_forms/f8821.pdf`` (Rev. January 2021) as dumped with
``pypdf`` — the map tests below re-verify every mapped AcroForm name, type,
and checkbox on-state against the shipped PDF on every run, and the render
tests assert a real round-trip (baked text read back with pymupdf, per-widget
rect placement included). The missing-PDF tests monkeypatch the module's
forms-dir resolution so they never depend on repo state.

Note on read-back: ``_render_acroform_pdf`` FLATTENS its output (widgets and
the AcroForm are deliberately dropped so values are baked, print-safe page
content). ``pypdf.get_fields()`` therefore validates the map against the
SOURCE PDF, while the OUTPUT is asserted via ``fitz`` ``bake()`` +
``get_text()`` — including clip reads at each source widget's rect to prove
placement, not just presence.
"""

from __future__ import annotations

import io
from pathlib import Path

import fitz  # pymupdf
import pytest
from pypdf import PdfReader

from app.exports.form_prefill import WatermarkMode
from app.exports.f8821_fill import render_8821, _f8821_form_path
from app.exports.f8821_field_map import (
    F8821_FIELDS,
    F8821_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
    VALID_F8821_FIELDS,
)

IRS_FORMS_DIR = Path(__file__).parent.parent.parent / "data" / "irs_forms"
F8821_PDF = IRS_FORMS_DIR / "f8821.pdf"


def _inp(value: str) -> dict:
    return {"value": value, "source_ref": "test#1", "origin": "user_entered"}


# Realistic synthetic fill for every mapped text field (unique values so the
# round-trip containment assertions are meaningful).
FULL_TEXT_INPUTS: dict[str, str] = {
    "taxpayer_name_address":  "Riverbend Trading Inc, 88 Wharf St, Folly Beach SC 29439",
    "taxpayer_tin":           "82-4431907",
    "taxpayer_phone":         "843-555-0142",
    "taxpayer_plan_number":   "003",
    "designee_name_address":  "Marta Voss CPA, 12 Elm Ct, Columbia SC 29201",
    "designee_caf":           "3102-88741R",
    "designee_ptin":          "P04471263",
    "designee_phone":         "803-555-0177",
    "designee_fax":           "803-555-0178",
    "designee2_name_address": "Hollis & Grey EA LLP, 9 Bay Rd, Charleston SC 29401",
    "designee2_caf":          "5560-12904R",
    "designee2_ptin":         "P09218347",
    "designee2_phone":        "854-555-0119",
    "designee2_fax":          "854-555-0120",
    "tax_type_1":             "Income",
    "tax_form_1":             "1120",
    "tax_years_1":            "2023, 2024",
    "tax_matters_1":          "Balance due inquiries",
    "tax_type_2":             "Employment",
    "tax_form_2":             "941",
    "tax_years_2":            "Q1-Q4 2024",
    "tax_matters_2":          "Deposit penalty abatement",
    "tax_type_3":             "Civil Penalty",
    "tax_form_3":             "8278",
    "tax_years_3":            "2022",
    "tax_matters_3":          "Sec. 6721 information returns",
    "taxpayer_print_name":    "Dana R. Whitfield",
    "taxpayer_title":         "President",
}


def _render_full(watermark_mode: WatermarkMode = WatermarkMode.NONE) -> bytes:
    inputs = {k: _inp(v) for k, v in FULL_TEXT_INPUTS.items()}
    for cb_key in F8821_CHECKBOX_FIELDS:
        inputs[cb_key] = _inp("true")
    pdf_bytes, content_type = render_8821(
        tax_year=2025, watermark_mode=watermark_mode, inputs=inputs
    )
    assert content_type == "application/pdf"
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 0
    return pdf_bytes


def _baked_page(pdf_bytes: bytes) -> fitz.Page:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    doc.bake()
    return doc[0]


def _source_widget_rects() -> dict[str, fitz.Rect]:
    """Fully-qualified field name -> widget rect (fitz coords) from the SOURCE."""
    reader = PdfReader(str(F8821_PDF))
    page = reader.pages[0]
    height = float(page.mediabox.height)
    rects: dict[str, fitz.Rect] = {}
    for ref in page.get("/Annots") or []:
        obj = ref.get_object()
        if obj.get("/Subtype") != "/Widget":
            continue
        parts: list[str] = []
        node = obj
        while node is not None:
            t = node.get("/T")
            if t is not None:
                parts.append(str(t))
            parent = node.get("/Parent")
            node = parent.get_object() if parent is not None else None
        name = ".".join(reversed(parts))
        x0, y0, x1, y1 = (float(v) for v in obj["/Rect"])
        # PDF origin is bottom-left; fitz origin is top-left.
        rects[name] = fitz.Rect(x0, height - max(y0, y1), x1, height - min(y0, y1))
    return rects


# ---------------------------------------------------------------------------
# Field-map integrity (no PDF required)
# ---------------------------------------------------------------------------

class TestF8821MapIntegrity:
    def test_no_duplicate_acroform_targets(self):
        targets = list(F8821_FIELDS.values()) + [
            v[0] for v in F8821_CHECKBOX_FIELDS.values()
        ]
        assert len(targets) == len(set(targets)), "two logical keys share a field"

    def test_targets_are_page1_and_never_the_irs_use_box(self):
        for name in VALID_F8821_FIELDS:
            assert name.startswith("topmostSubform[0].Page1[0]."), name
            assert "Pg1Header" not in name, (
                f"{name} targets the 'For IRS Use Only' box — never write there"
            )

    def test_valid_fields_is_the_union_of_both_maps(self):
        expected = set(F8821_FIELDS.values()) | {
            v[0] for v in F8821_CHECKBOX_FIELDS.values()
        }
        assert VALID_F8821_FIELDS == frozenset(expected)

    def test_taxpayer_completed_keys_are_not_mapped(self):
        for key in TAXPAYER_COMPLETED_KEYS:
            assert key not in F8821_FIELDS
            assert key not in F8821_CHECKBOX_FIELDS


# ---------------------------------------------------------------------------
# Ground truth: every mapped name/type/on-state exists in the shipped PDF
# ---------------------------------------------------------------------------

class TestF8821SourcePdfGroundTruth:
    def test_source_pdf_is_shipped(self):
        assert F8821_PDF.exists(), (
            f"{F8821_PDF} missing — run scripts/download_irs_forms.sh"
        )

    def test_every_mapped_text_field_exists_as_tx(self):
        fields = PdfReader(str(F8821_PDF)).get_fields()
        for logical, name in F8821_FIELDS.items():
            assert name in fields, f"{logical}: {name} not in PDF"
            assert fields[name].get("/FT") == "/Tx", f"{logical}: {name} not /Tx"

    def test_every_mapped_checkbox_exists_with_declared_on_state(self):
        fields = PdfReader(str(F8821_PDF)).get_fields()
        for logical, (name, on_state) in F8821_CHECKBOX_FIELDS.items():
            assert name in fields, f"{logical}: {name} not in PDF"
            assert fields[name].get("/FT") == "/Btn", f"{logical}: {name} not /Btn"
            states = fields[name].get("/_States_") or []
            assert f"/{on_state}" in [str(s) for s in states], (
                f"{logical}: on-state /{on_state} not in {states}"
            )


# ---------------------------------------------------------------------------
# Missing-PDF behavior — monkeypatched, never depends on repo state
# ---------------------------------------------------------------------------

class TestRender8821MissingPdf:
    def test_raises_file_not_found_when_forms_dir_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.exports.f8821_fill.IRS_FORMS_DIR", tmp_path)
        with pytest.raises(FileNotFoundError, match="8821"):
            render_8821(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs={"taxpayer_tin": _inp("82-4431907")},
            )

    def test_year_specific_pdf_is_preferred(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.exports.f8821_fill.IRS_FORMS_DIR", tmp_path)
        (tmp_path / "f8821.pdf").write_bytes(b"%PDF-fallback")
        (tmp_path / "f8821_2025.pdf").write_bytes(b"%PDF-year")
        assert _f8821_form_path(2025) == tmp_path / "f8821_2025.pdf"
        assert _f8821_form_path(2024) == tmp_path / "f8821.pdf"
        assert _f8821_form_path(None) == tmp_path / "f8821.pdf"


# ---------------------------------------------------------------------------
# Real round-trip through the public renderer (repo PDF is a shipped fixture;
# these tests FAIL — not skip — if it has been removed)
# ---------------------------------------------------------------------------

class TestRender8821RoundTrip:
    def test_full_fill_values_all_appear_and_sit_in_their_boxes(self):
        pdf_bytes = _render_full()

        # The house renderer flattens: the output must carry NO live AcroForm
        # (values are baked into page content, print-safe everywhere).
        out_fields = PdfReader(io.BytesIO(pdf_bytes)).get_fields()
        assert not out_fields, "output should be flattened (no live form fields)"

        page = _baked_page(pdf_bytes)
        text = page.get_text()
        for logical, value in FULL_TEXT_INPUTS.items():
            assert value in text, f"{logical}: {value!r} missing from baked text"

        # Placement: each value must appear INSIDE its own widget rect (clip
        # read at the source widget's rect), and each checkbox rect shows "X".
        rects = _source_widget_rects()
        for logical, value in FULL_TEXT_INPUTS.items():
            rect = rects[F8821_FIELDS[logical]] + (-3, -3, 3, 3)
            clipped = page.get_text(clip=rect)
            assert value.split(",")[0] in clipped, (
                f"{logical}: {value!r} not inside its widget rect {rect}"
            )
        for logical, (name, _on) in F8821_CHECKBOX_FIELDS.items():
            rect = rects[name] + (-3, -3, 3, 3)
            assert "X" in page.get_text(clip=rect), (
                f"{logical}: checkbox {name} not marked"
            )

    def test_irs_use_only_header_box_stays_untouched(self):
        page = _baked_page(_render_full())
        # "For IRS Use Only" box (top-right; pdf y 690-745 -> fitz y h-745..h-690).
        h = page.rect.height
        clipped = page.get_text(clip=fitz.Rect(495, h - 745, 580, h - 688))
        for value in FULL_TEXT_INPUTS.values():
            assert value not in clipped, f"{value!r} leaked into the IRS-use-only box"
        assert "X" not in clipped

    def test_draft_watermark_present_and_none_absent(self):
        draft_text = _baked_page(_render_full(WatermarkMode.DRAFT)).get_text()
        assert "DRAFT" in draft_text
        clean_text = _baked_page(_render_full(WatermarkMode.NONE)).get_text()
        assert "DRAFT" not in clean_text
        # The fill itself must survive watermarking.
        assert FULL_TEXT_INPUTS["taxpayer_tin"] in draft_text

    def test_blank_unknown_and_falsy_checkbox_inputs_are_skipped(self):
        pdf_bytes, _ = render_8821(
            tax_year=2025,
            watermark_mode=WatermarkMode.NONE,
            inputs={
                "taxpayer_tin": _inp("82-4431907"),
                "taxpayer_phone": _inp(""),                     # blank -> skipped
                "not_a_real_8821_key": _inp("ROGUE-MARKER-77"),  # unknown -> skipped
                "taxpayer_plan_number": "not-a-dict",            # malformed -> skipped
                "designee_notice_copies": _inp("false"),         # falsy -> /Off
            },
        )
        page = _baked_page(pdf_bytes)
        text = page.get_text()
        assert "82-4431907" in text
        assert "ROGUE-MARKER-77" not in text
        rects = _source_widget_rects()
        cb_rect = rects[F8821_CHECKBOX_FIELDS["designee_notice_copies"][0]] + (-3, -3, 3, 3)
        assert "X" not in page.get_text(clip=cb_rect)

    def test_signature_and_date_are_never_filled(self):
        pdf_bytes, _ = render_8821(
            tax_year=2025,
            watermark_mode=WatermarkMode.NONE,
            inputs={
                "taxpayer_tin": _inp("82-4431907"),
                "taxpayer_signature": _inp("FORGED-SIGNATURE-MARKER"),
                "taxpayer_date": _inp("01/01/2099"),
            },
        )
        text = _baked_page(pdf_bytes).get_text()
        assert "FORGED-SIGNATURE-MARKER" not in text
        assert "01/01/2099" not in text
        assert "82-4431907" in text
