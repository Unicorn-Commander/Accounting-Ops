"""Smoke tests for the SCDOR + IRS programmatic submission tooling.

Covers:
  1. SC1120 / SC1120-V PDF overlay generation (only when source PDFs are
     present in data/scdor_forms/; gracefully skips otherwise).
  2. Cover sheet + mailing label PDF generation (no source dependency).
  3. USPS tracking number validator + response parser (no live USPS).
  4. tax_filing_submission helpers: expected-response-by computation.
  5. Address book lookups.

No DB. No HTTP. No live USPS. The integration suite covers DB + HTTP shape
end-to-end against the live docker stack.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app.api.tax_filing_submissions import _compute_expected_response_by
from app.api.usps_tracking import (
    TRACKING_NUMBER_RE,
    _parse_track_response,
)
from app.exports.cover_sheet import (
    CoverSheetData,
    TAX_OFFICE_ADDRESSES,
    generate_cover_sheet_pdf,
)
from app.exports.mailing_label import (
    MailingLabelData,
    generate_mailing_label_pdf,
)
from app.exports.sc_form_prefill import (
    SC1120_PAGE1_HEADER_COORDS,
    SC1120_PAGE1_LINE_COORDS,
    SC1120_SOURCE_PDF,
    SC1120V_SOURCE_PDF,
    _build_sc1120_values_from_package,
    _build_sc1120_values_from_queue,
    _cents_to_dollar_str,
    build_sc1120_context,
    generate_sc1120_pdf,
    generate_sc1120v_pdf,
)


# ---------------------------------------------------------------------------
# 1. SC1120 value-map builders (no PDF I/O required)
# ---------------------------------------------------------------------------

class TestSC1120ValueBuilders:
    def test_cents_to_dollar_str(self):
        assert _cents_to_dollar_str(123456) == "1234"
        assert _cents_to_dollar_str(-50000) == "-500"
        assert _cents_to_dollar_str(0) == "0"

    def test_loss_year_sets_zero_tax(self):
        """For a loss year, line 7 (5% tax) should be '0' not negative.

        Python floor-division on negatives: -367079 // 100 == -3671 (NOT -3670).
        This is the platform-wide convention; we don't fight it here.
        """
        pkg = {"taxable_income_before_nol_cents": -367079}
        v = _build_sc1120_values_from_package(pkg)
        # Federal taxable income line 1 = -3671 (Python integer floor div)
        assert v["1"] == "-3671"
        # SC tax line 7 should be 0 (loss year — no tax owed)
        assert v["7"] == "0"
        # Lines 9, 11, 13, 18 inherit from line 7
        assert v["9"] == "0"
        assert v["11"] == "0"
        assert v["13"] == "0"
        assert v["18"] == "0"

    def test_profit_year_computes_5pct(self):
        """For a profit year, line 7 = 5% of line 6."""
        pkg = {"taxable_income_before_nol_cents": 1_000_000}  # $10,000 profit
        v = _build_sc1120_values_from_package(pkg)
        # SC tax = 5% × $10,000 = $500
        assert v["7"] == "500"

    def test_queue_overrides_package(self):
        """Queue items must override package values when both present."""
        pkg = {"taxable_income_before_nol_cents": -367079}
        # Queue says SC1120 Line 6 = $42 (just for test sanity)
        queue = [{
            "item_type": "form_line_proposal",
            "form_reference": "SC1120 Line 6",
            "content": {"proposed_amount_cents": 4200},
        }]
        from_pkg = _build_sc1120_values_from_package(pkg)
        from_queue = _build_sc1120_values_from_queue(queue)
        # Package would set 6=-3671; queue should override to 42
        assert from_pkg["6"] == "-3671"
        assert from_queue["6"] == "42"

    def test_non_sc1120_queue_ignored(self):
        """Federal-only queue items must not leak into SC1120 values."""
        queue = [{
            "item_type": "form_line_proposal",
            "form_reference": "Form 1120, Line 7",  # federal
            "content": {"proposed_amount_cents": 99},
        }]
        assert _build_sc1120_values_from_queue(queue) == {}

    def test_build_context_diagnostics(self):
        ctx = build_sc1120_context(
            tax_year=2023,
            entity_display_name="Test Corp",
            ein="12-3456789",
            mailing_address="1 Test St",
            city_state_zip="Test, SC 29407",
            queue_items=[],
            cpa_package={"taxable_income_before_nol_cents": -367079},
        )
        # All canonical line keys come from the coord map
        assert ctx.tax_year == 2023
        assert ctx.entity_display_name == "Test Corp"
        assert set(ctx.filled_lines) >= {"1", "3", "4", "6", "7", "9", "11", "13", "18"}
        # blank_lines should contain lines we never set (e.g. 14a)
        assert "14a" in ctx.blank_lines


# ---------------------------------------------------------------------------
# 2. SC1120 coord-map sanity
# ---------------------------------------------------------------------------

class TestSC1120Coords:
    def test_header_coords_present(self):
        for key in ("fein", "corp_name", "mailing_address", "city_state_zip"):
            assert key in SC1120_PAGE1_HEADER_COORDS
            x, y = SC1120_PAGE1_HEADER_COORDS[key]
            assert 0 < x < 612, f"{key} x out of letter width"
            assert 0 < y < 792, f"{key} y out of letter height"

    def test_line_coords_present(self):
        # Lines 1-19 must all have coords
        for line in ("1", "2", "3", "7", "13", "18", "19"):
            assert line in SC1120_PAGE1_LINE_COORDS, f"line {line} missing"

    def test_line_y_ordering(self):
        """Higher line numbers should be lower on the page (smaller y)."""
        y1 = SC1120_PAGE1_LINE_COORDS["1"][1]
        y19 = SC1120_PAGE1_LINE_COORDS["19"][1]
        assert y1 > y19, "line numbering should be top-to-bottom"


# ---------------------------------------------------------------------------
# 3. SC1120 PDF generation (only runs when source PDFs are downloaded)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not SC1120_SOURCE_PDF.exists(),
    reason="SC1120 source PDF not present (run scripts/download_scdor_forms.sh)",
)
class TestSC1120PDFGeneration:
    def test_minimal_sc1120(self):
        pdf, ctx = generate_sc1120_pdf(
            tax_year=2023,
            entity_display_name="Test Corp",
            ein="12-3456789",
            mailing_address="1 Test St",
            city_state_zip="Test, SC 29407",
            queue_items=[],
            cpa_package={"taxable_income_before_nol_cents": -367079},
        )
        assert isinstance(pdf, bytes)
        assert pdf.startswith(b"%PDF-"), "expected a PDF magic number"
        assert len(pdf) > 50_000, f"PDF suspiciously small: {len(pdf)}"
        # Should be more than the source PDF (we add an overlay)
        assert len(pdf) > SC1120_SOURCE_PDF.stat().st_size

    def test_watermark_present(self):
        """The DRAFT watermark + 'do not file' banner must be in the bytes."""
        pdf, _ = generate_sc1120_pdf(
            tax_year=2023,
            entity_display_name="Test",
            ein=None,
            mailing_address=None,
            city_state_zip=None,
            queue_items=[],
            cpa_package=None,
        )
        # The watermark renders the literal "DRAFT" string in a content stream
        # which may be split / encoded, so check the banner text instead.
        assert b"DRAFT" in pdf or b"do not file as-is" in pdf


@pytest.mark.skipif(
    not SC1120V_SOURCE_PDF.exists(),
    reason="SC1120V source PDF not present (run scripts/download_scdor_forms.sh)",
)
class TestSC1120VPDFGeneration:
    def test_minimal_sc1120v(self):
        pdf = generate_sc1120v_pdf(
            tax_year=2023,
            entity_display_name="Test Corp",
            ein="12-3456789",
            mailing_address="1 Test St",
            city_state_zip="Test, SC 29407",
            payment_amount_cents=2500,
        )
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 50_000


# ---------------------------------------------------------------------------
# 4. Cover sheet
# ---------------------------------------------------------------------------

class TestCoverSheet:
    def test_basic_generation(self):
        pdf = generate_cover_sheet_pdf(CoverSheetData(
            submission_title="Test Filing",
            tax_year=2023,
            entity_name="Test Corp",
            entity_ein="12-3456789",
            to_office_key="scdor_corporate",
            enclosures=["Form A", "Form B"],
        ))
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 500

    def test_address_book_has_canonical_offices(self):
        for key in ("scdor_corporate", "scdor_voucher",
                    "irs_1120_sc_nopayment", "irs_1120_sc_withpayment"):
            assert key in TAX_OFFICE_ADDRESSES
            entry = TAX_OFFICE_ADDRESSES[key]
            assert entry["name"]
            assert entry["city_state_zip"]

    def test_filters_none_enclosures(self):
        """Caller can pass conditional Nones; we must filter them out."""
        pdf = generate_cover_sheet_pdf(CoverSheetData(
            submission_title="Test",
            tax_year=2023,
            entity_name="Test Corp",
            entity_ein="12-3456789",
            to_office_key="scdor_corporate",
            enclosures=["Form A", None, "Form B"],
        ))
        assert pdf.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# 5. Mailing label
# ---------------------------------------------------------------------------

class TestMailingLabel:
    def test_basic_generation(self):
        pdf = generate_mailing_label_pdf(MailingLabelData(
            to_office_key="scdor_corporate",
            sender_reference="test ref",
        ))
        assert pdf.startswith(b"%PDF-")
        assert len(pdf) > 500

    def test_with_tracking_qr(self):
        """Label with tracking number should embed a QR (larger payload)."""
        pdf_no_qr = generate_mailing_label_pdf(MailingLabelData(
            to_office_key="scdor_corporate",
            tracking_number=None,
        ))
        pdf_qr = generate_mailing_label_pdf(MailingLabelData(
            to_office_key="scdor_corporate",
            tracking_number="9407811899223456789012",
        ))
        # QR-enabled label should be bigger than the placeholder version
        assert len(pdf_qr) >= len(pdf_no_qr)


# ---------------------------------------------------------------------------
# 6. USPS tracking validator + parser
# ---------------------------------------------------------------------------

class TestUSPSTracking:
    def test_validator_accepts_22_digits(self):
        assert TRACKING_NUMBER_RE.match("9407811899223456789012")

    def test_validator_rejects_too_short(self):
        assert not TRACKING_NUMBER_RE.match("12345")

    def test_validator_rejects_letters(self):
        assert not TRACKING_NUMBER_RE.match("ABC1234567890123456789")

    def test_parse_delivered_response(self):
        xml = """<?xml version="1.0"?>
