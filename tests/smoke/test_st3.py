"""Smoke tests for the SC ST-3 (State Sales & Use Tax Return) path.

Covers the deterministic worksheet math (no PDF), the unsupported-year guard,
and — when the gitignored ST-3 PDF is present — the end-to-end render across all
three WatermarkMode values. The render asserts use the same ``_needs_pdf`` skip
pattern as ``tests/smoke/test_w4p.py`` so CI (no PDF) stays green; the fallback
summary path is exercised separately because it needs no source PDF.
"""
from __future__ import annotations

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.st3_fill import ST3_SOURCE_PDF, render_st3
from app.tax.st3_worksheet import (
    SUPPORTED_TAX_YEARS,
    ST3WorksheetInput,
    compute_st3_worksheet,
    config_for,
)


def _needs_pdf():
    if not ST3_SOURCE_PDF.exists():
        pytest.skip("ST-3 PDF absent (gitignored) — skipping real-form render test")


# ---------------------------------------------------------------------------
# Deterministic worksheet
# ---------------------------------------------------------------------------

class TestWorksheet:
    def test_known_case_gross_deductions_net_state_tax(self):
        # gross 100,000 − deductions 20,000 -> net 80,000; state tax @6% = 4,800.
        r = compute_st3_worksheet(ST3WorksheetInput(
            tax_year=2025,
            gross_proceeds_cents=100_000_00,
            deductions_cents=20_000_00,
        ))
        assert r.net_taxable_cents == 80_000_00
        assert r.state_tax_cents == 4_800_00      # 80,000 × 6%
        assert r.local_tax_cents == 0             # no county rate supplied
        assert r.total_tax_cents == 4_800_00
        assert any("VARY BY COUNTY" in a for a in r.assumptions)

    def test_supplied_net_taxable_takes_precedence_and_flags(self):
        r = compute_st3_worksheet(ST3WorksheetInput(
            tax_year=2025,
            gross_proceeds_cents=100_000_00,
            deductions_cents=20_000_00,
            net_taxable_cents=75_000_00,   # disagrees with 80,000 derived
        ))
        assert r.net_taxable_cents == 75_000_00
        assert r.state_tax_cents == 4_500_00      # 75,000 × 6%
        assert any("does not equal" in a for a in r.assumptions)

    def test_county_local_rate_estimates_line5(self):
        # net 80,000 with a confirmed combined 1% local rate -> 800 local tax.
        r = compute_st3_worksheet(ST3WorksheetInput(
            tax_year=2025,
            gross_proceeds_cents=100_000_00,
            deductions_cents=20_000_00,
            county_local_rate="0.01",
        ))
        assert r.local_tax_cents == 800_00
        assert r.total_tax_cents == 4_800_00 + 800_00
        assert any("ST-389" in a for a in r.assumptions)

    def test_state_tax_rounds_half_up_cents(self):
        # net = $1,000.05 × 6% = $60.003 -> rounds to $60.00 (half-up at cents).
        r = compute_st3_worksheet(ST3WorksheetInput(
            tax_year=2025, net_taxable_cents=1_000_05,
        ))
        assert r.state_tax_cents == 60_00

    def test_unsupported_year_raises(self):
        with pytest.raises(ValueError, match="No SC ST-3 statutory constants"):
            compute_st3_worksheet(ST3WorksheetInput(tax_year=2099))

    def test_supported_years_have_six_percent(self):
        for y in SUPPORTED_TAX_YEARS:
            assert config_for(y).state_rate == "0.06"


# ---------------------------------------------------------------------------
# Render — real ST-3 form (requires the gitignored PDF; skipped when absent)
# ---------------------------------------------------------------------------

_INPUTS = {
    "business_name": {"value": "Magic Unicorn LLC", "source_ref": "entity:1", "origin": "user_entered"},
    "retail_license_number": {"value": "123456789", "source_ref": "lic:1", "origin": "user_entered"},
    "period": {"value": "05-2025", "source_ref": "period:1", "origin": "user_entered"},
    "gross_proceeds": {"value": "100000.00", "source_ref": "ws:item3", "origin": "computed"},
    "deductions": {"value": "20000.00", "source_ref": "ws:item5", "origin": "computed"},
    "net_taxable": {"value": "80000.00", "source_ref": "ws:item6", "origin": "computed"},
    "state_tax": {"value": "4800.00", "source_ref": "st3:line4", "origin": "computed"},
    "total_tax": {"value": "4800.00", "source_ref": "st3:line10", "origin": "computed"},
}


class TestRenderRealForm:
    def test_all_watermark_modes_render(self):
        _needs_pdf()
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, ct = render_st3(2025, mode, _INPUTS)
            assert ct == "application/pdf"
            assert pdf[:4] == b"%PDF"

    def test_blank_and_unknown_values_skipped(self):
        _needs_pdf()
        # Blank value + unknown key must not break the render and must not be
        # invented onto the form.
        inputs = {
            "business_name": {"value": "", "source_ref": "x", "origin": "user_entered"},
            "gross_proceeds": {"value": "5000.00", "source_ref": "ws", "origin": "computed"},
            "bogus_key": {"value": "999", "source_ref": "x", "origin": "x"},
        }
        pdf, ct = render_st3(2025, WatermarkMode.DRAFT, inputs)
        assert pdf[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# Render — fallback summary sheet (no source PDF needed)
# ---------------------------------------------------------------------------

class TestRenderFallback:
    def test_summary_sheet_builds_when_pdf_absent(self, monkeypatch, tmp_path):
        # Force the absent-PDF branch by pointing the source at a missing file,
        # regardless of whether the real PDF exists on this machine.
        import app.exports.st3_fill as st3_fill
        monkeypatch.setattr(
            st3_fill, "ST3_SOURCE_PDF", tmp_path / "does-not-exist.pdf",
        )
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, ct = render_st3(2025, mode, _INPUTS)
            assert ct == "application/pdf"
            assert pdf[:4] == b"%PDF"
