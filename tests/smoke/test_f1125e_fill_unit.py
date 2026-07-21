"""Unit tests for the Form 1125-E (Compensation of Officers) renderer.

State-independence rules
------------------------
- The missing-PDF test monkeypatches the module's forms-dir resolution to a
  ``tmp_path`` — it never depends on whether the repo has the PDF.
- The success tests use the real ``data/irs_forms/f1125e_2024.pdf`` and assert
  a REAL round-trip (no skips): pypdf ``get_fields()`` values at the
  pre-flatten stage, plus baked-text readback of the final artifact via BOTH
  pymupdf (``bake()`` + ``get_text()``) and pypdf (``extract_text()``). The
  final artifact is deliberately FLATTENED by the house renderer
  (``_render_acroform_pdf``), so ``get_fields()`` on the output must be empty —
  that flattening is itself asserted as an invariant.
"""

from __future__ import annotations

import io

import fitz  # PyMuPDF
import pytest
from pypdf import PdfReader, PdfWriter

import app.exports.f1125e_fill as f1125e_fill
from app.exports.f1125e_field_map import (
    F1125E_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
    VALID_F1125E_FIELDS,
)
from app.exports.f1125e_fill import _f1125e_form_path, render_1125e
from app.exports.form_prefill import WatermarkMode, _set_acroform_values


def _sr(value: str) -> dict:
    return {"value": value, "source_ref": "test#1", "origin": "user_entered"}


# ---------------------------------------------------------------------------
# Field map consistency (pure — no PDF needed)
# ---------------------------------------------------------------------------
class TestF1125EFieldMap:
    def test_full_shape(self):
        # header (2) + 20 rows x 6 cols (120) + totals (3) = 125, all unique.
        assert len(F1125E_FIELDS) == 125
        assert len(set(F1125E_FIELDS.values())) == 125
        assert VALID_F1125E_FIELDS == frozenset(F1125E_FIELDS.values())

    def test_officer_rows_are_nested_under_body_rows(self):
        """The officer table is NOT flat under Page1 — the fully-qualified name
        runs through Line1Table[0].BodyRow{N}[0] (the exact-match field-tree
        walker fills nothing if this segment is missing)."""
        for row in range(1, 21):
            base = 3 + 6 * (row - 1)
            for i, col in enumerate(
                ("name", "ssn", "pct_time", "common", "preferred", "comp")
            ):
                acro = F1125E_FIELDS[f"officer_{row}_{col}"]
                assert acro == (
                    "topmostSubform[0].Page1[0].Line1Table[0]"
                    f".BodyRow{row}[0].f1_{base + i}[0]"
                )

    def test_header_and_totals_are_direct_children_of_page1(self):
        assert F1125E_FIELDS["corporation_name"] == "topmostSubform[0].Page1[0].f1_1[0]"
        assert F1125E_FIELDS["ein"] == "topmostSubform[0].Page1[0].f1_2[0]"
        assert F1125E_FIELDS["line_2_total"] == "topmostSubform[0].Page1[0].f1_123[0]"
        assert F1125E_FIELDS["line_3_claimed"] == "topmostSubform[0].Page1[0].f1_124[0]"
        assert F1125E_FIELDS["line_4_net"] == "topmostSubform[0].Page1[0].f1_125[0]"

    def test_taxpayer_completed_keys(self):
        assert TAXPAYER_COMPLETED_KEYS == frozenset(
            f"officer_{row}_ssn" for row in range(1, 21)
        )
        assert "officer_1_name" not in TAXPAYER_COMPLETED_KEYS


# ---------------------------------------------------------------------------
# Ground truth: the map must match the shipped PDF exactly
# ---------------------------------------------------------------------------
class TestF1125EMapMatchesPdf:
    def test_map_is_exact_bijection_with_pdf_terminal_fields(self):
        source = _f1125e_form_path(2024)
        assert source.exists(), (
            f"{source} missing — run scripts/download_irs_forms.sh"
        )
        reader = PdfReader(str(source))
        fields = reader.get_fields() or {}
        terminals = {n for n, f in fields.items() if f.get("/FT") is not None}
        assert set(F1125E_FIELDS.values()) == terminals
        # The 1125-E is all text fields — no checkboxes to mis-set.
        assert all(
            fields[n].get("/FT") == "/Tx" for n in F1125E_FIELDS.values()
        )


