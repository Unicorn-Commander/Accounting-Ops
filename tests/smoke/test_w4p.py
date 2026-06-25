"""Smoke tests for the W-4P professional-substitute path.

Covers the deterministic worksheet math (no PDF), the field-map consistency, the
work-product provenance guard, and — when the gitignored IRS PDF is present —
the end-to-end render (fields land, conservative blank-fill, watermark by status).
Render tests skip when data/irs_forms/f-w4p.pdf is absent (as in CI).
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import (
    IRS_FORMS_DIR,
    FormName,
    W4PFill,
    WatermarkMode,
    generate_form_pdf,
    generate_w4p_pdf,
)
from app.exports.w4p_field_map import (
    VALID_W4P_FIELDS,
    W4P_FIELDS,
    W4P_FILING_STATUS_FIELDS,
)
from app.tax.w4p_worksheet import (
    SUPPORTED_TAX_YEARS,
    W4PFilingStatus,
    W4PWorksheetInput,
    compute_w4p_worksheet,
    config_for,
)

_W4P_PDF = IRS_FORMS_DIR / "f-w4p.pdf"
_P1 = "topmostSubform[0].Page1[0]"


def _needs_pdf():
    if not _W4P_PDF.exists():
        pytest.skip("W-4P PDF absent (gitignored) — skipping render test")


# ---------------------------------------------------------------------------
# Deterministic worksheet
# ---------------------------------------------------------------------------

class TestWorksheet:
    def test_mfj_two_kids_one_other_under_threshold(self):
        r = compute_w4p_worksheet(W4PWorksheetInput(
            tax_year=2026, filing_status=W4PFilingStatus.MFJ_OR_QSS,
            qualifying_children=2, other_dependents=1,
            estimated_annual_income=85000, estimated_itemized_deductions=40000,
        ))
        assert r.step3a_child_credit == 4400      # 2 * 2200 (TY2026 CTC)
        assert r.step3b_other_dep_credit == 500   # 1 * 500
        assert r.step3_total == 4900
        assert r.standard_deduction == 32200      # TY2026 MFJ
        assert r.step4b_deductions == 7800        # 40000 - 32200

    def test_2025_ctc_is_2000(self):
        r = compute_w4p_worksheet(W4PWorksheetInput(
            tax_year=2025, filing_status=W4PFilingStatus.SINGLE_OR_MFS,
            qualifying_children=2,
        ))
        assert r.step3a_child_credit == 4000      # 2 * 2000 (TY2025 CTC)
        assert r.standard_deduction == 15000      # TY2025 single

    def test_over_threshold_zeroes_step3(self):
        r = compute_w4p_worksheet(W4PWorksheetInput(
            tax_year=2026, filing_status=W4PFilingStatus.MFJ_OR_QSS,
            qualifying_children=3, estimated_annual_income=500000,
        ))
        assert r.step3_total == 0                 # above the $400k MFJ threshold
        assert any("threshold" in a for a in r.assumptions)

    def test_hoh_standard_deduction(self):
        r = compute_w4p_worksheet(W4PWorksheetInput(
            tax_year=2026, filing_status=W4PFilingStatus.HEAD_OF_HOUSEHOLD,
            estimated_itemized_deductions=30000,
        ))
        assert r.standard_deduction == 24150      # TY2026 HoH
        assert r.step4b_deductions == 30000 - 24150

    def test_deductions_floored_at_zero(self):
        r = compute_w4p_worksheet(W4PWorksheetInput(
            tax_year=2026, filing_status=W4PFilingStatus.SINGLE_OR_MFS,
            estimated_itemized_deductions=5000,   # below the standard deduction
        ))
        assert r.step4b_deductions == 0

    def test_unsupported_year_raises(self):
        with pytest.raises(ValueError, match="No W-4P statutory constants"):
            compute_w4p_worksheet(W4PWorksheetInput(
                tax_year=2099, filing_status=W4PFilingStatus.SINGLE_OR_MFS,
            ))

    def test_supported_years_have_configs(self):
        for y in SUPPORTED_TAX_YEARS:
            cfg = config_for(y)
            assert cfg.std_mfj_or_qss > cfg.std_single_or_mfs > 0
            assert cfg.ctc_per_child >= 2000


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_field_names_qualified(self):
        for key, name in W4P_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_no_duplicate_field_names(self):
        seen: dict[str, str] = {}
        for key, name in W4P_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_filing_status_on_states(self):
        assert W4P_FILING_STATUS_FIELDS["single_or_mfs"] == (f"{_P1}.c1_1[0]", "1")
        assert W4P_FILING_STATUS_FIELDS["mfj_or_qss"] == (f"{_P1}.c1_1[1]", "2")
        assert W4P_FILING_STATUS_FIELDS["head_of_household"] == (f"{_P1}.c1_1[2]", "3")

    def test_valid_fields_superset(self):
        assert set(W4P_FIELDS.values()) <= VALID_W4P_FIELDS


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # Values are flattened into page content so they render in EVERY viewer
        # (not dependent on /NeedAppearances). Assert on extracted text.
        _needs_pdf()
        from pypdf import PdfReader

        fill = W4PFill(
            tax_year=2026, first_name_mi="Aaron J", last_name="Stransky",
            address="123 Main St", filing_status="mfj_or_qss",
            step3_total="4900", step4b_deductions="7800",
        )
        pdf, ct = generate_w4p_pdf(fill, WatermarkMode.DRAFT)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        for needle in ("Aaron J", "Stransky", "123 Main St", "4900", "7800"):
            assert needle in txt, f"{needle!r} not visible in rendered W-4P"

    def test_conservative_blank_fill(self):
        _needs_pdf()
        from pypdf import PdfReader

        pdf, _ = generate_w4p_pdf(W4PFill(tax_year=2026, last_name="Solo"), WatermarkMode.DRAFT)
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        assert "Solo" in txt
        assert "Aaron" not in txt  # nothing we didn't set leaks in

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        fill = W4PFill(tax_year=2026, last_name="Solo")
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = generate_w4p_pdf(fill, mode)
            assert pdf[:4] == b"%PDF"

    def test_generate_form_pdf_redirects_w4p(self):
        # W-4P must not go through the corporate generate_form_pdf path.
        with pytest.raises(ValueError, match="generate_w4p_pdf"):
            generate_form_pdf(
                form_name=FormName.W4P.value, tax_year=2026,
                entity_display_name="x", ein=None, queue_items=[], cpa_package=None,
            )


# ---------------------------------------------------------------------------
# Provenance guard (anti-fabrication) — no PDF needed for the reject paths
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_value_without_source_ref_rejected(self):
        from app.api.work_products import _assert_provenance

        with pytest.raises(ValueError, match="source_ref"):
            _assert_provenance({"step3_total": {"value": "9999", "origin": "computed"}})

    def test_blank_value_allowed(self):
        from app.api.work_products import _assert_provenance

        _assert_provenance({"ssn": {"value": "", "origin": "user_entered"}})  # no raise

    def test_render_refuses_fabricated_figure(self):
        _needs_pdf()
        from app.api.work_products import _render_work_product

        with pytest.raises(ValueError):
            _render_work_product("w-4p", 2026, "draft",
                                 {"step4c_extra": {"value": "500"}})  # no source_ref

    def test_watermark_for_status(self):
        from app.api.work_products import _watermark_for_status

        assert _watermark_for_status("draft") == WatermarkMode.DRAFT
        assert _watermark_for_status("self_approved") == WatermarkMode.SELF_CERTIFIED
        assert _watermark_for_status("pro_reviewed") == WatermarkMode.NONE
        assert _watermark_for_status("finalized") == WatermarkMode.NONE
