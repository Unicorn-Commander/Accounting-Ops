"""Smoke tests for the CA Form 540 professional-substitute path.

Covers the field-map consistency (names qualified, no dups, VALID superset), the
radio-mark coordinate overlay, and — when the gitignored FTB PDF is present —
the end-to-end render (text values land on the right page, conservative
blank-fill, filing-status radio marked, watermark by status). Render tests skip
when data/state_forms/ca_540_2024.pdf is absent (as in CI), mirroring
``test_w4p.py``'s ``_needs_pdf`` guard. Values are flattened into page content,
so assertions read EXTRACTED TEXT (not /V).
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.ca_540_fill import (
    _ca540_form_path,
    _stamp_radio_marks,
    render_ca_540,
)
from app.exports.ca_540_field_map import (
    CA_540_CHECKBOX_FIELDS,
    CA_540_FIELDS,
    CA_540_RADIO_FIELDS,
    CA_540_RADIO_FIELD_NAMES,
    VALID_CA_540_FIELDS,
)

_CA540_PDF = _ca540_form_path(2024)


def _needs_pdf():
    if not _CA540_PDF.exists():
        pytest.skip("CA 540 PDF absent (gitignored) — skipping render test")


def _prov(value: str) -> dict:
    """A minimal provenance-bearing input record."""
    return {"value": value, "source_ref": "test#1", "origin": "user_entered"}


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in CA_540_FIELDS.items():
            assert name.startswith("540-"), f"{key} -> {name!r}"
            # text fields are plain "540-NNNN" (buttons carry a CB/RB suffix)
            assert not name.endswith(("CB", "RB")), f"{key} -> {name!r}"

    def test_checkbox_and_radio_names_qualified(self):
        for key, (name, on) in CA_540_CHECKBOX_FIELDS.items():
            assert name.startswith("540-") and name.endswith("CB"), f"{key} -> {name!r}"
            assert on == "Yes"
        for key, name in CA_540_RADIO_FIELD_NAMES.items():
            assert name.startswith("540-") and name.endswith("RB"), f"{key} -> {name!r}"

    def test_no_duplicate_field_names(self):
        names = (
            list(CA_540_FIELDS.values())
            + [v[0] for v in CA_540_CHECKBOX_FIELDS.values()]
            + list(CA_540_RADIO_FIELD_NAMES.values())
        )
        seen: dict[str, int] = {}
        for n in names:
            assert n not in seen, f"duplicate field name {n!r}"
            seen[n] = 1

    def test_valid_fields_superset(self):
        assert set(CA_540_FIELDS.values()) <= VALID_CA_540_FIELDS
        assert {v[0] for v in CA_540_CHECKBOX_FIELDS.values()} <= VALID_CA_540_FIELDS
        assert set(CA_540_RADIO_FIELD_NAMES.values()) <= VALID_CA_540_FIELDS

    def test_filing_status_has_five_options_on_side1(self):
        fs = CA_540_RADIO_FIELDS["filing_status"]
        assert set(fs) == {"single", "mfj", "mfs", "hoh", "qss"}
        for option, (page_index, rect) in fs.items():
            assert page_index == 0, f"{option} not on Side 1"
            assert len(rect) == 4

    def test_every_radio_group_has_a_parent_name(self):
        # Each option-rect group is documented by a parent "540-NNNN RB" name.
        assert set(CA_540_RADIO_FIELDS) == set(CA_540_RADIO_FIELD_NAMES)

    def test_essential_lines_present(self):
        # A few load-bearing lines a preparer always fills.
        for key in (
            "first_name", "last_name", "line13_federal_agi",
            "line19_taxable_income", "line64_total_tax",
            "line71_ca_withholding", "line111_amount_you_owe", "line115_refund",
        ):
            assert key in CA_540_FIELDS


# ---------------------------------------------------------------------------
# Radio-mark coordinate overlay (no FTB PDF needed — uses a blank page)
# ---------------------------------------------------------------------------

class TestRadioOverlay:
    def test_stamp_radio_mark_draws_single_x(self):
        from pypdf import PdfReader, PdfWriter

        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        buf = io.BytesIO()
        w.write(buf)
        buf.seek(0)

        rect = CA_540_RADIO_FIELDS["filing_status"]["mfj"][1]
        out = _stamp_radio_marks(buf.read(), [(0, rect)])
        data = PdfReader(io.BytesIO(out)).pages[0].get_contents().get_data().decode("latin-1")
        assert data.count("(X) Tj") == 1


# ---------------------------------------------------------------------------
# Render (requires the FTB PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # Flattened values render in EVERY viewer; assert on extracted text.
        _needs_pdf()
        from pypdf import PdfReader

        pdf, ct = render_ca_540(
            2024, WatermarkMode.DRAFT,
            {
                "first_name": _prov("Aaron"),
                "last_name": _prov("Stransky"),
                "line13_federal_agi": _prov("12345"),     # Side 2
                "line19_taxable_income": _prov("67890"),  # Side 2
                "line64_total_tax": _prov("54321"),       # Side 3
                "line115_refund": _prov("24680"),         # Side 5
                "filing_status": _prov("mfj"),
            },
        )
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        reader = PdfReader(io.BytesIO(pdf))
        assert len(reader.pages) == 6
        p0 = reader.pages[0].extract_text()
        p1 = reader.pages[1].extract_text()
        p2 = reader.pages[2].extract_text()
        p4 = reader.pages[4].extract_text()
        assert "Aaron" in p0 and "Stransky" in p0
        assert "12345" in p1 and "67890" in p1
        assert "54321" in p2
        assert "24680" in p4

    def test_conservative_blank_fill(self):
        _needs_pdf()
        from pypdf import PdfReader

        pdf, _ = render_ca_540(
            2024, WatermarkMode.DRAFT, {"last_name": _prov("Solo")}
        )
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        assert "Solo" in text
        # A distinctive amount we never supplied must not appear anywhere.
        assert "1357924680" not in text

    def test_unknown_key_is_skipped(self):
        _needs_pdf()
        # An unmapped logical key must be ignored, not raise.
        pdf, _ = render_ca_540(
            2024, WatermarkMode.DRAFT,
            {"not_a_real_line": _prov("999"), "first_name": _prov("Pat")},
        )
        assert pdf[:4] == b"%PDF"

    def test_filing_status_radio_marks_one_box(self):
        # Rendering with a filing status adds exactly one extra checkbox "X" to
        # Side 1 vs. rendering without it (the radio coordinate overlay fired).
        _needs_pdf()
        from pypdf import PdfReader

        def side1_x_count(inputs):
            pdf, _ = render_ca_540(2024, WatermarkMode.NONE, inputs)
            data = PdfReader(io.BytesIO(pdf)).pages[0].get_contents().get_data()
            return data.decode("latin-1").count("(X) Tj")

        base = side1_x_count({"first_name": _prov("Pat")})
        with_status = side1_x_count(
            {"first_name": _prov("Pat"), "filing_status": _prov("single")}
        )
        assert with_status == base + 1

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"last_name": _prov("Solo"), "filing_status": _prov("hoh")}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_ca_540(2024, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_missing_pdf_raises(self):
        # When no source PDF resolves, the renderer raises a clear error.
        if _CA540_PDF.exists():
            pytest.skip("PDF present — cannot exercise the missing-PDF path")
        with pytest.raises(FileNotFoundError):
            render_ca_540(2024, WatermarkMode.DRAFT, {"last_name": _prov("X")})