# ---------------------------------------------------------------------------
# Renderer behavior
# ---------------------------------------------------------------------------
class TestF1125ERender:
    @pytest.fixture
    def sample_inputs(self):
        return {
            "corporation_name": _sr("Quimby Analytics, Inc."),
            "ein": _sr("12-3456789"),
            "officers": [
                {
                    "name": _sr("Avery T. Quimby"),
                    "ssn": _sr("987-65-4321"),  # must be LEFT BLANK by renderer
                    "pct_time": _sr("100"),
                    "common": _sr("62"),
                    "preferred": _sr("55"),
                    "comp": _sr("185000"),
                },
                {
                    "name": _sr("Rosa L. Ferreira"),
                    "pct_time": _sr("80"),
                    "common": _sr("38"),
                    "comp": _sr("97500"),
                },
            ],
            "line_2_total": _sr("282500"),
            "line_3_claimed": _sr("12750"),
            "line_4_net": _sr("269750"),
        }

    def _expected_visible_values(self, sample_inputs) -> list[str]:
        vals = [
            sample_inputs["corporation_name"]["value"],
            sample_inputs["ein"]["value"],
            sample_inputs["line_2_total"]["value"],
            sample_inputs["line_3_claimed"]["value"],
            sample_inputs["line_4_net"]["value"],
        ]
        for officer in sample_inputs["officers"]:
            for col, payload in officer.items():
                if col == "ssn":
                    continue  # never rendered
                vals.append(payload["value"])
        return vals

    def test_missing_pdf_raises_regardless_of_repo_state(
        self, tmp_path, monkeypatch
    ):
        """Resolution is redirected to an empty tmp dir, so this passes whether
        or not the repo currently has the real PDF."""
        monkeypatch.setattr(f1125e_fill, "IRS_FORMS_DIR", tmp_path)
        with pytest.raises(FileNotFoundError, match="1125-E"):
            render_1125e(
                tax_year=2024,
                watermark_mode=WatermarkMode.DRAFT,
                inputs={"corporation_name": _sr("Nope Corp")},
            )

    def test_year_specific_path_preferred_then_fallback(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(f1125e_fill, "IRS_FORMS_DIR", tmp_path)
        fallback = tmp_path / "f1125e.pdf"
        fallback.write_bytes(b"%PDF-1.4 placeholder")
        assert _f1125e_form_path(2024) == fallback  # no year file -> fallback
        year_file = tmp_path / "f1125e_2024.pdf"
        year_file.write_bytes(b"%PDF-1.4 placeholder")
        assert _f1125e_form_path(2024) == year_file  # year file wins
        assert _f1125e_form_path(None) == fallback  # no year -> unversioned

    def test_acroform_get_fields_roundtrip_of_mapped_values(self, sample_inputs):
        """Prove the map's fully-qualified names really address the PDF's field
        tree: set /V through the house walker on a clone of the source PDF
        (pre-flatten stage) and read every value back via pypdf get_fields()."""
        source = _f1125e_form_path(2024)
        assert source.exists()

        expected_fields = {
            F1125E_FIELDS["corporation_name"]: "Quimby Analytics, Inc.",
            F1125E_FIELDS["ein"]: "12-3456789",
            F1125E_FIELDS["officer_1_name"]: "Avery T. Quimby",
            F1125E_FIELDS["officer_1_pct_time"]: "100",
            F1125E_FIELDS["officer_1_common"]: "62",
            F1125E_FIELDS["officer_1_preferred"]: "55",
            F1125E_FIELDS["officer_1_comp"]: "185000",
            F1125E_FIELDS["officer_2_name"]: "Rosa L. Ferreira",
            F1125E_FIELDS["officer_2_pct_time"]: "80",
            F1125E_FIELDS["officer_2_common"]: "38",
            F1125E_FIELDS["officer_2_comp"]: "97500",
            F1125E_FIELDS["line_2_total"]: "282500",
            F1125E_FIELDS["line_3_claimed"]: "12750",
            F1125E_FIELDS["line_4_net"]: "269750",
        }

        writer = PdfWriter(clone_from=io.BytesIO(source.read_bytes()))
        set_count = _set_acroform_values(writer, dict(expected_fields))
        assert set_count == len(expected_fields)

        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        readback = PdfReader(buf).get_fields()
        for acro_name, value in expected_fields.items():
            assert str(readback[acro_name].get("/V")) == value

    def test_render_success_real_roundtrip(self, sample_inputs):
        pdf_bytes, content_type = render_1125e(
            tax_year=2024,
            watermark_mode=WatermarkMode.DRAFT,
            inputs=sample_inputs,
        )
        assert content_type == "application/pdf"
        assert pdf_bytes.startswith(b"%PDF")

        expected = self._expected_visible_values(sample_inputs)

        # --- pymupdf readback: bake annotations, then extract page text ---
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        fitz_text = doc[0].get_text()
        for value in expected:
            assert value in fitz_text, f"{value!r} missing from baked text"
        # Officer SSN is an identity item — NEVER rendered even though supplied.
        assert "987-65-4321" not in fitz_text
        # DRAFT watermark path intact.
        assert "DRAFT" in fitz_text

        # --- pypdf readback of the same final artifact ---
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pypdf_text = reader.pages[0].extract_text()
        for value in expected:
            assert value in pypdf_text
        assert "987-65-4321" not in pypdf_text
        # House invariant: the output is FLATTENED (values are baked page
        # content, not a fragile fillable layer) — so no form fields remain.
        assert not reader.get_fields()

    def test_unknown_keys_and_blanks_are_skipped_not_fatal(self):
        pdf_bytes, _ = render_1125e(
            tax_year=2024,
            watermark_mode=WatermarkMode.NONE,
            inputs={
                "corporation_name": _sr("Solo Corp"),
                "not_a_real_key": _sr("SHOULD-NOT-APPEAR"),
                "ein": _sr(""),  # blank -> skipped
                "line_2_total": {"malformed": True},  # non-dict payload shape
                "officers": [
                    {"name": _sr("Only Officer"), "bogus_col": _sr("NOPE")},
                    "not-a-dict",
                ],
            },
        )
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        text = doc[0].get_text()
        assert "Solo Corp" in text
        assert "Only Officer" in text
        assert "SHOULD-NOT-APPEAR" not in text
        assert "NOPE" not in text

    def test_officer_21_plus_ignored(self):
        """The printed table has exactly 20 rows — extras must not bleed."""
        officers = [{"name": _sr(f"Officer Row {n:02d}")} for n in range(1, 23)]
        pdf_bytes, _ = render_1125e(
            tax_year=2024,
            watermark_mode=WatermarkMode.NONE,
            inputs={"officers": officers},
        )
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        doc.bake()
        text = doc[0].get_text()
        assert "Officer Row 20" in text
        assert "Officer Row 21" not in text
        assert "Officer Row 22" not in text
