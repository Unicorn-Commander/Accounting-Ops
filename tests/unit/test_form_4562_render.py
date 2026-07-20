"""Form 4562 field-map + renderer tests.

The field-map tests are the ones that matter most: an AcroForm name that does
not exist on the shipped PDF fills NOTHING and fails SILENTLY, and a name that
exists but belongs to a DIFFERENT printed line puts a real figure on the wrong
line. Both are caught here by resolving every mapped name against the actual
IRS PDFs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.exports.f4562_field_map import (
    F4562_REVISIONS,
    SUPPORTED_FORM_YEARS,
    revision_for_tax_year,
)
from app.exports.form_prefill import IRS_FORMS_DIR, WatermarkMode

pypdf = pytest.importorskip("pypdf")


REVISION_PDFS = {
    "2022_2024": ("f4562_2022.pdf", "f4562_2023.pdf", "f4562_2024.pdf"),
    "2025": ("f4562_2025.pdf",),
}


def _all_mapped_names(revision) -> set[str]:
    names: set[str] = set()

    def add(value) -> None:
        if isinstance(value, str):
            names.add(value)

    for value in revision.scalars.values():
        add(value)
    for row in revision.line_6_rows:
        for value in row.values():
            add(value)
    for table in (revision.section_b, revision.section_c):
        for row in table.values():
            for value in row.values():
                add(value)
    for rows in revision.section_b_extra.values():
        for row in rows:
            for value in row.values():
                add(value)
    for row in revision.line_26_rows + revision.line_27_rows + revision.line_42_rows:
        for value in row.values():
            add(value)
    for cols in revision.vehicle_lines.values():
        for value in cols:
            add(value)
    return names


def _pdf_paths(revision_key: str) -> list[Path]:
    return [IRS_FORMS_DIR / name for name in REVISION_PDFS[revision_key]]


def _require(path: Path) -> None:
    if not path.exists():
        pytest.skip(
            f"{path.name} not present — run scripts/download_irs_forms.sh 4562"
        )


@pytest.mark.parametrize(
    ("revision_key", "pdf_name"),
    [(k, n) for k, names in REVISION_PDFS.items() for n in names],
)
def test_every_mapped_field_exists_on_the_real_pdf(revision_key: str, pdf_name: str) -> None:
    """Every AcroForm name in the map must exist on the shipped PDF.

    A name that does not exist fills nothing and fails SILENTLY — this is the
    single most valuable check in the file.
    """
    path = IRS_FORMS_DIR / pdf_name
    _require(path)
    actual = set((pypdf.PdfReader(str(path)).get_fields() or {}).keys())
    mapped = _all_mapped_names(F4562_REVISIONS[revision_key])
    missing = sorted(mapped - actual)
    assert not missing, f"{pdf_name}: {len(missing)} mapped field(s) absent: {missing[:10]}"


def test_2022_2024_revisions_share_one_layout() -> None:
    """The 2022/2023/2024 PDFs must have identical text-field name sets.

    This is what makes ONE map valid for all three. If a future re-download
    changes any of them, this fails rather than silently mis-filling.
    """
    paths = _pdf_paths("2022_2024")
    for path in paths:
        _require(path)
    name_sets = []
    for path in paths:
        fields = pypdf.PdfReader(str(path)).get_fields() or {}
        name_sets.append({n for n, spec in fields.items() if str(spec.get("/FT")) == "/Tx"})
    assert name_sets[0] == name_sets[1] == name_sets[2]
    assert len(name_sets[0]) == 210


def test_2025_revision_is_a_different_layout() -> None:
    """Guards the assumption that 2025 must NOT reuse the 2022-2024 map."""
    old = IRS_FORMS_DIR / "f4562_2024.pdf"
    new = IRS_FORMS_DIR / "f4562_2025.pdf"
    _require(old)
    _require(new)
    old_fields = pypdf.PdfReader(str(old)).get_fields() or {}
    new_fields = pypdf.PdfReader(str(new)).get_fields() or {}
    assert set(old_fields) != set(new_fields)
    assert len(pypdf.PdfReader(str(new)).pages) == 3


def test_section_b_class_rows_moved_in_2025() -> None:
    """REGRESSION: the 2025 revision inserted a 50-year class at row 19h,
    pushing residential rental to 19i and nonresidential real to 19j.

    Reusing the 2024 rows would file residential rental depreciation in the
    50-year row — a real figure on the wrong line, with nothing to catch it.
    """
    r2024 = revision_for_tax_year(2024)
    r2025 = revision_for_tax_year(2025)

    assert "Line19h_1" in r2024.section_b["27.5"]["b"]
    assert "Line19i_1" in r2024.section_b["39"]["b"]
    assert "50" not in r2024.section_b

    assert "Line19h" in r2025.section_b["50"]["b"]
    assert "Line19i_1" in r2025.section_b["27.5"]["b"]
    assert "Line19j_1" in r2025.section_b["39"]["b"]

    # The two revisions must NOT resolve 27.5 to the same widget.
    assert r2024.section_b["27.5"]["b"] != r2025.section_b["27.5"]["b"]


def test_2025_section_c_gained_a_50_year_row() -> None:
    assert "50" not in revision_for_tax_year(2024).section_c
    assert "50" in revision_for_tax_year(2025).section_c


def test_2025_split_line_23_into_23a_and_23b() -> None:
    r2024 = revision_for_tax_year(2024)
    r2025 = revision_for_tax_year(2025)
    assert "23" in r2024.scalars and "23a" not in r2024.scalars
    assert "23a" in r2025.scalars and "23b" in r2025.scalars


def test_preprinted_columns_are_mapped_to_none() -> None:
    """Preprinted cells (e.g. "27.5 yrs.") must never be written into."""
    for year in (2024, 2025):
        row = revision_for_tax_year(year).section_b["27.5"]
        assert row["d"] is None, "recovery period is preprinted on the 27.5 row"
        assert row["e"] is None, "convention is preprinted on the 27.5 row"
        assert row["f"] is None, "method is preprinted on the 27.5 row"
        assert row["c"] is not None and row["g"] is not None


def test_unsupported_year_raises_rather_than_guessing_a_layout() -> None:
    with pytest.raises(ValueError, match="No verified Form 4562 field map"):
        revision_for_tax_year(2019)
    with pytest.raises(ValueError, match="requires a tax year"):
        revision_for_tax_year(None)


def test_supported_form_years_are_the_ones_we_pinned() -> None:
    assert SUPPORTED_FORM_YEARS == (2022, 2023, 2024, 2025)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def test_renderer_is_registered_in_the_form_registry() -> None:
    from app.exports.form_registry import get_spec, render_form  # noqa: F401

    spec = get_spec("4562")
    assert spec is not None
    assert spec.mode == "computed"
    assert spec.module == "app.exports.f4562_fill"
    assert spec.func == "render_4562"


def test_renderer_fills_and_produces_a_pdf() -> None:
    from app.exports.f4562_fill import render_4562

    _require(IRS_FORMS_DIR / "f4562_2024.pdf")
    inputs = {
        "name": {"value": "Acme Manufacturing Inc", "source_ref": "intake#1", "origin": "user_entered"},
        "business_activity": {"value": "Machining", "source_ref": "intake#1", "origin": "user_entered"},
        "1": {"value": "1220000", "source_ref": "computed:form4562@2024", "origin": "computed"},
        "12": {"value": "50000", "source_ref": "computed:form4562@2024", "origin": "computed"},
        "22": {"value": "74400", "source_ref": "computed:form4562@2024", "origin": "computed"},
        "sectionb_5_c": {"value": "32000", "source_ref": "computed:form4562@2024", "origin": "computed"},
        "sectionb_5_g": {"value": "6400", "source_ref": "computed:form4562@2024", "origin": "computed"},
    }
    pdf_bytes, content_type = render_4562(2024, WatermarkMode.DRAFT, inputs)
    assert content_type == "application/pdf"
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 10_000


def test_renderer_requires_a_mapped_tax_year() -> None:
    from app.exports.f4562_fill import render_4562

    with pytest.raises(ValueError):
        render_4562(2019, WatermarkMode.DRAFT, {})
    with pytest.raises(ValueError):
        render_4562(None, WatermarkMode.DRAFT, {})


def test_renderer_leaves_identifying_number_blank_for_the_human() -> None:
    from app.exports.f4562_fill import _resolve_field, render_4562

    _require(IRS_FORMS_DIR / "f4562_2024.pdf")
    revision = revision_for_tax_year(2024)
    # The field IS mapped (so the key is recognized) …
    assert _resolve_field(revision, "identifying_number") is not None
    # … but the renderer refuses to fill it. Render with only that key and
    # confirm nothing was written.
    inputs = {"identifying_number": {"value": "12-3456789", "source_ref": "x", "origin": "user_entered"}}
    pdf_bytes, _ = render_4562(2024, WatermarkMode.DRAFT, inputs)
    assert b"12-3456789" not in pdf_bytes


def test_renderer_skips_unknown_and_preprinted_keys() -> None:
    from app.exports.f4562_fill import _resolve_field

    revision = revision_for_tax_year(2024)
    assert _resolve_field(revision, "not_a_real_key") is None
    assert _resolve_field(revision, "sectionb_27.5_d") is None      # preprinted
    assert _resolve_field(revision, "sectionb_999_c") is None       # no such class
    assert _resolve_field(revision, "listed26_9_a") is None         # row out of range
    assert _resolve_field(revision, "sectionb_5_c") is not None


def _rendered_positions(pdf_bytes: bytes, targets: set[str]) -> dict[str, tuple[int, int, int]]:
    """Where each target string actually landed in the rendered PDF."""
    import io

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    found: dict[str, tuple[int, int, int]] = {}
    for page_no, page in enumerate(reader.pages):
        def visit(text, cm, tm, font_dict, font_size, _page=page_no):
            stripped = text.strip()
            if stripped in targets:
                found.setdefault(stripped, (_page, round(tm[4]), round(tm[5])))

        page.extract_text(visitor_text=visit)
    return found


def test_residential_rental_renders_one_row_lower_on_the_2025_form() -> None:
    """END-TO-END proof of the row shift, asserted on the RENDERED ARTIFACT.

    This is the check that would actually catch the silent-wrong-row bug: it
    does not trust the map, it reads back where the ink landed. Residential
    rental (27.5-year) sits on row 19h in 2022-2024 and on row 19i in 2025 —
    one 12-point row LOWER — because the 2025 revision inserted a 50-year class
    above it.
    """
    from app.exports.f4562_fill import render_4562

    for year in (2024, 2025):
        _require(IRS_FORMS_DIR / f"f4562_{year}.pdf")

    sentinel = "7654321"
    inputs = {
        "sectionb_27.5_g": {
            "value": sentinel, "source_ref": "computed:form4562", "origin": "computed",
        }
    }
    placed: dict[int, tuple[int, int, int]] = {}
    for year in (2024, 2025):
        pdf_bytes, _ = render_4562(year, WatermarkMode.NONE, inputs)
        found = _rendered_positions(pdf_bytes, {sentinel})
        assert sentinel in found, f"TY{year}: value did not render at all"
        placed[year] = found[sentinel]

    page_2024, _, y_2024 = placed[2024]
    page_2025, _, y_2025 = placed[2025]
    assert page_2024 == page_2025 == 0
    # One printed row lower on the 2025 form (rows are 12 points tall).
    assert y_2024 - y_2025 == 12, (
        f"expected the 27.5 row to move down exactly one row: "
        f"2024 y={y_2024}, 2025 y={y_2025}"
    )


def test_section_c_unknown_period_falls_back_to_class_life_row() -> None:
    """Form 4562 line 20 instructions: an ADS period with no printed row goes on
    line 20a ("class life")."""
    from app.exports.f4562_fill import _resolve_field

    revision = revision_for_tax_year(2024)
    got = _resolve_field(revision, "sectionc_7_g")
    assert got == revision.section_c["class_life"]["g"]