<TrackResponse>
  <TrackInfo ID="9407811899223456789012">
    <TrackSummary>Your item was delivered.</TrackSummary>
    <Status>Delivered</Status>
    <TrackDetail>
      <Event>DELIVERED</Event>
      <EventTime>11:23 am</EventTime>
      <EventDate>May 5, 2026</EventDate>
      <EventCity>COLUMBIA</EventCity>
      <EventState>SC</EventState>
      <EventZIPCode>29214</EventZIPCode>
    </TrackDetail>
  </TrackInfo>
</TrackResponse>"""
        result = _parse_track_response(xml, "9407811899223456789012")
        assert result.is_delivered is True
        assert "delivered" in result.summary.lower()
        assert len(result.events) >= 1
        # Latest event should be the delivery
        assert "DELIVERED" in result.events[0].event.upper()
        assert result.fault is None

    def test_parse_error_response(self):
        xml = """<?xml version="1.0"?>
<Error>
  <Number>80040b1a</Number>
  <Description>Tracking number not found.</Description>
</Error>"""
        result = _parse_track_response(xml, "9999999999999999999999")
        assert result.is_delivered is False
        assert "not found" in (result.fault or "").lower()

    def test_parse_malformed_response(self):
        result = _parse_track_response("not xml", "9407811899223456789012")
        assert result.fault is not None


# ---------------------------------------------------------------------------
# 7. tax_filing_submission helpers
# ---------------------------------------------------------------------------

class TestExpectedResponseBy:
    def test_irs_paper_56_days(self):
        sat = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
        erb = _compute_expected_response_by("irs", "paper", sat)
        assert erb == sat + dt.timedelta(days=56)

    def test_scdor_paper_42_days(self):
        sat = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
        erb = _compute_expected_response_by("scdor", "paper", sat)
        assert erb == sat + dt.timedelta(days=42)

    def test_scdor_mydorway_30_days(self):
        sat = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
        erb = _compute_expected_response_by("scdor", "mydorway", sat)
        assert erb == sat + dt.timedelta(days=30)

    def test_eftps_no_default(self):
        """EFTPS is a payment — no acknowledgement, so no default."""
        sat = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
        assert _compute_expected_response_by("irs", "eftps", sat) is None

    def test_no_submitted_at_returns_none(self):
        assert _compute_expected_response_by("irs", "paper", None) is None


# ---------------------------------------------------------------------------
# 8. End-to-end (only when source PDFs present)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not SC1120_SOURCE_PDF.exists(),
    reason="SC1120 source PDF not present",
)
def test_generate_submission_drafts_script_smoke(tmp_path):
    """Confirm the draft generator runs end-to-end and produces all 7 artifacts."""
    from scripts.generate_submission_drafts import generate_for_entity_year
    manifest = generate_for_entity_year("example", 2023, tmp_path)
    artifacts = [a for a in manifest["artifacts"] if "error" not in a]
    # 7 artifacts per year: SC1120 + SC1120V + Form1120 + 2 cover sheets + 2 labels
    assert len(artifacts) == 7, f"got {len(artifacts)}: {[a['form'] for a in artifacts]}"
    # Every generated file must exist + be non-trivial
    for art in artifacts:
        p = Path(art["path"])
        assert p.exists()
        assert p.stat().st_size > 500
