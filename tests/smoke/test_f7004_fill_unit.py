"""Unit tests for Form 7004 (business extension) renderer."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure app exports are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


class TestF7004FieldMap:
    """Verify field map structure and coverage."""

    def test_field_map_keys_exist(self):
        """All logical keys in F7004_FIELDS map to AcroForm names."""
        from app.exports.f7004_field_map import F7004_FIELDS, VALID_F7004_FIELDS

        assert isinstance(F7004_FIELDS, dict)
        assert len(F7004_FIELDS) > 0

        for key, acro_name in F7004_FIELDS.items():
            assert isinstance(key, str)
            assert isinstance(acro_name, str)
            assert acro_name in VALID_F7004_FIELDS

    def test_checkbox_field_map_keys_exist(self):
        """All logical keys in F7004_CHECKBOX_FIELDS map to AcroForm names."""
        from app.exports.f7004_field_map import F7004_CHECKBOX_FIELDS, VALID_F7004_FIELDS

        assert isinstance(F7004_CHECKBOX_FIELDS, dict)
        assert len(F7004_CHECKBOX_FIELDS) > 0

        for key, (acro_name, on_state) in F7004_CHECKBOX_FIELDS.items():
            assert isinstance(key, str)
            assert isinstance(acro_name, str)
            assert isinstance(on_state, str)
            assert acro_name in VALID_F7004_FIELDS
            assert on_state == "1"


class TestF7004Render:
    """Verify render_7004 behavior."""

    def test_render_missing_pdf_raises(self):
        """render_7004 raises FileNotFoundError when PDF is absent."""
        from app.exports.form_prefill import WatermarkMode
        from app.exports.f7004_fill import render_7004

        with pytest.raises(FileNotFoundError, match="IRS Form 7004 PDF not found"):
            render_7004(tax_year=2025, watermark_mode=WatermarkMode.DRAFT, inputs={})

    def test_render_skips_blank_values(self):
        """render_7004 skips inputs with blank/None values."""
        from app.exports.form_prefill import WatermarkMode
        from app.exports.f7004_fill import render_7004

        # Mock the PDF read and render function to avoid file I/O and PDF processing
        mock_source_bytes = b"fake_pdf_content"
        mock_render_result = (b"filled_pdf", "application/pdf")

        with patch("app.exports.f7004_fill._f7004_form_path") as mock_path, \
             patch("app.exports.f7004_fill._render_acroform_pdf") as mock_render:
            
            # Setup mock path to return a non-existent path (to trigger FileNotFoundError)
            # But we want to test the logic before that, so let's mock the path to exist
            mock_path.return_value = MagicMock()
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.read_bytes.return_value = mock_source_bytes
            
            mock_render.return_value = mock_render_result

            inputs = {
                "name": {"value": "", "source_ref": "test", "origin": "user"},
                "ein": {"value": None, "source_ref": "test", "origin": "user"},
                "form_code": {"value": "12", "source_ref": "test", "origin": "user"},
            }

            pdf_bytes, content_type = render_7004(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs=inputs,
            )

            assert pdf_bytes == b"filled_pdf"
            assert content_type == "application/pdf"
            
            # Verify that _render_acroform_pdf was called with only the non-blank value
            call_args = mock_render.call_args
            field_values = call_args[0][1]  # Second positional argument
            
            assert "name" not in field_values  # Skipped because blank
            assert "ein" not in field_values   # Skipped because None
            assert "form_code" in field_values # Included because "12"

    def test_render_checkbox_truthy(self):
        """render_7004 sets checkbox when value is truthy."""
        from app.exports.form_prefill import WatermarkMode
        from app.exports.f7004_fill import render_7004

        mock_source_bytes = b"fake_pdf_content"
        mock_render_result = (b"filled_pdf", "application/pdf")

        with patch("app.exports.f7004_fill._f7004_form_path") as mock_path, \
             patch("app.exports.f7004_fill._render_acroform_pdf") as mock_render:
            
            mock_path.return_value = MagicMock()
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.read_bytes.return_value = mock_source_bytes
            
            mock_render.return_value = mock_render_result

            inputs = {
                "foreign_corp_2": {"value": "true", "source_ref": "test", "origin": "user"},
            }

            render_7004(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs=inputs,
            )

            call_args = mock_render.call_args
            field_values = call_args[0][1]
            
            # Check that the checkbox field was set to "1"
            assert "topmostSubform[0].Page1[0].c1_1[0]" in field_values
            assert field_values["topmostSubform[0].Page1[0].c1_1[0]"] == "1"

    def test_render_checkbox_falsy(self):
        """render_7004 does not set checkbox when value is falsy."""
        from app.exports.form_prefill import WatermarkMode
        from app.exports.f7004_fill import render_7004

        mock_source_bytes = b"fake_pdf_content"
        mock_render_result = (b"filled_pdf", "application/pdf")

        with patch("app.exports.f7004_fill._f7004_form_path") as mock_path, \
             patch("app.exports.f7004_fill._render_acroform_pdf") as mock_render:
            
            mock_path.return_value = MagicMock()
            mock_path.return_value.exists.return_value = True
            mock_path.return_value.read_bytes.return_value = mock_source_bytes
            
            mock_render.return_value = mock_render_result

            inputs = {
                "foreign_corp_2": {"value": "false", "source_ref": "test", "origin": "user"},
            }

            render_7004(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs=inputs,
            )

            call_args = mock_render.call_args
            field_values = call_args[0][1]
            
            # Check that the checkbox field was NOT set
            assert "topmostSubform[0].Page1[0].c1_1[0]" not in field_values
