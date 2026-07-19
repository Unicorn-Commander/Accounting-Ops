"""Golden-case tests for the vendored 1040 computation chain.

Hand-verified against the authoritative 2025 OBBBA constants (standard deduction
MFJ $31,500 / single $15,750; Rev. Proc. 2024-40 ordinary brackets). These guard
the engine + config wiring against drift — a wrong bracket here is real money on
a real return.
"""
from __future__ import annotations

import pytest

from app.tax.compute_1040 import compute_1040
from app.tax.engines.configs_1040 import standard_deduction_for, tax_config_for
from app.tax.engines.form1040 import Form1040Inputs, calculate_form1040


class TestEngineGolden:
    def test_mfj_120k_wages_2025(self):
        # MFJ, $120,000 wages, standard deduction. Taxable = 120,000 - 31,500 =
        # 88,500. Tax = 10%*23,850 + 12%*(88,500-23,850) = 2,385 + 7,758 = $10,143.
        cfg = tax_config_for(2025, "mfj")
        assert cfg.standard_deduction_cents == 3150000  # OBBBA 2025 MFJ
        r = calculate_form1040(Form1040Inputs(filing_status="mfj", wages_cents=12000000), cfg)
        assert r.agi_cents == 12000000
        assert r.deduction_cents == 3150000
        assert r.taxable_income_cents == 8850000
        assert r.total_tax_cents == 1014300  # $10,143.00 exactly

    def test_single_60k_wages_2025(self):
        # Single, $60,000 wages. Taxable = 60,000 - 15,750 = 44,250.
        # Tax = 10%*11,925 + 12%*(44,250-11,925) = 1,192.50 + 3,879 = $5,071.50.
        cfg = tax_config_for(2025, "single")
        assert cfg.standard_deduction_cents == 1575000  # OBBBA 2025 single
        r = calculate_form1040(Form1040Inputs(filing_status="single", wages_cents=6000000), cfg)
        assert r.taxable_income_cents == 4425000
        assert r.total_tax_cents == 507150  # $5,071.50

    def test_obbba_2025_standard_deductions(self):
        # Regression guard: 2025 MUST be the OBBBA figures, not pre-OBBBA.
        assert standard_deduction_for(2025, "single") == 1575000
        assert standard_deduction_for(2025, "mfj") == 3150000
        assert standard_deduction_for(2025, "hoh") == 2362500
        assert standard_deduction_for(2026, "mfj") == 3220000

    def test_unsupported_year_raises(self):
        with pytest.raises(ValueError, match="No 1040 tax config"):
            tax_config_for(2099, "single")


class TestCompute1040Payload:
    def test_payload_shape_and_provenance(self):
        out = compute_1040(
            2025, "mfj",
            {
                "wages": {"value": "120000", "source_ref": "w2#1"},
                "withholding": {"value": "15000", "source_ref": "w2#1"},
            },
        )
        inp = out["inputs"]
        # Income lines carry the document source_ref.
        assert inp["wages_1a"]["value"] == "120000"
        assert inp["wages_1a"]["source_ref"] == "w2#1"
        # Computed lines carry the engine source_ref + origin=computed.
        assert inp["agi_11"]["value"] == "120000"
        assert inp["taxable_income_15"]["value"] == "88500"
        assert inp["total_tax_24"]["value"] == "10143"
        assert inp["total_tax_24"]["source_ref"].startswith("computed:form1040")
        assert inp["total_tax_24"]["origin"] == "computed"
        # Provenance summary present.
        assert out["provenance"]["computed"]["total_tax"] == "10143"
        assert out["provenance"]["engine"].startswith("app.tax.engines.form1040")

    def test_every_nonblank_input_has_source_ref(self):
        # The work-product provenance guard must pass on a computed 1040.
        from app.api.work_products import _assert_provenance

        out = compute_1040(2025, "single", {"wages": {"value": "60000", "source_ref": "w2#1"}})
        _assert_provenance(out["inputs"])  # must not raise

    def test_unsupported_year_raises(self):
        with pytest.raises(ValueError):
            compute_1040(2099, "single", {})

    def test_bad_filing_status_raises(self):
        with pytest.raises(ValueError, match="filing_status"):
            compute_1040(2025, "nope", {})
