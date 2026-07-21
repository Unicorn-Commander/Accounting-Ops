"""Unit tests for the Form 7004 (business extension) renderer + field map.

Three layers, mirroring the 4868/1120 house pattern but with the field map
pinned to a fresh ``pypdf`` dump of the official PDF:

1. Field-map consistency (no PDF needed): qualified names, no duplicates,
   ground-truth pins for every tricky assignment (the IRS ``f1_NN`` numbering
   is tab-order, not line-order), taxpayer-completed EIN posture.
2. Map-vs-PDF audit (source PDF required): every mapped name exists in the
   real AcroForm with the right /FT type and checkbox on-state, and the map
   covers EVERY terminal /Tx + /Btn field on the form.
3. Render round-trip (source PDF required): values land VISIBLY inside their
   own widget rects (positional assertion via pymupdf clip — text presence
   alone does not prove placement), the output is flattened/print-ready, the
   EIN is never auto-filled, and blank/unknown/malformed inputs stay blank.

The missing-PDF behavior is tested with a monkeypatched forms dir, so it never
depends on whether this checkout has ``data/irs_forms/f7004.pdf``. Render
tests skip ONLY when the gitignored source PDF is genuinely absent (CI); when
the PDF is present every test runs.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from app.exports.form_prefill import WatermarkMode
from app.exports.f7004_fill import render_7004, _f7004_form_path
from app.exports.f7004_field_map import (
    F7004_CHECKBOX_FIELDS,
    F7004_FIELDS,
    F7004_TAXPAYER_COMPLETED_KEYS,
    VALID_F7004_FIELDS,
)

_P1 = "topmostSubform[0].Page1[0]"
_SRC = _f7004_form_path(None)


def _needs_pdf():
    if not _SRC.exists():
        pytest.skip("Form 7004 PDF absent (gitignored) — skipping render test")


def _inp(value: str) -> dict:
    return {"value": value, "source_ref": "test#1", "origin": "user_entered"}


# ---------------------------------------------------------------------------
# 1. Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in F7004_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_names_qualified(self):
        for key, (name, on_state) in F7004_CHECKBOX_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
            assert on_state and on_state != "Off"

    def test_no_duplicate_field_names(self):
        seen: dict[str, str] = {}
        for key, name in F7004_FIELDS.items():
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key
        cb_names = [v[0] for v in F7004_CHECKBOX_FIELDS.values()]
        assert len(set(cb_names)) == len(cb_names)
        assert not (set(cb_names) & set(F7004_FIELDS.values()))

    def test_exact_logical_key_surface(self):
        # The complete caller-facing surface, pinned. ("form_code" is a
        # renderer-level convenience, not a map entry.)
        assert set(F7004_FIELDS) == {
            "name", "ein", "address", "room_suite", "city_town", "state",
            "country", "zip_code", "form_code_digit1", "form_code_digit2",
            "calendar_year_yy", "fiscal_year_begin", "fiscal_year_begin_yy",
            "fiscal_year_end", "fiscal_year_end_yy",
            "tentative_tax_6", "payments_7", "balance_due_8",
        }
        assert set(F7004_CHECKBOX_FIELDS) == {
            "foreign_corp_2", "consolidated_3", "regs_qualifier_4",
            "short_year_initial", "short_year_final", "short_year_accounting",
            "short_year_consolidated", "short_year_other",
        }

    def test_ground_truth_pins(self):
        # Pinned against the pypdf dump of the official PDF. The f1_NN numbers
        # run in TAB order: the header eats f1_1..f1_8 (EIN is f1_2, NOT the
        # address), line 1 is TWO comb digits (f1_9/f1_10), line 5a spans
        # f1_11..f1_15, and the dollar lines are f1_16..f1_18.
        assert F7004_FIELDS["name"] == f"{_P1}.f1_1[0]"
        assert F7004_FIELDS["ein"] == f"{_P1}.f1_2[0]"
        assert F7004_FIELDS["address"] == f"{_P1}.f1_3[0]"
        assert F7004_FIELDS["room_suite"] == f"{_P1}.f1_4[0]"
        assert F7004_FIELDS["city_town"] == f"{_P1}.f1_5[0]"
        assert F7004_FIELDS["state"] == f"{_P1}.f1_6[0]"
        assert F7004_FIELDS["country"] == f"{_P1}.f1_7[0]"
        assert F7004_FIELDS["zip_code"] == f"{_P1}.f1_8[0]"
        assert F7004_FIELDS["form_code_digit1"] == f"{_P1}.f1_9[0]"
        assert F7004_FIELDS["form_code_digit2"] == f"{_P1}.f1_10[0]"
        assert F7004_FIELDS["calendar_year_yy"] == f"{_P1}.f1_11[0]"
        assert F7004_FIELDS["fiscal_year_begin"] == f"{_P1}.f1_12[0]"
        assert F7004_FIELDS["fiscal_year_begin_yy"] == f"{_P1}.f1_13[0]"
        assert F7004_FIELDS["fiscal_year_end"] == f"{_P1}.f1_14[0]"
        assert F7004_FIELDS["fiscal_year_end_yy"] == f"{_P1}.f1_15[0]"
        assert F7004_FIELDS["tentative_tax_6"] == f"{_P1}.f1_16[0]"
        assert F7004_FIELDS["payments_7"] == f"{_P1}.f1_17[0]"
        assert F7004_FIELDS["balance_due_8"] == f"{_P1}.f1_18[0]"

    def test_checkbox_on_states_pinned(self):
        # Lines 2-4 are plain /1 boxes; line 5b's five reason boxes have
        # DISTINCT on-states /1../5 (from each field's /_States_).
        assert F7004_CHECKBOX_FIELDS["foreign_corp_2"] == (f"{_P1}.c1_1[0]", "1")
        assert F7004_CHECKBOX_FIELDS["consolidated_3"] == (f"{_P1}.c1_2[0]", "1")
        assert F7004_CHECKBOX_FIELDS["regs_qualifier_4"] == (f"{_P1}.c1_3[0]", "1")
        assert F7004_CHECKBOX_FIELDS["short_year_initial"] == (f"{_P1}.c1_4[0]", "1")
        assert F7004_CHECKBOX_FIELDS["short_year_final"] == (f"{_P1}.c1_4[1]", "2")
        assert F7004_CHECKBOX_FIELDS["short_year_accounting"] == (f"{_P1}.c1_4[2]", "3")
        assert F7004_CHECKBOX_FIELDS["short_year_consolidated"] == (f"{_P1}.c1_4[3]", "4")
        assert F7004_CHECKBOX_FIELDS["short_year_other"] == (f"{_P1}.c1_4[4]", "5")

    def test_ein_mapped_but_taxpayer_completed(self):
        assert "ein" in F7004_FIELDS
        assert F7004_TAXPAYER_COMPLETED_KEYS == frozenset({"ein"})

    def test_valid_fields_is_superset(self):
        assert set(F7004_FIELDS.values()) <= VALID_F7004_FIELDS
        assert {v[0] for v in F7004_CHECKBOX_FIELDS.values()} <= VALID_F7004_FIELDS


# ---------------------------------------------------------------------------
# 2. Map-vs-PDF audit (requires the IRS PDF; skipped only when absent, as in CI)
# ---------------------------------------------------------------------------

class TestFieldMapMatchesPdf:
    def test_every_mapped_field_exists_with_right_type_and_states(self):
        _needs_pdf()
        from pypdf import PdfReader

        fields = PdfReader(str(_SRC)).get_fields()
        assert fields, "source PDF has no AcroForm fields"

        for key, name in F7004_FIELDS.items():
            f = fields.get(name)
            assert f is not None, f"{key}: {name} not in PDF"
            assert f.get("/FT") == "/Tx", f"{key}: {name} is {f.get('/FT')}, not /Tx"

        for key, (name, on_state) in F7004_CHECKBOX_FIELDS.items():
            f = fields.get(name)
            assert f is not None, f"{key}: {name} not in PDF"
            assert f.get("/FT") == "/Btn", f"{key}: {name} is {f.get('/FT')}, not /Btn"
            states = [str(s) for s in (f.get("/_States_") or [])]
            assert f"/{on_state}" in states, (
                f"{key}: on-state /{on_state} not in {states} for {name}"
            )

    def test_map_covers_every_terminal_field(self):
        # The form has no fillable widget we do not know about — full-coverage
        # guard against a silently revised PDF.
        _needs_pdf()
        from pypdf import PdfReader

        fields = PdfReader(str(_SRC)).get_fields()
        terminals = {
            name for name, f in fields.items() if f.get("/FT") in ("/Tx", "/Btn")
        }
        assert terminals == set(VALID_F7004_FIELDS)


# ---------------------------------------------------------------------------
# 3. Render round-trip (requires the IRS PDF; skipped only when absent)
# ---------------------------------------------------------------------------

def _widget_rects(src: Path) -> dict[str, tuple[int, tuple[float, float, float, float]]]:
    """Fully-qualified widget name -> (page_index, /Rect) from the SOURCE PDF."""
    from pypdf import PdfReader

    out: dict[str, tuple[int, tuple[float, float, float, float]]] = {}
    for pno, page in enumerate(PdfReader(str(src)).pages):
        for ref in page.get("/Annots") or []:
            obj = ref.get_object()
            if obj.get("/Subtype") != "/Widget":
                continue
            parts: list[str] = []
            o = obj
            while o is not None:
                t = o.get("/T")
                if t is not None:
                    parts.append(str(t))
                parent = o.get("/Parent")
                o = parent.get_object() if parent is not None else None
            name = ".".join(reversed(parts))
            r = obj.get("/Rect")
            out[name] = (pno, tuple(float(v) for v in r))
    return out


def _baked_doc(pdf: bytes):
    import fitz

    doc = fitz.open(stream=pdf, filetype="pdf")
    doc.bake()
    return doc


def _clip_text(doc, pno: int, rect: tuple[float, float, float, float]) -> str:
    """Text inside a pypdf (bottom-left origin) rect, with a small pad."""
    import fitz

    page = doc[pno]
    h = page.rect.height
    x0, y0, x1, y1 = rect
    clip = fitz.Rect(x0 - 2, h - y1 - 2, x1 + 12, h - y0 + 2)
    return page.get_text(clip=clip)


class TestRender:
    def test_full_round_trip_every_value_placed(self):
        # Every text key (via the caller-facing surface, incl. the "form_code"
        # convenience split) + two checkboxes: each supplied value must appear
        # INSIDE ITS OWN widget rect in the baked output — placement, not mere
        # presence — and in both pypdf and pymupdf read-backs.
        _needs_pdf()
        from pypdf import PdfReader

        text_values = {
            "name":       "Acme Widgets Inc",
            "address":    "4821 Commerce Blvd",
            "room_suite": "Ste 310",
            "city_town":  "Folly Beach",
            "state":      "SC",
            "country":    "USA",
            "zip_code":   "29439",
            "calendar_year_yy":     "25",
            "fiscal_year_begin":    "Jul 1",
            "fiscal_year_begin_yy": "25",
            "fiscal_year_end":      "Jun 30",
            "fiscal_year_end_yy":   "26",
            "tentative_tax_6": "48210",
            "payments_7":      "35000",
            "balance_due_8":   "13210",
        }
        inputs = {k: _inp(v) for k, v in text_values.items()}
        inputs["form_code"] = _inp("12")           # -> "1" | "2" comb boxes
        inputs["foreign_corp_2"] = _inp("true")
        inputs["short_year_accounting"] = _inp("yes")

        pdf, ct = render_7004(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"

        # pypdf read-back: the output is FLATTENED print-ready (no live
        # AcroForm left to hold values) and every long value is extractable.
        out_reader = PdfReader(io.BytesIO(pdf))
        assert out_reader.get_fields() is None
        p1 = out_reader.pages[0].extract_text()
        for needle in (
            "Acme Widgets Inc", "4821 Commerce Blvd", "Ste 310", "Folly Beach",
            "29439", "Jul 1", "Jun 30", "48210", "35000", "13210",
        ):
            assert needle in p1, f"{needle!r} not visible via pypdf extract_text"

        # pymupdf positional read-back: clip each SOURCE widget rect on the
        # baked output and require that box to contain its own value.
        rects = _widget_rects(_SRC)
        doc = _baked_doc(pdf)
        try:
            for key, value in text_values.items():
                pno, rect = rects[F7004_FIELDS[key]]
                clip = _clip_text(doc, pno, rect)
                assert value in clip, f"{key}: {value!r} not inside its box (got {clip!r})"
            for key, digit in (("form_code_digit1", "1"), ("form_code_digit2", "2")):
                pno, rect = rects[F7004_FIELDS[key]]
                assert digit in _clip_text(doc, pno, rect), f"{key} missing {digit!r}"
            for cb in ("foreign_corp_2", "short_year_accounting"):
                name, _on = F7004_CHECKBOX_FIELDS[cb]
                pno, rect = rects[name]
                assert "X" in _clip_text(doc, pno, rect), f"{cb} box not checked"
        finally:
            doc.close()

    def test_ein_never_filled(self):
        _needs_pdf()
        pdf, _ = render_7004(
            2025, WatermarkMode.DRAFT,
            {"name": _inp("Acme Widgets Inc"), "ein": _inp("82-4821337")},
        )
        doc = _baked_doc(pdf)
        try:
            text = "\n".join(p.get_text() for p in doc)
        finally:
            doc.close()
        assert "Acme Widgets Inc" in text
        assert "82-4821337" not in text          # identity item left for the filer
        rects = _widget_rects(_SRC)
        doc = _baked_doc(pdf)
        try:
            pno, rect = rects[F7004_FIELDS["ein"]]
            assert "4821337" not in _clip_text(doc, pno, rect)
        finally:
            doc.close()

    def test_conservative_blank_fill_and_unknown_keys(self):
        # Blank / None / unknown / non-dict inputs neither raise nor write;
        # nothing we did not supply becomes visible.
        _needs_pdf()
        pdf, _ = render_7004(
            2025, WatermarkMode.NONE,
            {
                "name":            _inp("Solo Filer LLC"),
                "tentative_tax_6": _inp(""),               # blank -> skipped
                "payments_7":      {"value": None, "source_ref": "x", "origin": "x"},
                "not_a_real_key":  _inp("77742"),          # unknown -> skipped
                "balance_due_8":   "77742",                # non-dict -> skipped
            },
        )
        doc = _baked_doc(pdf)
        try:
            text = "\n".join(p.get_text() for p in doc)
        finally:
            doc.close()
        assert "Solo Filer LLC" in text
        assert "77742" not in text

    def test_form_code_malformed_skipped(self):
        # Line 1 is a 2-digit comb; a 1- or 3-char code is never guessed into
        # the boxes. Assert positionally: both comb boxes stay empty.
        _needs_pdf()
        rects = _widget_rects(_SRC)
        for bad in ("1", "123"):
            pdf, _ = render_7004(
                2025, WatermarkMode.NONE,
                {"form_code": _inp(bad), "name": _inp("Malformed Code Co")},
            )
            doc = _baked_doc(pdf)
            try:
                for key in ("form_code_digit1", "form_code_digit2"):
                    pno, rect = rects[F7004_FIELDS[key]]
                    clip = _clip_text(doc, pno, rect)
                    assert not any(ch.isdigit() for ch in clip), (
                        f"form_code={bad!r} leaked into {key}: {clip!r}"
                    )
            finally:
                doc.close()

    def test_checkbox_falsy_left_off_and_distinct_on_states(self):
        # A falsy checkbox stays empty; setting one 5b reason marks THAT box
        # (each has its own on-state) and not its neighbors.
        _needs_pdf()
        rects = _widget_rects(_SRC)
        pdf, _ = render_7004(
            2025, WatermarkMode.NONE,
            {
                "foreign_corp_2":   _inp("false"),
                "short_year_final": _inp("true"),
            },
        )
        doc = _baked_doc(pdf)
        try:
            name, _on = F7004_CHECKBOX_FIELDS["foreign_corp_2"]
            pno, rect = rects[name]
            assert "X" not in _clip_text(doc, pno, rect)

            name, _on = F7004_CHECKBOX_FIELDS["short_year_final"]
            pno, rect = rects[name]
            assert "X" in _clip_text(doc, pno, rect)

            for neighbor in ("short_year_initial", "short_year_other"):
                name, _on = F7004_CHECKBOX_FIELDS[neighbor]
                pno, rect = rects[name]
                assert "X" not in _clip_text(doc, pno, rect)
        finally:
            doc.close()

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"name": _inp("Watermark Modes Inc")}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, ct = render_7004(2025, mode, inputs)
            assert ct == "application/pdf" and pdf[:4] == b"%PDF"
            doc = _baked_doc(pdf)
            try:
                assert "Watermark Modes Inc" in doc[0].get_text()
            finally:
                doc.close()

    def test_year_fallback_renders(self):
        # A year with no f7004_<year>.pdf falls back to the unversioned PDF;
        # empty inputs return a valid (unfilled) official form.
        _needs_pdf()
        pdf, ct = render_7004(1999, WatermarkMode.DRAFT, {})
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# Missing-PDF + path resolution (state-independent: never touches the repo's
# data/irs_forms, so these pass whether or not the PDF is present)
# ---------------------------------------------------------------------------

class TestPathResolutionAndMissingPdf:
    def test_missing_pdf_raises(self, monkeypatch, tmp_path):
        import app.exports.f7004_fill as mod

        monkeypatch.setattr(
            mod, "_f7004_form_path", lambda ty: tmp_path / "f7004.pdf"
        )
        with pytest.raises(FileNotFoundError, match="IRS Form 7004 PDF not found"):
            render_7004(2025, WatermarkMode.DRAFT, {"name": _inp("X Corp")})

    def test_path_resolution_prefers_year_specific(self, monkeypatch, tmp_path):
        import app.exports.f7004_fill as mod

        monkeypatch.setattr(mod, "IRS_FORMS_DIR", tmp_path)

        # Nothing on disk: resolves to the (nonexistent) unversioned fallback.
        assert mod._f7004_form_path(2025) == tmp_path / "f7004.pdf"
        assert not mod._f7004_form_path(2025).exists()

        # Unversioned present: chosen for any year and for None.
        (tmp_path / "f7004.pdf").write_bytes(b"%PDF-stub")
        assert mod._f7004_form_path(2025) == tmp_path / "f7004.pdf"
        assert mod._f7004_form_path(None) == tmp_path / "f7004.pdf"

        # Year-specific present: preferred for that year only.
        (tmp_path / "f7004_2025.pdf").write_bytes(b"%PDF-stub")
        assert mod._f7004_form_path(2025) == tmp_path / "f7004_2025.pdf"
        assert mod._f7004_form_path(2024) == tmp_path / "f7004.pdf"
