"""Form 4562 (Depreciation and Amortization) renderer.

Registered in ``app.exports.form_registry`` as kind ``4562``, mode ``computed``:
the figures come from the deterministic depreciation engine
(``app.tax.engines.depreciation`` via ``app.tax.compute_4562``), not from an LLM
and not typed by hand. This renderer's only job is to put already-computed,
already-provenance-checked values on the right boxes of the right form revision.

Conservative blank-fill
-----------------------
Any line whose input is absent (or blank) is left blank. We never guess, never
insert a zero, and never derive one line from another here — derivation happens
in the engine, where it is tested.

Left blank for the human
------------------------
The taxpayer's **identifying number** (EIN/SSN) is intentionally LEFT BLANK even
if supplied, consistent with the 1040 / 1120 / 1120-S / 1065 renderers. The
line-18 general-asset-account election and the Part V Section A/C yes-no
checkboxes are likewise not filled — they are elections and factual
representations for the filer, not computed figures.

Revision safety
---------------
The Part III Section B class rows MOVED in the 2025 revision (a new 50-year row
at 19h pushed residential rental to 19i and nonresidential real to 19j). This
renderer therefore resolves every Section B/C row through
``f4562_field_map.revision_for_tax_year(tax_year)`` and RAISES for a tax year
with no verified map, rather than rendering onto a guessed layout. See
``f4562_field_map`` for the full revision table.

XFA-hybrid form
---------------
Form 4562 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so it
is filled through ``form_prefill._render_acroform_pdf`` — the XFA-safe field-tree
walker that BAKES each value in as a flattened, visible overlay so it renders in
every viewer and print path (a pypdf-filled AcroForm otherwise prints blank
through the macOS/CUPS raster path).

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f4562_fill import render_4562
    from app.tax.compute_4562 import compute_4562

    payload = compute_4562(2024, assets=[...], business_taxable_income=250000)
    pdf_bytes, content_type = render_4562(2024, WatermarkMode.DRAFT, payload["inputs"])
"""

from __future__ import annotations

from pathlib import Path

from app.exports.f4562_field_map import (
    F4562Revision,
    revision_for_tax_year,
)
from app.exports.form_prefill import (
    IRS_FORMS_DIR,
    WatermarkMode,
    _render_acroform_pdf,
)

# Identity item left blank for the human even if supplied (mirrors 1120 / 1065).
_LEAVE_BLANK_FOR_HUMAN = frozenset({"identifying_number", "ein", "ssn"})

_SECTION_B_PREFIX = "sectionb_"
_SECTION_C_PREFIX = "sectionc_"
_LINE_6_PREFIX = "6_"
_LISTED_26_PREFIX = "listed26_"
_LISTED_27_PREFIX = "listed27_"
_VEHICLE_PREFIX = "vehicle_"
_AMORT_PREFIX = "amort42_"


