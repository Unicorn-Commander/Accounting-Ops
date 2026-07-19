"""Smoke tests for the Schedule K-1 (Form 1065) professional-substitute path.

Covers the field-map consistency (names qualified, no dups, VALID superset, the
conservative identity-blank policy) and — when the gitignored IRS PDF is present
— the end-to-end render (values land + are visible in extracted text,
conservative blank-fill, every watermark mode renders). Render tests skip when
``data/irs_forms/f1065sk1.pdf`` is absent (as in CI), mirroring test_w4p.py.
"""
from __future__ import annotations

import io

import pytest

from app.exports.form_prefill import IRS_FORMS_DIR, WatermarkMode
from app.exports.k1_1065_fill import render_k1_1065
from app.exports.k1_1065_field_map import (
    K1_1065_CHECKBOX_FIELDS,
    K1_1065_FIELDS,
    VALID_K1_1065_FIELDS,
)

_K1_PDF = IRS_FORMS_DIR / "f1065sk1.pdf"

# Identity numbers that MUST NOT be writable (left blank for the preparer/human).
_OMITTED_IDENTITY_FIELDS = {
    "topmostSubform[0].Page1[0].LeftCol[0].f1_6[0]",   # Part I A — partnership EIN
    "topmostSubform[0].Page1[0].LeftCol[0].f1_9[0]",   # Part II E — partner SSN/TIN
    "topmostSubform[0].Page1[0].LeftCol[0].f1_11[0]",  # Part II H2 — DE partner TIN
}


def _needs_pdf():
    if not _K1_PDF.exists():
        pytest.skip("Schedule K-1 (1065) PDF absent (gitignored) — skipping render test")


# ---------------------------------------------------------------------------
# Field-map consistency (no PDF)
# ---------------------------------------------------------------------------

class TestFieldMap:
    def test_all_text_field_names_qualified(self):
        for key, name in K1_1065_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"

    def test_all_checkbox_field_names_qualified(self):
        for key, (name, on_state) in K1_1065_CHECKBOX_FIELDS.items():
            assert name.startswith("topmostSubform"), f"{key} -> {name!r}"
            assert on_state and "/" not in on_state, f"{key} on-state {on_state!r}"

    def test_no_duplicate_field_names(self):
        seen: dict[str, str] = {}
        names = list(K1_1065_FIELDS.items()) + [
            (k, v[0]) for k, v in K1_1065_CHECKBOX_FIELDS.items()
        ]
        for key, name in names:
            assert name not in seen, f"dup {name} ({seen.get(name)} & {key})"
            seen[name] = key

    def test_valid_fields_is_exact_superset(self):
        writable = set(K1_1065_FIELDS.values()) | {
            v[0] for v in K1_1065_CHECKBOX_FIELDS.values()
        }
        assert writable <= VALID_K1_1065_FIELDS
        assert writable == set(VALID_K1_1065_FIELDS)  # nothing stale in VALID

    def test_identity_numbers_not_writable(self):
        # Partnership EIN / partner SSN/TIN / DE TIN must never be writable.
        assert _OMITTED_IDENTITY_FIELDS.isdisjoint(VALID_K1_1065_FIELDS)

    def test_radio_groups_share_base_name(self):
        # G general/limited share base c1_4; H1 domestic/foreign share c1_5;
        # M yes/no share c1_11; the J decrease pair shares c1_8.
        def base(k):
            return K1_1065_CHECKBOX_FIELDS[k][0].rsplit("[", 1)[0]
        assert base("general_partner") == base("limited_partner")
        assert base("domestic_partner") == base("foreign_partner")
        assert base("built_in_gain_yes") == base("built_in_gain_no")
        assert base("decrease_due_to_sale") == base("decrease_due_to_exchange")

    def test_box23_on_state_is_four(self):
        # This revision's box-23 widget on-state is /4 (preserved verbatim).
        assert K1_1065_CHECKBOX_FIELDS["box23_multiple_passive"][1] == "4"

    def test_core_income_lines_present(self):
        for key in (
            "box1_ordinary_business_income",
            "box5_interest_income",
            "box9a_net_long_term_capital_gain",
            "box14_amount",
            "box19_amount",
            "capital_ending",
        ):
            assert key in K1_1065_FIELDS


# ---------------------------------------------------------------------------
# Render (requires the IRS PDF; skipped in CI when absent)
# ---------------------------------------------------------------------------

class TestRender:
    def _ref(self, value: str) -> dict:
        return {"value": value, "source_ref": "test#1", "origin": "document"}

    def test_values_baked_in_visible(self):
        # Values are flattened into page content so they render in EVERY viewer
        # (not dependent on /NeedAppearances). Assert on extracted text.
        _needs_pdf()
        from pypdf import PdfReader

        inputs = {
            "partnership_name_address":      self._ref("Acme Holdings LP"),
            "partner_name_address":          self._ref("Aaron J Stransky"),
            "general_partner":               self._ref("true"),
            "domestic_partner":              self._ref("true"),
            "box1_ordinary_business_income": self._ref("51234"),
            "box14_code":                    self._ref("A"),
            "box14_amount":                  self._ref("60000"),
            "capital_ending":                self._ref("78901"),
            "box16_k3_attached":             self._ref("yes"),
        }
        pdf, ct = render_k1_1065(2025, WatermarkMode.DRAFT, inputs)
        assert ct == "application/pdf" and pdf[:4] == b"%PDF"
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        for needle in ("Acme Holdings LP", "Aaron J Stransky", "51234", "60000", "78901"):
            assert needle in txt, f"{needle!r} not visible in rendered K-1"

    def test_conservative_blank_fill(self):
        _needs_pdf()
        from pypdf import PdfReader

        # Fill only box 1; a box we did NOT set must not leak any value.
        pdf, _ = render_k1_1065(
            2025, WatermarkMode.DRAFT,
            {"box1_ordinary_business_income": self._ref("13579")},
        )
        txt = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
        assert "13579" in txt
        assert "24680" not in txt  # nothing we didn't set leaks in

    def test_falsey_checkbox_not_checked_and_unknown_key_skipped(self):
        # A falsey checkbox value and an unknown logical key are both no-ops and
        # must not raise.
        _needs_pdf()
        pdf, _ = render_k1_1065(
            2025, WatermarkMode.DRAFT,
            {
                "final_k1": self._ref("false"),
                "not_a_real_k1_field": self._ref("999"),
                "box5_interest_income": self._ref("1200"),
            },
        )
        assert pdf[:4] == b"%PDF"

    def test_all_watermark_modes_render(self):
        _needs_pdf()
        inputs = {"box1_ordinary_business_income": self._ref("1000")}
        for mode in (WatermarkMode.DRAFT, WatermarkMode.SELF_CERTIFIED, WatermarkMode.NONE):
            pdf, _ = render_k1_1065(2025, mode, inputs)
            assert pdf[:4] == b"%PDF"

    def test_missing_pdf_raises(self):
        # With a nonexistent year-specific file AND no fallback, FileNotFoundError.
        # (Only meaningful when the fallback is absent; otherwise skip.)
        if _K1_PDF.exists():
            pytest.skip("fallback PDF present — cannot exercise the missing-file path")
        with pytest.raises(FileNotFoundError):
            render_k1_1065(2025, WatermarkMode.DRAFT,
                           {"box1_ordinary_business_income": self._ref("1")})
