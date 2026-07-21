"""Smoke tests for the Form 1096 renderer + field map.

Ground truth is the committed official 2024 fillable PDF
(``data/irs_forms/f1096_2024.pdf``); the form itself is PAGE INDEX 1 of the
3-page file. Field names + on-states were verified against a
``pypdf.PdfReader().get_fields()`` dump and a rasterized marker render.

State independence
------------------
- The missing-PDF test monkeypatches the module's forms dir to a tmp_path —
  it never depends on repo state.
- The success tests REQUIRE the committed source PDF and FAIL (never skip)
  if it is missing: ``_render_acroform_pdf`` flattens its output (widgets +
  AcroForm dropped, values baked into the page), so the round-trip is
  asserted two ways:
    * against the SOURCE pdf via ``pypdf.get_fields()`` — every mapped name
      exists with the right type and on-state;
    * against the RENDERED artifact via pymupdf ``bake()`` + ``get_text()``
      — every supplied value appears, and appears INSIDE its own widget's
      rect (placement, not just presence).
"""

from __future__ import annotations

import io
from functools import lru_cache

import fitz  # pymupdf
import pytest
from pypdf import PdfReader

import app.exports.f1096_fill as f1096_fill
from app.exports.f1096_field_map import (
    F1096_CHECKBOX_FIELDS,
    F1096_FIELDS,
    VALID_F1096_FIELDS,
)
from app.exports.f1096_fill import render_1096
from app.exports.form_prefill import IRS_FORMS_DIR, WatermarkMode

SOURCE_PDF = IRS_FORMS_DIR / "f1096_2024.pdf"
FORM_PAGE = 1  # page 0 = "Attention" notice, page 1 = the form, page 2 = instructions
TAX_YEAR = 2024  # matches the committed revision


def _inp(value: str) -> dict:
    return {"value": value, "source_ref": "test#1", "origin": "user_entered"}


@lru_cache(maxsize=1)
def _source_widget_rects() -> dict[str, tuple[float, float, float, float]]:
    """Fully-qualified field name -> widget rect (fitz/top-left coords) on the form page."""
    reader = PdfReader(str(SOURCE_PDF))
    page = reader.pages[FORM_PAGE]
    page_h = float(page.mediabox.height)
    rects: dict[str, tuple[float, float, float, float]] = {}
    for ref in page.get("/Annots") or []:
        obj = ref.get_object()
        if obj.get("/Subtype") != "/Widget":
            continue
        parts: list[str] = []
        node = obj
        while node is not None:
            t = node.get("/T")
            if t is not None:
                parts.append(str(t))
            parent = node.get("/Parent")
            node = parent.get_object() if parent is not None else None
        fq = ".".join(reversed(parts))
        x0, y0, x1, y1 = (float(v) for v in obj["/Rect"])
        rects[fq] = (
            min(x0, x1), page_h - max(y0, y1), max(x0, x1), page_h - min(y0, y1)
        )
    return rects


def _baked_form_page(pdf_bytes: bytes) -> str:
    """Full text of the baked form page of a rendered artifact."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    doc.bake()
    text = doc[FORM_PAGE].get_text()
    doc.close()
    return text


def _clip_text(pdf_bytes: bytes, acro_name: str, pad: float = 2.0) -> str:
    """Baked text INSIDE the given field's widget rect on the rendered artifact."""
    x0, y0, x1, y1 = _source_widget_rects()[acro_name]
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    doc.bake()
    clip = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad)
    text = doc[FORM_PAGE].get_text("text", clip=clip)
    doc.close()
    return text


# ---------------------------------------------------------------------------
# Path resolution / missing PDF — never depends on repo state
# ---------------------------------------------------------------------------

