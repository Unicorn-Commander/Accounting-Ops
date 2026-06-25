"""Smoke tests for the Form 941 professional-substitute path.

Covers the field-map consistency, the dollars/cents amount splitter, and — when
the gitignored IRS PDF is present — the end-to-end render (fields land on the
right page, money split into dollars+cents, employer name fans out to all three
name boxes, conservative blank-fill, watermark by status). Render tests skip when
``data/irs_forms/f941.pdf`` is absent (as in CI), mirroring ``test_w4p.py``.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import IRS_FORMS_DIR, WatermarkMode
from app.exports.f941_fill import _split_amount, render_941
from app.exports.f941_field_map import (
    FORM_941_AGGREGATE_FILER_FIELDS,
    FORM_941_CENTS_FIELDS,
    FORM_941_DEPOSIT_SCHEDULE_FIELDS,
    FORM_941_DESIGNEE_FIELDS,
    FORM_941_FIELDS,
    FORM_941_LINE15B_FIELDS,
    FORM_941_LINE15D_ACCOUNT_TYPE,
    FORM_941_NAME_FIELDS,
    FORM_941_QUARTER_FIELDS,
    FORM_941_SINGLE_CHECKBOXES,
    VALID_941_FIELDS,
)

_F941_PDF = IRS_FORMS_DIR / "f941.pdf"


def _needs_pdf():
    if not _F941_PDF.exists():
        pytest.skip("Form 941 PDF absent (gitignored) — skipping render test")


def _prov(value: str) -> dict:
    """Wrap a value with the provenance shape the renderer expects."""
    return {"value": value, "source_ref": "test#1", "origin": "document"}


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_field_names_qualified(self):
        for key, name in FORM_941_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
        for key, name in FORM_941_CENTS_FIELDS.items():
            assert name.startswith("topmostSubform"), f"cents {key} -> {name!r}"
        for name in FORM_941_NAME_FIELDS:
            assert name.startswith("topmostSubform"), name

    def test_no_duplicate_dollar_field_names(self):
        seen: dict[str, str] = {}
        for key, name in FORM_941_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_no_duplicate_cents_field_names(self):
        seen: dict[str, str] = {}
        for key, name in FORM_941_CENTS_FIELDS.items():
            assert name not in seen, f"dup cents {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_dollars_and_cents_boxes_are_distinct(self):
        # A money key's cents box must never collide with any dollars box.
        dollar_boxes = set(FORM_941_FIELDS.values())
        for key, cents_name in FORM_941_CENTS_FIELDS.items():
            assert cents_name not in dollar_boxes, f"{key} cents box reuses a dollars box"

    def test_every_cents_key_has_a_dollars_box(self):
        # Each money logical key appears in BOTH maps (dollars + cents).
        assert set(FORM_941_CENTS_FIELDS) <= set(FORM_941_FIELDS)

    def test_valid_fields_superset(self):
        assert set(FORM_941_FIELDS.values()) <= VALID_941_FIELDS
        assert set(FORM_941_CENTS_FIELDS.values()) <= VALID_941_FIELDS
        assert set(FORM_941_NAME_FIELDS) <= VALID_941_FIELDS

    def test_quarter_on_states(self):
        # Each quarter sets a page-1 header box + a 941-V voucher circle, both
        # with the matching on-state ("1".."4").
        for q in ("1", "2", "3", "4"):
            targets = FORM_941_QUARTER_FIELDS[q]
            assert len(targets) == 2
            for fname, on_state in targets:
                assert on_state == q
                assert fname in VALID_941_FIELDS

    def test_choice_maps_in_valid(self):
        for m in (
            FORM_941_LINE15B_FIELDS,
            FORM_941_LINE15D_ACCOUNT_TYPE,
            FORM_941_DEPOSIT_SCHEDULE_FIELDS,
            FORM_941_DESIGNEE_FIELDS,
            FORM_941_AGGREGATE_FILER_FIELDS,
            FORM_941_SINGLE_CHECKBOXES,
        ):
            for fname, _on in m.values():
                assert fname in VALID_941_FIELDS


# ---------------------------------------------------------------------------
# Amount splitter (load-bearing formatting logic, no PDF)
# ---------------------------------------------------------------------------

class TestSplitAmount:
    @pytest.mark.parametrize("raw,expected", [
        ("12345.67", ("12345", "67")),
        ("12,345", ("12345", None)),
        ("$1,000.05", ("1000", "05")),
        ("185000.00", ("185000", "00")),
        ("-50.2", ("-50", "20")),     # padded to 2 cents digits
        (".75", ("0", "75")),
        ("12345.6789", ("12345", "67")),  # truncated, never rounded
        ("0", ("0", None)),
        ("", (None, None)),
        ("-", (None, None)),
    ])
    def test_split(self, raw, expected):
        assert _split_amount(raw) == expected


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def test_values_baked_in_visible(self):
        # Values are flattened into page content so they render in EVERY viewer
        # (not dependent on /NeedAppearances). Assert on extracted text.
        _needs_pdf()
        from pypdf import PdfReader

        pdf, ct = render_941(
            tax_year=2026,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={
                "name":                          _prov("Acme Widgets LLC"),
                "quarter":                       _prov("2"),
                "line2_wages":                   _prov("185000.00"),
                "line3_fed_income_tax_withheld": _prov("21450.50"),
                "line16_total_liability":        _prov("32000.00"),
                "voucher_amount_paid":           _prov("4575.25"),
                "deposit_schedule":              _prov("monthly"),
            },
        )
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        pages = PdfReader(io.BytesIO(pdf)).pages
        p1 = pages[0].extract_text()
        p2 = pages[1].extract_text()
        p3 = pages[2].extract_text()

        # Page 1 — entity name + line 2/3 dollar amounts.
        assert "Acme Widgets LLC" in p1
        assert "185000" in p1            # line 2 wages, dollars box
        assert "21450" in p1             # line 3 withholding, dollars box
        # Page 2 — name fans out here; monthly total liability lands here.
        assert "Acme Widgets LLC" in p2
        assert "32000" in p2
        # Page 3 (941-V voucher) — name fans out; amount paid lands here.
        assert "Acme Widgets LLC" in p3
        assert "4575" in p3

    def test_conservative_blank_fill(self):
        _needs_pdf()
        from pypdf import PdfReader

        pdf, _ = render_941(
            tax_year=2026,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={"name": _prov("Solo Proprietor Inc")},
        )
        pages = PdfReader(io.BytesIO(pdf)).pages
        p1 = pages[0].extract_text()
        assert "Solo Proprietor Inc" in p1
        # A line we never set must not leak any value in.
        assert "185000" not in p1
        assert "999999" not in p1

    def test_unknown_key_skipped(self):
        # An unrecognized logical key must not raise and must not appear.
        _needs_pdf()
        from pypdf import PdfReader

        pdf, _ = render_941(
            tax_year=2026,
            watermark_mode=WatermarkMode.NONE,
            inputs={
                "name": _prov("Edge Case Co"),
                "not_a_real_941_line": _prov("SHOULD_NOT_RENDER"),
            },
        )
        txt = "".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)
        assert "Edge Case Co" in txt
        assert "SHOULD_NOT_RENDER" not in txt

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_941(2026, mode, {"name": _prov("Mode Test LLC")})
            assert pdf[:4] == b"%PDF"

    def test_missing_pdf_raises(self, tmp_path, monkeypatch):
        # When no source PDF can be resolved, the renderer raises a clear error.
        import app.exports.f941_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)
        with pytest.raises(FileNotFoundError, match="Form 941 PDF not found"):
            render_941(2026, WatermarkMode.DRAFT, {"name": _prov("X")})
