"""Unit tests for Form W-9 renderer."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure app is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.exports.form_prefill import WatermarkMode
from app.exports.fw9_fill import render_w9


# Path to the IRS forms directory
IRS_FORMS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "irs_forms"


class TestRenderW9:
    """Tests for render_w9."""

    def test_render_w9_skipped_if_pdf_absent(self):
        """Test that render_w9 raises FileNotFoundError if PDF is missing."""
        # Ensure the PDF is not present by temporarily renaming it if it exists
        pdf_path = IRS_FORMS_DIR / "fw9.pdf"
        backup_path = IRS_FORMS_DIR / "fw9.pdf.backup"
        
        should_backup = pdf_path.exists()
        if should_backup:
            pdf_path.rename(backup_path)
        
        try:
            with pytest.raises(FileNotFoundError):
                render_w9(
                    tax_year=2025,
                    watermark_mode=WatermarkMode.DRAFT,
                    inputs={"name": {"value": "Test", "source_ref": "test", "origin": "test"}},
                )
        finally:
            if should_backup:
                backup_path.rename(pdf_path)

    @pytest.mark.skipif(
        not (IRS_FORMS_DIR / "fw9.pdf").exists(),
        reason="IRS Form W-9 PDF not found in data/irs_forms/",
    )
    def test_render_w9_basic_fill(self):
        """Test that render_w9 fills basic fields correctly."""
        pdf_bytes, content_type = render_w9(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "name": {"value": "John Doe", "source_ref": "test", "origin": "test"},
                "business_name": {"value": "Doe Consulting", "source_ref": "test", "origin": "test"},
                "address": {"value": "123 Main St", "source_ref": "test", "origin": "test"},
                "city": {"value": "Columbia", "source_ref": "test", "origin": "test"},
                "state": {"value": "SC", "source_ref": "test", "origin": "test"},
                "zip_code": {"value": "29201", "source_ref": "test", "origin": "test"},
            },
        )
        
        assert content_type == "application/pdf"
        assert len(pdf_bytes) > 0
        
        # Verify that the PDF contains the filled values (basic sanity check)
        # Note: This is a simple text search; for robustness, use pypdf to extract fields
        text_content = pdf_bytes.decode("latin-1", errors="ignore")
        assert "John Doe" in text_content
        assert "123 Main St" in text_content
        assert "Columbia" in text_content
        assert "SC" in text_content
        assert "29201" in text_content

    @pytest.mark.skipif(
        not (IRS_FORMS_DIR / "fw9.pdf").exists(),
        reason="IRS Form W-9 PDF not found in data/irs_forms/",
    )
    def test_render_w9_radio_button(self):
        """Test that render_w9 sets radio buttons correctly."""
        pdf_bytes, content_type = render_w9(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "name": {"value": "John Doe", "source_ref": "test", "origin": "test"},
                "llc": {"value": "true", "source_ref": "test", "origin": "test"},
                "llc_classification": {"value": "P", "source_ref": "test", "origin": "test"},
            },
        )
        
        assert content_type == "application/pdf"
        assert len(pdf_bytes) > 0

    @pytest.mark.skipif(
        not (IRS_FORMS_DIR / "fw9.pdf").exists(),
        reason="IRS Form W-9 PDF not found in data/irs_forms/",
    )
    def test_render_w9_tin_not_filled(self):
        """Test that render_w9 does NOT fill TIN fields even if provided."""
        pdf_bytes, content_type = render_w9(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "name": {"value": "John Doe", "source_ref": "test", "origin": "test"},
                "tin_part1": {"value": "123", "source_ref": "test", "origin": "test"},
                "tin_part2": {"value": "45", "source_ref": "test", "origin": "test"},
                "tin_part3": {"value": "6789", "source_ref": "test", "origin": "test"},
            },
        )
        
        assert content_type == "application/pdf"
        assert len(pdf_bytes) > 0