def test_missing_pdf_raises(monkeypatch, tmp_path):
    """With the forms dir pointed at an empty tmp dir, render raises (no skip)."""
    monkeypatch.setattr(f1096_fill, "IRS_FORMS_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="Form 1096"):
        render_1096(
            tax_year=TAX_YEAR,
            watermark_mode=WatermarkMode.DRAFT,
            inputs={"filer_name": _inp("Nope Inc")},
        )


def test_form_path_prefers_year_specific(monkeypatch, tmp_path):
    """f1096_<year>.pdf wins over f1096.pdf; generic is the fallback."""
    monkeypatch.setattr(f1096_fill, "IRS_FORMS_DIR", tmp_path)
    generic = tmp_path / "f1096.pdf"
    yearfile = tmp_path / f"f1096_{TAX_YEAR}.pdf"
    generic.write_bytes(b"%PDF-fake")
    assert f1096_fill._f1096_form_path(TAX_YEAR) == generic  # only generic exists
    yearfile.write_bytes(b"%PDF-fake")
    assert f1096_fill._f1096_form_path(TAX_YEAR) == yearfile  # year-specific wins
    assert f1096_fill._f1096_form_path(None) == generic


def test_source_pdf_is_committed():
    """The 2024 source PDF ships with the repo — success tests below fail, not skip."""
    assert SOURCE_PDF.exists(), (
        f"{SOURCE_PDF} is missing — run scripts/download_irs_forms.sh; "
        "the Form 1096 tests are meant to run for real, not skip."
    )


# ---------------------------------------------------------------------------
# Field map vs. the SOURCE pdf (the pypdf get_fields round-trip)
# ---------------------------------------------------------------------------

def test_field_map_matches_source_pdf():
    fields = PdfReader(str(SOURCE_PDF)).get_fields()
    assert fields, "source PDF has no AcroForm fields?"

    for key, name in F1096_FIELDS.items():
        assert name in fields, f"text key {key!r}: {name} not in PDF"
        assert fields[name].get("/FT") == "/Tx", f"{key!r}: {name} is not /Tx"

    seen_states: set[str] = set()
    for key, (name, on_state) in F1096_CHECKBOX_FIELDS.items():
        assert name in fields, f"checkbox key {key!r}: {name} not in PDF"
        assert fields[name].get("/FT") == "/Btn", f"{key!r}: {name} is not /Btn"
        states = fields[name].get("/_States_") or []
        assert f"/{on_state}" in [str(s) for s in states], (
            f"{key!r}: on-state /{on_state} not in {states}"
        )
        seen_states.add(on_state)

    # 34 grid boxes, each with a UNIQUE on-state /1../34, no shared targets.
    assert len(F1096_CHECKBOX_FIELDS) == 34
    assert seen_states == {str(i) for i in range(1, 35)}
    assert len(VALID_F1096_FIELDS) == len(F1096_FIELDS) + len(F1096_CHECKBOX_FIELDS)

    # Every mapped widget has a rect on the form page (placement is checkable).
    rects = _source_widget_rects()
    for name in VALID_F1096_FIELDS:
        assert name in rects, f"{name} has no widget on page {FORM_PAGE}"


# ---------------------------------------------------------------------------
# Rendered-artifact round-trips (pymupdf bake + get_text, placement included)
# ---------------------------------------------------------------------------

REALISTIC = {
    "filer_name": "Magic Unicorn Tech Inc",
    "address_line1": "1400 Palmetto Blvd Suite 42",
    "city_state_zip": "Folly Beach, SC 29439",
    "contact_person": "Aaron Stransky",
    "phone": "843-555-0142",
    "email": "books@magicunicorn.tech",
    "fax": "843-555-0143",
    "ein": "12-3456789",
    "box_3_total_forms": "12",
    "box_4_withheld": "1500.00",
    "box_5_total_amount": "84250.00",
    "signer_title": "President",
}


def test_render_realistic_roundtrip():
    inputs = {k: _inp(v) for k, v in REALISTIC.items()}
    inputs["box_6_1099_nec"] = _inp("true")

    pdf_bytes, content_type = render_1096(TAX_YEAR, WatermarkMode.DRAFT, inputs)
    assert content_type == "application/pdf"
    assert pdf_bytes.startswith(b"%PDF")

    # Every supplied value appears on the baked form page…
    text = _baked_form_page(pdf_bytes)
    for key, value in REALISTIC.items():
        assert value in text, f"{key!r} value {value!r} missing from baked page"
    assert "DRAFT" in text  # watermark path intact

    # …and appears INSIDE its own widget's rect (placement, not just presence).
    for key, value in REALISTIC.items():
        clip = _clip_text(pdf_bytes, F1096_FIELDS[key])
        assert value in clip, f"{key!r} value not inside its own box: {clip!r}"

    # The checked box bakes an X inside the 1099-NEC cell, and only there.
    nec_name, _ = F1096_CHECKBOX_FIELDS["box_6_1099_nec"]
    misc_name, _ = F1096_CHECKBOX_FIELDS["box_6_1099_misc"]
    assert "X" in _clip_text(pdf_bytes, nec_name)
    assert "X" not in _clip_text(pdf_bytes, misc_name)

    # The artifact is flattened by design: values are BAKED, no live AcroForm.
    assert not PdfReader(io.BytesIO(pdf_bytes)).get_fields()


def test_marker_every_text_key_lands_in_its_own_box():
    """A unique marker per mapped text key must land inside that key's rect."""
    markers = {key: f"MK{i:02d}Z" for i, key in enumerate(F1096_FIELDS, start=1)}
    pdf_bytes, _ = render_1096(
        TAX_YEAR,
        WatermarkMode.NONE,
        {key: _inp(m) for key, m in markers.items()},
    )
    for key, marker in markers.items():
        clip = _clip_text(pdf_bytes, F1096_FIELDS[key])
        assert marker in clip, f"{key!r} marker {marker} not in its box: {clip!r}"


@pytest.mark.parametrize(
    "cb_key",
    [
        "box_6_w2g",       # /1 — first cell
        "box_6_1099_k",    # /16 — unnamed wrapper, hangs directly off Page1[0]
        "box_6_1099_nec",  # /20 — the common case
        "box_6_5498_qa",   # /33 — the other unnamed wrapper
        "box_6_5498_sa",   # /34 — lone row-3 cell
    ],
)
def test_box6_checkbox_bakes_x_in_its_own_cell(cb_key):
    pdf_bytes, _ = render_1096(
        TAX_YEAR,
        WatermarkMode.NONE,
        {"filer_name": _inp("CB Check Inc"), cb_key: _inp("true")},
    )
    name, _ = F1096_CHECKBOX_FIELDS[cb_key]
    assert "X" in _clip_text(pdf_bytes, name), f"{cb_key}: no X in its cell"
    # …and no stray X in a different cell of the grid.
    other = "box_6_1098" if cb_key != "box_6_1098" else "box_6_w2g"
    assert "X" not in _clip_text(pdf_bytes, F1096_CHECKBOX_FIELDS[other][0])


def test_multiple_box6_raises():
    with pytest.raises(ValueError, match="only one form type"):
        render_1096(
            TAX_YEAR,
            WatermarkMode.DRAFT,
            inputs={
                "filer_name": _inp("Test Corp"),
                "box_6_1099_nec": _inp("true"),
                "box_6_1099_misc": _inp("true"),
            },
        )


def test_falsy_box6_values_do_not_count_or_check():
    """'false'/'no' box-6 values neither check the box nor trip the exclusivity guard."""
    pdf_bytes, _ = render_1096(
        TAX_YEAR,
        WatermarkMode.NONE,
        inputs={
            "box_6_1099_nec": _inp("true"),
            "box_6_1099_misc": _inp("false"),
            "box_6_w2g": _inp("no"),
        },
    )
    assert "X" in _clip_text(pdf_bytes, F1096_CHECKBOX_FIELDS["box_6_1099_nec"][0])
    assert "X" not in _clip_text(pdf_bytes, F1096_CHECKBOX_FIELDS["box_6_1099_misc"][0])
    assert "X" not in _clip_text(pdf_bytes, F1096_CHECKBOX_FIELDS["box_6_w2g"][0])


def test_blank_values_and_unknown_keys_are_skipped():
    """Blank/None values and unknown keys (incl. keys DROPPED from the blind map
    because the 2024 revision has no such widgets) render nothing — no crash,
    no stray text."""
    pdf_bytes, content_type = render_1096(
        TAX_YEAR,
        WatermarkMode.DRAFT,
        inputs={
            "filer_name": _inp(""),                    # blank -> skipped
            "box_3_total_forms": {"value": None, "source_ref": "t", "origin": "u"},
            "ein": _inp("   "),                        # whitespace-only -> skipped
            "final_return": _inp("UNK1MARK"),          # no such box on Form 1096
            "box_6_8849": _inp("true"),                # not on the 1096 grid
            "address_line2": _inp("UNK2MARK"),         # no second street line
            "totally_unknown": _inp("UNK3MARK"),
            "not_a_payload": "raw-string-ignored",     # non-dict payload
        },
    )
    assert content_type == "application/pdf"
    text = _baked_form_page(pdf_bytes)
    for stray in ("UNK1MARK", "UNK2MARK", "UNK3MARK", "raw-string-ignored"):
        assert stray not in text


def test_legacy_city_state_zip_parts_join_into_combined_box():
    """Separate city/state/zip_code inputs are transcribed into the single
    combined city/state/ZIP box; an explicit city_state_zip wins over parts."""
    pdf_bytes, _ = render_1096(
        TAX_YEAR,
        WatermarkMode.NONE,
        inputs={
            "city": _inp("Folly Beach"),
            "state": _inp("SC"),
            "zip_code": _inp("29439"),
        },
    )
    assert "Folly Beach, SC 29439" in _clip_text(pdf_bytes, F1096_FIELDS["city_state_zip"])

    pdf_bytes2, _ = render_1096(
        TAX_YEAR,
        WatermarkMode.NONE,
        inputs={
            "city_state_zip": _inp("Columbia, SC 29201"),
            "city": _inp("Ignoredville"),
        },
    )
    clip = _clip_text(pdf_bytes2, F1096_FIELDS["city_state_zip"])
    assert "Columbia, SC 29201" in clip
    assert "Ignoredville" not in clip


def test_watermark_none_has_no_draft_stamp():
    pdf_bytes, _ = render_1096(
        TAX_YEAR,
        WatermarkMode.NONE,
        inputs={"filer_name": _inp("No Watermark Inc")},
    )
    text = _baked_form_page(pdf_bytes)
    assert "No Watermark Inc" in text
    assert "DRAFT" not in text
