"""Tests for Form 1096 renderer."""

from __future__ import annotations

import os
import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.f1096_fill import render_1096


# Skip if the PDF is not present (downloaded by scripts/download_irs_forms.sh)
@pytest.mark.skipif(
    not os.path.exists("data/irs_forms/f1096.pdf"),
    reason="f1096.pdf not found in data/irs_forms/"
)
class TestRender1096:
    def test_render_minimal(self):
        """Render with minimal required fields."""
        pdf_bytes, content_type = render_1096(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "filer_name": {"value": "Test Corp", "source_ref": "test", "origin": "user"},
                "ein": {"value": "12-3456789", "source_ref": "test", "origin": "user"},
                "box_3_total_forms": {"value": "1", "source_ref": "test", "origin": "user"},
                "box_6_1099_nec": {"value": "true", "source_ref": "test", "origin": "user"},
            },
        )
        assert content_type == "application/pdf"
        assert len(pdf_bytes) > 0

    def test_render_multiple_box6_raises(self):
        """Render with multiple Box 6 checkboxes should raise ValueError."""
        with pytest.raises(ValueError, match="only one form type"):
            render_1096(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs={
                    "filer_name": {"value": "Test Corp", "source_ref": "test", "origin": "user"},
                    "box_6_1099_nec": {"value": "true", "source_ref": "test", "origin": "user"},
                    "box_6_1099_misc": {"value": "true", "source_ref": "test", "origin": "user"},
                },
            )

    def test_render_blank_fill(self):
        """Render with blank values should leave fields empty."""
        pdf_bytes, content_type = render_1096(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "filer_name": {"value": "", "source_ref": "test", "origin": "user"},
                "box_3_total_forms": {"value": None, "source_ref": "test", "origin": "user"},
            },
        )
        assert content_type == "application/pdf"
        assert len(pdf_bytes) > 0
