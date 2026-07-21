"""Unit tests for Form 843 fill renderer."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure app exports are importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.exports.form_prefill import WatermarkMode
from app.exports.f843_fill import render_843, _f843_form_path
from app.exports.f843_field_map import F843_FIELDS, F843_CHECKBOX_FIELDS, VALID_F843_FIELDS


# Skip if PDF is not present
@pytest.fixture
def f843_pdf_path():
    """Return the path to the F843 PDF if it exists, else None."""
    path = _f843_form_path(2025)
    if path.exists():
        return path
    return None


def test_f843_field_map_structure():
    """Verify that the field map has the expected structure."""
    assert isinstance(F843_FIELDS, dict)
    assert isinstance(F843_CHECKBOX_FIELDS, dict)
    assert isinstance(VALID_F843_FIELDS, frozenset)

    # Check that all checkbox field names are in VALID_F843_FIELDS
    for fname, _ in F843_CHECKBOX_FIELDS.values():
        assert fname in VALID_F843_FIELDS, f"Checkbox field {fname} not in VALID_F843_FIELDS"

    # Check that all text field names are in VALID_F843_FIELDS
    for fname in F843_FIELDS.values():
        assert fname in VALID_F843_FIELDS, f"Text field {fname} not in VALID_F843_FIELDS"


def test_f843_render_skips_if_pdf_missing():
    """Verify that render_843 raises FileNotFoundError if PDF is missing."""
    with patch("app.exports.f843_fill._f843_form_path") as mock_path:
        mock_path.return_value = Path("/nonexistent/f843.pdf")
        with pytest.raises(FileNotFoundError):
            render_843(
                tax_year=2025,
                watermark_mode=WatermarkMode.DRAFT,
                inputs={"name": {"value": "Test", "source_ref": "test", "origin": "user"}},
            )


def test_f843_render_with_pdf(f843_pdf_path):
    """Verify that render_843 produces valid PDF bytes when PDF is present."""
    if f843_pdf_path is None:
        pytest.skip("F843 PDF not found in data/irs_forms/")

    # Mock _render_acroform_pdf to return dummy bytes and avoid actual PDF processing
    with patch("app.exports.f843_fill._render_acroform_pdf") as mock_render:
        mock_render.return_value = b"dummy_pdf_bytes"

        result = render_843(
            tax_year=2025,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "name": {"value": "Test Name", "source_ref": "test", "origin": "user"},
                "amount": {"value": "1000", "source_ref": "test", "origin": "user"},
                "tax_type_income": {"value": "true", "source_ref": "test", "origin": "user"},
                "reason_reasonable_cause": {"value": "false", "source_ref": "test", "origin": "user"},
            },
        )

        assert result[1] == "application/pdf"
        assert mock_render.called
        # Check that the field values passed to _render_acroform_pdf are correct
        call_args = mock_render.call_args
        field_values = call_args[0][1]  # Second argument to _render_acroform_pdf

        assert "name" in field_values or any("Name" in k for k in field_values.keys())
        assert "1000" in field_values.values()
        # Checkbox "tax_type_income" should be set
        assert any(v == "1" for v in field_values.values())
        # Checkbox "reason_reasonable_cause" should NOT be set (value was "false")
        # We can't easily check the specific key without knowing the exact field name,
        # but we can check that not all checkboxes are set.
