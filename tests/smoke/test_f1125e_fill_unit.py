"""Unit tests for Form 1125-E renderer."""

from __future__ import annotations

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.f1125e_fill import render_1125e
from app.exports.f1125e_field_map import F1125E_FIELDS, TAXPAYER_COMPLETED_KEYS


class TestF1125EFieldMap:
    def test_fields_exist(self):
        assert len(F1125E_FIELDS) > 0

    def test_taxpayer_completed_keys(self):
        assert "officer_1_ssn" in TAXPAYER_COMPLETED_KEYS
        assert "officer_20_ssn" in TAXPAYER_COMPLETED_KEYS
        assert "officer_1_name" not in TAXPAYER_COMPLETED_KEYS


class TestF1125ERender:
    @pytest.fixture
    def sample_inputs(self):
        return {
            "corporation_name": {"value": "Test Corp", "source_ref": "test", "origin": "user"},
            "ein": {"value": "12-3456789", "source_ref": "test", "origin": "user"},
            "officers": [
                {
                    "name": {"value": "John Doe", "source_ref": "test", "origin": "user"},
                    "ssn": {"value": "123-45-6789", "source_ref": "test", "origin": "user"},
                    "pct_time": {"value": "100", "source_ref": "test", "origin": "user"},
                    "common": {"value": "50", "source_ref": "test", "origin": "user"},
                    "preferred": {"value": "0", "source_ref": "test", "origin": "user"},
                    "comp": {"value": "100000", "source_ref": "test", "origin": "user"},
                },
            ],
            "line_2_total": {"value": "100000", "source_ref": "test", "origin": "user"},
            "line_3_claimed": {"value": "0", "source_ref": "test", "origin": "user"},
            "line_4_net": {"value": "100000", "source_ref": "test", "origin": "user"},
        }

    def test_render_1125e_skipped_if_pdf_missing(self):
        """If the PDF is not present, render should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            render_1125e(tax_year=2024, watermark_mode=WatermarkMode.DRAFT, inputs={})

    def test_render_1125e_success(self, sample_inputs, tmp_path):
        """Render should succeed if PDF is present."""
        # Create a dummy PDF file to satisfy the renderer
        from app.exports.form_prefill import IRS_FORMS_DIR
        dummy_pdf = IRS_FORMS_DIR / "f1125e_2024.pdf"
        dummy_pdf.parent.mkdir(parents=True, exist_ok=True)
        dummy_pdf.write_bytes(b"%PDF-1.4 dummy")

        try:
            pdf_bytes, content_type = render_1125e(
                tax_year=2024,
                watermark_mode=WatermarkMode.DRAFT,
                inputs=sample_inputs,
            )
            assert content_type == "application/pdf"
            assert len(pdf_bytes) > 0
        finally:
            dummy_pdf.unlink(missing_ok=True)
