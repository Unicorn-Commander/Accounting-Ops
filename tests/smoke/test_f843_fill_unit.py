"""Unit tests for the Form 843 (Claim for Refund / Request for Abatement) renderer.

Ground truth: the field map was verified against the official
``data/irs_forms/f843.pdf`` (Rev. December 2024) by dumping
``pypdf.PdfReader.get_fields()`` (names, /FT types, checkbox on-states) and by
a marker render pinned visually to every printed caption.

What is covered
---------------
- Field-map consistency (no PDF needed): qualified names, no duplicates,
  VALID superset, exclusive-group on-states.
- Map-vs-PDF ground truth: every mapped name EXISTS in the shipped PDF with the
  right field type, and every checkbox's configured on-state is one of the
  field's real /_States_ (via ``pypdf.get_fields()`` on the source).
- Real render round-trips: values are read back from the RENDERED bytes via
  BOTH pypdf text extraction and pymupdf ``bake()`` + ``get_text()``; checked
  boxes are verified POSITIONALLY (an "X" glyph intersecting the widget's
  /Rect). The renderer flattens the AcroForm by design (values are baked so
  they print everywhere), so the output intentionally has no live fields —
  ``get_fields()`` is therefore asserted against the source, and the values
  against the flattened page content.
- Conservative blank-fill, unknown/blank/falsey-checkbox handling, watermark
  modes, and the missing-PDF error path (monkeypatched forms dir — never
  dependent on repo state).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

# Ensure app exports are importable when run straight from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.exports.form_prefill import WatermarkMode
from app.exports.f843_fill import render_843, _f843_form_path
from app.exports.f843_field_map import (
    F843_FIELDS,
    F843_CHECKBOX_FIELDS,
    TAXPAYER_COMPLETED_KEYS,
    VALID_F843_FIELDS,
)

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pypdf_all_text(pdf: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


def _fitz_baked_text(pdf: bytes) -> str:
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    doc.bake()
    return "\n".join(p.get_text() for p in doc)


def _source_checkbox_rects() -> dict[str, tuple[int, tuple[float, float, float, float]]]:
    """Widget /Rects for every checkbox, read from the SOURCE PDF via pymupdf."""
    import fitz

    rects: dict[str, tuple[int, tuple[float, float, float, float]]] = {}
    with fitz.open(_f843_form_path(None)) as src:
        for pno, page in enumerate(src):
            for w in page.widgets() or []:
                rects[w.field_name] = (pno, tuple(w.rect))
    return rects


def _has_x_at(pdf: bytes, page_index: int, rect: tuple[float, float, float, float]) -> bool:
    """True if the rendered output has an 'X' glyph intersecting ``rect``."""
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    doc.bake()
    box = fitz.Rect(rect) + (-1, -1, 1, 1)
    return any(
        w[4] == "X" and fitz.Rect(w[:4]).intersects(box)
        for w in doc[page_index].get_text("words")
    )


def _inp(value: str, ref: str = "intake#1", origin: str = "user_entered") -> dict:
    return {"value": value, "source_ref": ref, "origin": origin}


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF required)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in F843_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_names_qualified_with_on_state(self):
        for key, (name, on_state) in F843_CHECKBOX_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
            assert on_state and on_state != "Off"

    def test_no_duplicate_field_names(self):
        # The old (pre-audit) map aliased one widget to several logical keys —
        # a written box would depend on dict iteration order. Forbid that.
        seen: dict[str, str] = {}
        for key, name in F843_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key
        cb_names = [v[0] for v in F843_CHECKBOX_FIELDS.values()]
        assert len(set(cb_names)) == len(cb_names)
        assert not (set(cb_names) & set(F843_FIELDS.values()))

    def test_valid_fields_is_superset(self):
        assert set(F843_FIELDS.values()) <= VALID_F843_FIELDS
        assert {v[0] for v in F843_CHECKBOX_FIELDS.values()} <= VALID_F843_FIELDS

    def test_key_logical_keys_present(self):
        for k in (
            "name", "ssn", "address", "city", "state", "zip", "ein",
            "period_begin", "period_end", "amount", "irc_section", "explanation",
        ):
            assert k in F843_FIELDS, k

    def test_exclusive_group_on_states(self):
        # Reason-for-filing group: 17 positions, on-states "1".."17" in order.
        for i in range(17):
            key_name = f"{_P1}.c1_1[{i}]"
            matches = [
                (k, on) for k, (n, on) in F843_CHECKBOX_FIELDS.items() if n == key_name
            ]
            assert len(matches) == 1, f"c1_1[{i}] mapped {len(matches)} times"
            assert matches[0][1] == str(i + 1), f"c1_1[{i}] on-state {matches[0][1]}"
        # Line 7 group: 4 positions, on-states "1".."4" in order.
        for i in range(4):
            key_name = f"{_P2}.c2_15[{i}]"
            matches = [
                (k, on) for k, (n, on) in F843_CHECKBOX_FIELDS.items() if n == key_name
            ]
            assert len(matches) == 1
            assert matches[0][1] == str(i + 1)

    def test_line7_reason_keys(self):
        assert F843_CHECKBOX_FIELDS["reason_irs_error"] == (f"{_P2}.c2_15[0]", "1")
        assert F843_CHECKBOX_FIELDS["reason_written_advice"] == (f"{_P2}.c2_15[1]", "2")
        assert F843_CHECKBOX_FIELDS["reason_reasonable_cause"] == (f"{_P2}.c2_15[2]", "3")
        assert F843_CHECKBOX_FIELDS["reason_none_of_above"] == (f"{_P2}.c2_15[3]", "4")

    def test_nothing_withheld_from_autofill(self):
        # Form 843 needs its identity block to be processable; the module
        # deliberately withholds nothing (documented in the field map).
        assert TAXPAYER_COMPLETED_KEYS == frozenset()


# ---------------------------------------------------------------------------
# Map vs. the shipped PDF — pypdf.get_fields() ground truth
# ---------------------------------------------------------------------------

class TestMapMatchesPdf:
    def test_every_mapped_field_exists_with_right_type(self):
        from pypdf import PdfReader

        fields = PdfReader(str(_f843_form_path(None))).get_fields()
        assert fields, "source PDF has no AcroForm fields"

        for key, name in F843_FIELDS.items():
            assert name in fields, f"text field {key} -> {name} not in PDF"
            assert fields[name].get("/FT") == "/Tx", f"{name} is not /Tx"

        for key, (name, on_state) in F843_CHECKBOX_FIELDS.items():
            assert name in fields, f"checkbox {key} -> {name} not in PDF"
            assert fields[name].get("/FT") == "/Btn", f"{name} is not /Btn"
            states = fields[name].get("/_States_") or []
            assert f"/{on_state}" in states, (
                f"{key}: on-state /{on_state} not in {name} states {states}"
            )


# ---------------------------------------------------------------------------
# Render round-trips (real PDF in data/irs_forms/, shipped with the repo)
# ---------------------------------------------------------------------------

class TestRender:
    #: realistic synthetic claim: 1040 late-payment penalty abatement,
    #: reasonable cause.
    INPUTS = {
        "name":           _inp("Riley T Example"),
        "ssn":            _inp("123-45-6789"),
        "address":        _inp("410 Palmetto Ave"),
        "apt_no":         _inp("12B"),
        "city":           _inp("Folly Beach"),
        "state":          _inp("SC"),
        "zip":            _inp("29439"),
        "daytime_phone":  _inp("843-555-0142"),
        "period_begin":   _inp("01/01/2024"),
        "period_end":     _inp("12/31/2024"),
        "amount":         _inp("4,250.00", ref="calc#1", origin="computed"),
        "payment_date_1": _inp("04/15/2025"),
        "irc_section":    _inp("6651(a)(2)"),
        "explanation":    _inp(
            "Reasonable cause: taxpayer was hospitalized during the filing period."
        ),
        "filing_reason_penalty_reasonable_cause": _inp("true"),
        "tax_type_income":         _inp("true"),
        "return_1040":             _inp("true"),
        "reason_reasonable_cause": _inp("true"),
    }
    TEXT_VALUES = (
        "Riley T Example", "123-45-6789", "410 Palmetto Ave", "12B",
        "Folly Beach", "SC", "29439", "843-555-0142", "01/01/2024",
        "12/31/2024", "4,250.00", "04/15/2025", "6651(a)(2)",
        "Reasonable cause: taxpayer was hospitalized during the filing period.",
    )

    def test_round_trip_all_supplied_values_visible(self):
        pdf, ct = render_843(2025, WatermarkMode.DRAFT, dict(self.INPUTS))
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        pypdf_text = _pypdf_all_text(pdf)
        fitz_text = _fitz_baked_text(pdf)
        for needle in self.TEXT_VALUES:
            assert needle in pypdf_text, f"{needle!r} missing (pypdf extract)"
            assert needle in fitz_text, f"{needle!r} missing (pymupdf bake)"

    def test_round_trip_checkboxes_marked_at_their_rects(self):
        pdf, _ = render_843(2025, WatermarkMode.DRAFT, dict(self.INPUTS))
        rects = _source_checkbox_rects()
        for key in (
            "filing_reason_penalty_reasonable_cause",
            "tax_type_income",
            "return_1040",
            "reason_reasonable_cause",
        ):
            fname = F843_CHECKBOX_FIELDS[key][0]
            pno, rect = rects[fname]
            assert _has_x_at(pdf, pno, rect), f"{key} not marked at {fname}"

    def test_falsey_checkbox_left_off(self):
        pdf, _ = render_843(
            2025, WatermarkMode.NONE,
            {
                "tax_type_income":  _inp("true"),
                "tax_type_estate":  _inp("false"),
                "return_1040":      _inp("no"),
            },
        )
        rects = _source_checkbox_rects()
        on_pno, on_rect = rects[F843_CHECKBOX_FIELDS["tax_type_income"][0]]
        assert _has_x_at(pdf, on_pno, on_rect)
        for off_key in ("tax_type_estate", "return_1040"):
            pno, rect = rects[F843_CHECKBOX_FIELDS[off_key][0]]
            assert not _has_x_at(pdf, pno, rect), f"{off_key} must stay off"

    def test_conservative_blank_fill_and_unknown_keys(self):
        pdf, _ = render_843(
            2025, WatermarkMode.DRAFT,
            {
                "name":          _inp("Solo Filer"),
                "amount":        _inp(""),                # blank -> skipped
                "not_a_real_key": _inp("777888"),          # unknown -> skipped
                "bad_payload":   "just-a-string",          # non-dict -> skipped
            },
        )
        doc = _fitz_baked_text(pdf)
        assert "Solo Filer" in doc
        assert "777888" not in doc      # unknown key never wrote
        assert "4,250.00" not in doc    # a figure we never supplied

    def test_all_watermark_modes_render(self):
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_843(2025, mode, {"name": _inp("Water Marks")})
            assert pdf[:4] == b"%PDF"
            assert "Water Marks" in _fitz_baked_text(pdf)

    def test_year_fallback_renders(self):
        # A year with no f843_<year>.pdf falls back to the unversioned f843.pdf.
        pdf, ct = render_843(1999, WatermarkMode.DRAFT, {"name": _inp("Fallback Case")})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        assert "Fallback Case" in _fitz_baked_text(pdf)


# ---------------------------------------------------------------------------
# Path resolution + missing-PDF error path (state-independent: tmp_path only)
# ---------------------------------------------------------------------------

class TestPathResolution:
    def test_prefers_year_specific_pdf(self, tmp_path, monkeypatch):
        import app.exports.f843_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)
        (tmp_path / "f843.pdf").write_bytes(b"%PDF-unversioned")
        (tmp_path / "f843_2025.pdf").write_bytes(b"%PDF-year")
        assert mod._f843_form_path(2025).name == "f843_2025.pdf"
        assert mod._f843_form_path(2024).name == "f843.pdf"   # year file absent
        assert mod._f843_form_path(None).name == "f843.pdf"

    def test_missing_pdf_raises_filenotfound(self, tmp_path, monkeypatch):
        # Redirect the module's forms dir to an EMPTY tmp dir — this must fail
        # identically whether or not the repo checkout has the real PDF.
        import app.exports.f843_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)
        with pytest.raises(FileNotFoundError):
            render_843(2025, WatermarkMode.DRAFT, {"name": _inp("Nobody")})
