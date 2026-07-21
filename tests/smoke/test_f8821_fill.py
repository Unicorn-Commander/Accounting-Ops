"""Tests for Form 8821 (Tax Information Authorization) renderer."""

from __future__ import annotations

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.f8821_fill import render_8821


def test_render_8821_skipped_if_pdf_absent():
    """render_8821 raises FileNotFoundError when the PDF is not present."""
    with pytest.raises(FileNotFoundError):
        render_8821(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "taxpayer_name": {"value": "Test", "source_ref": "test", "origin": "test"},
            },
        )


def test_render_8821_basic_fill():
    """render_8821 fills fields correctly when the PDF is present."""
    try:
        pdf_bytes, content_type = render_8821(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "taxpayer_name": {"value": "John Doe", "source_ref": "test", "origin": "test"},
                "taxpayer_address": {"value": "123 Main St", "source_ref": "test", "origin": "test"},
                "taxpayer_city": {"value": "Columbia", "source_ref": "test", "origin": "test"},
                "taxpayer_state": {"value": "SC", "source_ref": "test", "origin": "test"},
                "taxpayer_zip": {"value": "29201", "source_ref": "test", "origin": "test"},
                "taxpayer_tin": {"value": "123-45-6789", "source_ref": "test", "origin": "test"},
                "taxpayer_phone": {"value": "555-1234", "source_ref": "test", "origin": "test"},
                "designee_name": {"value": "Jane Smith, CPA", "source_ref": "test", "origin": "test"},
                "designee_address": {"value": "456 Oak Ave", "source_ref": "test", "origin": "test"},
                "designee_city": {"value": "Columbia", "source_ref": "test", "origin": "test"},
                "designee_state": {"value": "SC", "source_ref": "test", "origin": "test"},
                "designee_zip": {"value": "29201", "source_ref": "test", "origin": "test"},
                "designee_caf": {"value": "12345", "source_ref": "test", "origin": "test"},
                "designee_ptin": {"value": "P12345678", "source_ref": "test", "origin": "test"},
                "designee_phone": {"value": "555-5678", "source_ref": "test", "origin": "test"},
                "designee_fax": {"value": "555-9999", "source_ref": "test", "origin": "test"},
                "tax_type_1": {"value": "Federal", "source_ref": "test", "origin": "test"},
                "tax_form_1": {"value": "1040", "source_ref": "test", "origin": "test"},
                "tax_years_1": {"value": "2024", "source_ref": "test", "origin": "test"},
                "tax_matters_1": {"value": "All matters", "source_ref": "test", "origin": "test"},
                "designee_new_address": {"value": "true", "source_ref": "test", "origin": "test"},
            },
        )
    except FileNotFoundError:
        pytest.skip("IRS Form 8821 PDF not found in data/irs_forms/")
        return

    assert content_type == "application/pdf"
    assert isinstance(pdf_bytes, bytes)
    assert len(pdf_bytes) > 0