def _f4562_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 4562 source PDF, preferring the year-specific file."""
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f4562_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f4562.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def _indexed_row(rows: tuple[dict[str, str], ...], key: str, prefix: str) -> str | None:
    """Resolve a ``<prefix><row>_<col>`` key against an ordered row tuple."""
    rest = key[len(prefix):]
    row_txt, _, col = rest.partition("_")
    if not col:
        return None
    try:
        row_idx = int(row_txt) - 1
    except ValueError:
        return None
    if not (0 <= row_idx < len(rows)):
        return None
    return rows[row_idx].get(col)


def _section_row(
    revision: F4562Revision, key: str, prefix: str, which: str
) -> str | None:
    """Resolve a ``sectionb_<period>_<col>`` / ``sectionc_<period>_<col>`` key.

    The period may itself contain an underscore (``class_life``), so the COLUMN
    is split off the right-hand side.
    """
    body, _, col = key[len(prefix):].rpartition("_")
    if not body or not col:
        return None
    table = revision.section_b if which == "b" else revision.section_c
    row = table.get(body)
    if row is None and which == "c":
        # An ADS period with no printed row falls to line 20a "class life",
        # per the Form 4562 instructions for line 20.
        row = table.get("class_life")
    if row is None:
        return None
    # A ``None`` column is PREPRINTED on the form — never write into it.
    return row.get(col)


def _resolve_field(revision: F4562Revision, key: str) -> str | None:
    """Logical input key -> AcroForm field name for this revision, or None."""
    if key in revision.scalars:
        return revision.scalars[key]
    if key.startswith(_SECTION_B_PREFIX):
        return _section_row(revision, key, _SECTION_B_PREFIX, "b")
    if key.startswith(_SECTION_C_PREFIX):
        return _section_row(revision, key, _SECTION_C_PREFIX, "c")
    if key.startswith(_LISTED_26_PREFIX):
        return _indexed_row(revision.line_26_rows, key, _LISTED_26_PREFIX)
    if key.startswith(_LISTED_27_PREFIX):
        return _indexed_row(revision.line_27_rows, key, _LISTED_27_PREFIX)
    if key.startswith(_AMORT_PREFIX):
        return _indexed_row(revision.line_42_rows, key, _AMORT_PREFIX)
    if key.startswith(_LINE_6_PREFIX):
        return _indexed_row(revision.line_6_rows, key, _LINE_6_PREFIX)
    if key.startswith(_VEHICLE_PREFIX):
        line_txt, _, col_txt = key[len(_VEHICLE_PREFIX):].partition("_")
        cols = revision.vehicle_lines.get(line_txt)
        if not cols:
            return None
        try:
            idx = int(col_txt) - 1
        except ValueError:
            return None
        return cols[idx] if 0 <= idx < len(cols) else None
    return None


def render_4562(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 4562 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line/box is left blank (conservative blank-fill).

    Parameters
    ----------
    tax_year:
        REQUIRED — the Section B class rows differ between form revisions, so a
        missing or unmapped year raises rather than rendering onto a guessed
        layout.
    watermark_mode:
        DRAFT / SELF_CERTIFIED / NONE, driven by the work product's status.
    inputs:
        ``{logical_key: {"value": str, "source_ref": str, "origin": str}}``.
        Logical keys (produced by ``app.tax.compute_4562.build_4562_inputs``):

          - header: ``"name"``, ``"business_activity"``
            (``"identifying_number"`` is accepted but deliberately NOT filled);
          - Part I/II/III/IV/V/VI scalar lines by bare line number: ``"1"``…
            ``"17"``, ``"21"``, ``"22"``, ``"23"`` (2022-2024) or ``"23a"`` /
            ``"23b"`` (2025), ``"25"``, ``"28"``, ``"29"``, ``"43"``, ``"44"``;
          - Part I line 6 rows: ``"6_<row>_<a|b|c>"`` (rows 1-2);
          - Part III Section B: ``"sectionb_<period>_<b..g>"`` where ``period``
            is the GDS recovery period (``"3"``, ``"5"``, ``"27.5"``, ``"39"``…);
          - Part III Section C: ``"sectionc_<period>_<b..g>"`` or
            ``"sectionc_class_life_<b..g>"``;
          - Part V: ``"listed26_<row>_<a..i>"`` / ``"listed27_<row>_<a..h>"``
            (rows 1-3);
          - Part V Section B vehicles: ``"vehicle_<30|31|32|33>_<1..6>"``;
          - Part VI: ``"amort42_<row>_<a..f>"`` (rows 1-2).

        Provenance (``source_ref``) is validated upstream; this renderer trusts
        the caller. A blank value, an unknown key, and a key that maps to a
        PREPRINTED column are all skipped (conservative — we never write into a
        box we do not recognize, and never over a preprinted form value).

    Raises
    ------
    ValueError
        If ``tax_year`` is None or has no verified field map.
    FileNotFoundError
        If no Form 4562 source PDF is present in ``data/irs_forms/``.
    """
    revision = revision_for_tax_year(tax_year)

    source_path = _f4562_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 4562 PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}
    for key, payload in inputs.items():
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value).strip() == "":
            continue
        if key in _LEAVE_BLANK_FOR_HUMAN:
            continue
        acro_name = _resolve_field(revision, key)
        if acro_name:
            field_values[acro_name] = str(value)

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"


__all__ = ["render_4562", "_f4562_form_path", "WatermarkMode"]
