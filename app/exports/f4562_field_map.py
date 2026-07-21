"""IRS Form 4562 (Depreciation and Amortization) — AcroForm field-name maps.

Two DISTINCT form layouts are supported, because the IRS materially redesigned
Form 4562 for the 2025 revision:

  ``"2022_2024"``  Form 4562 revisions 2022, 2023 and 2024. 2 pages, 210 text
                   fields. VERIFIED: the three revisions have byte-identical
                   sorted text-field name lists (checked with pypdf against
                   ``irs-prior/f4562--2022.pdf``, ``--2023.pdf``, ``--2024.pdf``),
                   so one map serves all three.

  ``"2025"``       Form 4562 revision 2025 ("Created 10/9/25"). 3 pages, 223 text
                   fields.

WHY THE SPLIT MATTERS — a silent-wrong-row trap
-----------------------------------------------
The 2025 revision INSERTED a new "50-year property" class into Part III
Section B at row **h**, which pushed the two real-property classes DOWN a row:

    class                          2022-2024 row      2025 row
    25-year property                   19g              19g
    50-year property                   (none)           19h   <-- NEW
    Residential rental (27.5 yr)       19h              19i   <-- MOVED
    Nonresidential real (39 yr)        19i              19j   <-- MOVED

Section C gained a matching 50-year ADS row at **20e**, and Part IV line 23 was
split into 23a (interest costs capitalized under §263A(f)) and 23b. Part VI
(amortization) moved from page 2 to page 3.

Rendering 2024's row letters onto the 2025 form would therefore file residential
rental depreciation in the **50-year** row — a wrong figure on a wrong line, with
nothing to catch it. So the engine keys Part III by RECOVERY PERIOD and this
module owns the per-revision period -> row resolution.

HOW THE MAP WAS BUILT (positional, not hand-transcribed)
--------------------------------------------------------
Every name below was derived by walking each PDF's widget annotations with
pypdf, grouping by the IRS's own structured subform path (``SectionBTable[0].
Line19b[0]``, ``Table_Ln26[0].BodyRow1[0]``, ``PartVITable[0].BodyRow1[0]``, …)
and ordering the columns within each group by their /Rect x-coordinate — i.e.
the printed left-to-right column order. Scalar (non-table) fields were pinned by
reading order against the printed line labels.

``None`` marks a column that is PREPRINTED on the form (e.g. "27.5 yrs." in the
recovery-period column of the residential-rental row). Those widgets exist but
are 1 point wide; writing to them would stamp text into the form's rule lines.
The renderer skips any column mapped to ``None``.

CONVENTIONS
-----------
- Dollar amounts are written as plain strings ("12500" — no ``$``, no commas).
- Section B/C column keys are the printed column letters:
  ``b`` month/year placed in service, ``c`` basis, ``d`` recovery period,
  ``e`` convention, ``f`` method, ``g`` depreciation deduction.
  (Column ``a``, the classification, is preprinted on every row.)
- Part V line 26 columns are ``a``..``i``; line 27 columns are ``a``..``h``
  (property used 50% or less takes no §179, so it has no column (i)).
- Part VI line 42 columns are ``a``..``f``.

RE-PINNING A FUTURE REVISION
----------------------------
When a later ``f4562_{year}.pdf`` is added, re-run the positional dump and add a
new revision entry. Do NOT reuse an existing revision's rows without diffing the
printed Section B/C class labels first — see the trap above.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class F4562Revision:
    """One Form 4562 layout revision."""

    key: str
    # Tax years this layout is the correct form for.
    tax_years: tuple[int, ...]
    # Header + scalar lines: logical key -> AcroForm field name.
    scalars: dict[str, str]
    # Part I line 6 rows (description, cost, elected cost), in printed order.
    line_6_rows: tuple[dict[str, str], ...]
    # Part III Section B: recovery period -> {column letter: field name | None}.
    section_b: dict[str, dict[str, str | None]]
    # Part III Section C: recovery period -> {column letter: field name | None}.
    section_c: dict[str, dict[str, str | None]]
    # Part V line 26 / 27 rows, in printed order.
    line_26_rows: tuple[dict[str, str], ...]
    line_27_rows: tuple[dict[str, str], ...]
    # Part V Section B vehicle mileage lines 30-33: line -> 6 vehicle columns.
    vehicle_lines: dict[str, tuple[str, ...]]
    # Part VI line 42 rows, in printed order.
    line_42_rows: tuple[dict[str, str], ...]
    # Extra rows in Section B that share a class (the form prints two blank rows
    # for residential rental and nonresidential real). period -> extra row dicts.
    section_b_extra: dict[str, tuple[dict[str, str | None], ...]] = field(
        default_factory=dict
    )


def _p1(name: str) -> str:
    return f"topmostSubform[0].Page1[0].{name}[0]"


def _p2(name: str) -> str:
    return f"topmostSubform[0].Page2[0].{name}[0]"


def _p3(name: str) -> str:
    return f"topmostSubform[0].Page3[0].{name}[0]"


def _sb(rev_page: str, row: str, name: str) -> str:
    return f"topmostSubform[0].{rev_page}[0].SectionBTable[0].{row}[0].{name}[0]"


def _sc(rev_page: str, row: str, name: str) -> str:
    return f"topmostSubform[0].{rev_page}[0].SectionCTable[0].{row}[0].{name}[0]"


def _tbl(page: str, table: str, row: str, name: str) -> str:
    return f"topmostSubform[0].{page}[0].{table}[0].{row}[0].{name}[0]"


_COLS_BCDEFG = ("b", "c", "d", "e", "f", "g")


def _row(page: str, row: str, names: list[str | None], kind: str = "B") -> dict[str, str | None]:
    """Build a Section B/C row dict from its six ordered column field names."""
    fn = _sb if kind == "B" else _sc
    return {
        col: (fn(page, row, n) if n is not None else None)
        for col, n in zip(_COLS_BCDEFG, names)
    }


# ---------------------------------------------------------------------------
# Revision "2022_2024" — Form 4562 (2022) / (2023) / (2024). 2 pages.
# ---------------------------------------------------------------------------

_R2224_SCALARS: dict[str, str] = {
    # Header
    "name": _p1("f1_1"),
    "business_activity": _p1("f1_2"),
    "identifying_number": _p1("f1_3"),
    # Part I — §179
    "1": _p1("f1_4"),    # Maximum amount
    "2": _p1("f1_5"),    # Total cost of §179 property placed in service
    "3": _p1("f1_6"),    # Threshold cost
    "4": _p1("f1_7"),    # Reduction in limitation
    "5": _p1("f1_8"),    # Dollar limitation for tax year
    "7": _p1("f1_15"),   # Listed property (from line 29)
    "8": _p1("f1_16"),   # Total elected cost
    "9": _p1("f1_17"),   # Tentative deduction
    "10": _p1("f1_18"),  # Carryover of disallowed deduction from prior year
    "11": _p1("f1_19"),  # Business income limitation
    "12": _p1("f1_20"),  # §179 expense deduction
    "13": _p1("f1_21"),  # Carryover of disallowed deduction to next year
    # Part II
    "14": _p1("f1_22"),  # Special depreciation allowance
    "15": _p1("f1_23"),  # Property subject to §168(f)(1) election
    "16": _p1("f1_24"),  # Other depreciation (including ACRS)
    # Part III Section A
    "17": _p1("f1_25"),  # MACRS deductions for assets placed in service before this year
    # Part IV Summary (page 1 on this revision)
    "21": _p1("f1_107"),  # Listed property (from line 28)
    "22": _p1("f1_108"),  # Total
    "23": _p1("f1_109"),  # §263A costs
    # Part V Section A
    "25": _p2("f2_1"),   # Special depreciation allowance for qualified listed property
    "28": _p2("f2_53"),  # Add column (h), lines 25-27
    "29": _p2("f2_54"),  # Add column (i), line 26
    # Part VI
    "43": _p2("f2_91"),  # Amortization of costs that began before this tax year
    "44": _p2("f2_92"),  # Total
}

_R2224 = F4562Revision(
    key="2022_2024",
    tax_years=(2022, 2023, 2024),
    scalars=_R2224_SCALARS,
    line_6_rows=(
        {
            "a": _tbl("Page1", "Table_Ln6", "BodyRow1", "f1_9"),
            "b": _tbl("Page1", "Table_Ln6", "BodyRow1", "f1_10"),
            "c": _tbl("Page1", "Table_Ln6", "BodyRow1", "f1_11"),
        },
        {
            "a": _tbl("Page1", "Table_Ln6", "BodyRow2", "f1_12"),
            "b": _tbl("Page1", "Table_Ln6", "BodyRow2", "f1_13"),
            "c": _tbl("Page1", "Table_Ln6", "BodyRow2", "f1_14"),
        },
    ),
    # Section B rows a-i: 3 / 5 / 7 / 10 / 15 / 20 / 25 / 27.5 / 39.
    section_b={
        "3":    _row("Page1", "Line19a", ["R4", "f1_26", "f1_27", "f1_28", "f1_29", "f1_30"]),
        "5":    _row("Page1", "Line19b", ["R5", "f1_31", "f1_32", "f1_33", "f1_34", "f1_35"]),
        "7":    _row("Page1", "Line19c", ["R6", "f1_36", "f1_37", "f1_38", "f1_39", "f1_40"]),
        "10":   _row("Page1", "Line19d", ["R7", "f1_41", "f1_42", "f1_43", "f1_44", "f1_45"]),
        "15":   _row("Page1", "Line19e", ["R8", "f1_46", "f1_47", "f1_48", "f1_49", "f1_50"]),
        "20":   _row("Page1", "Line19f", ["R9", "f1_51", "f1_52", "f1_53", "f1_54", "f1_55"]),
        # 25-year: recovery period ("25 yrs.") and method ("S/L") are preprinted.
        "25":   _row("Page1", "Line19g", ["R10", "f1_56", None, "f1_58", None, "f1_60"]),
        # Residential rental: recovery / convention / method all preprinted.
        "27.5": _row("Page1", "Line19h_1", ["f1_61", "f1_62", None, None, None, "f1_66"]),
        # Nonresidential real: same, first of two printed rows.
        "39":   _row("Page1", "Line19i_1", ["f1_73", "f1_74", None, None, None, "f1_78"]),
    },
    section_b_extra={
        "27.5": (
            _row("Page1", "Line19h_2", ["f1_67", "f1_68", None, None, None, "f1_72"]),
        ),
        # The second nonresidential row leaves column (d) fillable (for a legacy
        # 31.5-year nonresidential asset).
        "39": (
            _row("Page1", "Line19i_2", ["f1_79", "f1_80", "f1_81", None, None, "f1_84"]),
        ),
    },
    # Section C: 20a class life / 20b 12-yr / 20c 30-yr / 20d 40-yr.
    section_c={
        "class_life": _row("Page1", "Line20a", ["R11", "f1_85", "f1_86", "f1_87", None, "f1_89"], "C"),
        "12":         _row("Page1", "Line20b", ["R12", "f1_90", None, "f1_92", None, "f1_94"], "C"),
        "30":         _row("Page1", "Line20c", ["f1_95", "f1_96", None, None, None, "f1_100"], "C"),
        "40":         _row("Page1", "Line20d", ["f1_101", "f1_102", None, None, None, "f1_106"], "C"),
    },
    line_26_rows=tuple(
        {
            col: _tbl("Page2", "Table_Ln26", f"BodyRow{r}", f"f2_{base + i}")
            for i, col in enumerate("abcdefghi")
        }
        for r, base in ((1, 2), (2, 11), (3, 20))
    ),
    line_27_rows=tuple(
        {
            col: _tbl("Page2", "Table_Ln27", f"BodyRow{r}", f"f2_{base + i}")
            for i, col in enumerate("abcdefgh")
        }
        for r, base in ((1, 29), (2, 37), (3, 45))
    ),
    vehicle_lines={
        "30": tuple(_tbl("Page2", "Table_Ln30-33", "Line30", f"f2_{55 + i}") for i in range(6)),
        "31": tuple(_tbl("Page2", "Table_Ln30-33", "Line31", f"f2_{61 + i}") for i in range(6)),
        "32": tuple(_tbl("Page2", "Table_Ln30-33", "Line32", f"f2_{67 + i}") for i in range(6)),
        "33": tuple(_tbl("Page2", "Table_Ln30-33", "Line33", f"f2_{73 + i}") for i in range(6)),
    },
    line_42_rows=tuple(
        {
            col: _tbl("Page2", "PartVITable", f"BodyRow{r}", f"f2_{base + i}")
            for i, col in enumerate("abcdef")
        }
        for r, base in ((1, 79), (2, 85))
    ),
)


# ---------------------------------------------------------------------------
# Revision "2025" — Form 4562 (2025), "Created 10/9/25". 3 pages.
# ---------------------------------------------------------------------------

_R25_SCALARS: dict[str, str] = {
    # Header + Part I/II/III Section A are UNCHANGED from 2022-2024.
    "name": _p1("f1_1"),
    "business_activity": _p1("f1_2"),
    "identifying_number": _p1("f1_3"),
    "1": _p1("f1_4"),
    "2": _p1("f1_5"),
    "3": _p1("f1_6"),
    "4": _p1("f1_7"),
    "5": _p1("f1_8"),
    "7": _p1("f1_15"),
    "8": _p1("f1_16"),
    "9": _p1("f1_17"),
    "10": _p1("f1_18"),
    "11": _p1("f1_19"),
    "12": _p1("f1_20"),
    "13": _p1("f1_21"),
    "14": _p1("f1_22"),
    "15": _p1("f1_23"),
    "16": _p1("f1_24"),
    "17": _p1("f1_25"),
    # Part IV moved to PAGE 2 and line 23 split into 23a / 23b.
    "21": _p2("f2_1"),
    "22": _p2("f2_2"),
    "23a": _p2("f2_3"),   # basis attributable to §263A(f) interest costs
    "23b": _p2("f2_4"),   # other §263A costs
    # Part V Section A
    "25": _p2("f2_5"),
    "28": _p2("f2_57"),
    "29": _p2("f2_58"),
    # Part VI moved to PAGE 3.
    "43": _p3("f3_13"),
    "44": _p3("f3_14"),
}

_R25 = F4562Revision(
    key="2025",
    tax_years=(2025,),
    scalars=_R25_SCALARS,
    line_6_rows=(
        {
            "a": _tbl("Page1", "Table_Ln6", "BodyRow1", "f1_9"),
            "b": _tbl("Page1", "Table_Ln6", "BodyRow1", "f1_10"),
            "c": _tbl("Page1", "Table_Ln6", "BodyRow1", "f1_11"),
        },
        {
            "a": _tbl("Page1", "Table_Ln6", "BodyRow2", "f1_12"),
            "b": _tbl("Page1", "Table_Ln6", "BodyRow2", "f1_13"),
            "c": _tbl("Page1", "Table_Ln6", "BodyRow2", "f1_14"),
        },
    ),
    # Section B rows a-j: 3 / 5 / 7 / 10 / 15 / 20 / 25 / 50 / 27.5 / 39.
    # NOTE the 50-year class at row h — see the module docstring.
    section_b={
        "3":    _row("Page1", "Line19a", ["f1_26", "f1_27", "f1_28", "f1_29", "f1_30", "f1_31"]),
        "5":    _row("Page1", "Line19b", ["f1_32", "f1_33", "f1_34", "f1_35", "f1_36", "f1_37"]),
        "7":    _row("Page1", "Line19c", ["f1_38", "f1_39", "f1_40", "f1_41", "f1_42", "f1_43"]),
        "10":   _row("Page1", "Line19d", ["f1_44", "f1_45", "f1_46", "f1_47", "f1_48", "f1_49"]),
        "15":   _row("Page1", "Line19e", ["f1_50", "f1_51", "f1_52", "f1_53", "f1_54", "f1_55"]),
        "20":   _row("Page1", "Line19f", ["f1_56", "f1_57", "f1_58", "f1_59", "f1_60", "f1_61"]),
        "25":   _row("Page1", "Line19g", ["f1_62", "f1_63", None, "f1_65", None, "f1_67"]),
        # NEW in the 2025 revision: 50-year property (50 yrs. MM S/L).
        "50":   _row("Page1", "Line19h", ["f1_68", "f1_69", None, None, None, "f1_73"]),
        # Residential rental MOVED from 19h to 19i.
        "27.5": _row("Page1", "Line19i_1", ["f1_74", "f1_75", None, None, None, "f1_79"]),
        # Nonresidential real MOVED from 19i to 19j.
        "39":   _row("Page1", "Line19j_1", ["f1_86", "f1_87", None, None, None, "f1_91"]),
    },
    section_b_extra={
        "27.5": (
            _row("Page1", "Line19i_2", ["f1_80", "f1_81", None, None, None, "f1_85"]),
        ),
        "39": (
            _row("Page1", "Line19j_2", ["f1_92", "f1_93", "f1_94", None, None, "f1_97"]),
        ),
    },
    # Section C gained a 50-year row at 20e.
    section_c={
        "class_life": _row("Page1", "Line20a", ["f1_98", "f1_99", "f1_100", "f1_101", None, "f1_103"], "C"),
        "12":         _row("Page1", "Line20b", ["f1_104", "f1_105", None, "f1_107", None, "f1_109"], "C"),
        "30":         _row("Page1", "Line20c", ["f1_110", "f1_111", None, None, None, "f1_115"], "C"),
        "40":         _row("Page1", "Line20d", ["f1_116", "f1_117", None, None, None, "f1_121"], "C"),
        "50":         _row("Page1", "Line20e", ["f1_122", "f1_123", None, None, None, "f1_127"], "C"),
    },
    line_26_rows=tuple(
        {
            col: _tbl("Page2", "Table_Ln26", f"BodyRow{r}", f"f2_{base + i}")
            for i, col in enumerate("abcdefghi")
        }
        for r, base in ((1, 6), (2, 15), (3, 24))
    ),
    line_27_rows=tuple(
        {
            col: _tbl("Page2", "Table_Ln27", f"BodyRow{r}", f"f2_{base + i}")
            for i, col in enumerate("abcdefgh")
        }
        for r, base in ((1, 33), (2, 41), (3, 49))
    ),
    vehicle_lines={
        "30": tuple(_tbl("Page2", "Table_Ln30-33", "Line30", f"f2_{59 + i}") for i in range(6)),
        "31": tuple(_tbl("Page2", "Table_Ln30-33", "Line31", f"f2_{65 + i}") for i in range(6)),
        "32": tuple(_tbl("Page2", "Table_Ln30-33", "Line32", f"f2_{71 + i}") for i in range(6)),
        "33": tuple(_tbl("Page2", "Table_Ln30-33", "Line33", f"f2_{77 + i}") for i in range(6)),
    },
    line_42_rows=tuple(
        {
            col: _tbl("Page3", "PartVITable", f"BodyRow{r}", f"f3_{base + i}")
            for i, col in enumerate("abcdef")
        }
        for r, base in ((1, 1), (2, 7))
    ),
)


F4562_REVISIONS: dict[str, F4562Revision] = {
    _R2224.key: _R2224,
    _R25.key: _R25,
}

# Tax year -> revision key. A year with no entry has no verified field map and
# MUST raise rather than be rendered onto a guessed layout.
_YEAR_TO_REVISION: dict[int, str] = {
    year: rev.key for rev in F4562_REVISIONS.values() for year in rev.tax_years
}

SUPPORTED_FORM_YEARS: tuple[int, ...] = tuple(sorted(_YEAR_TO_REVISION))

# Form 4562 column (a) on the Part VI amortization table and the Part I line 6
# description column are narrow; longer text is clipped by the flattened
# overlay. Measured against the shipped PDFs.
DESCRIPTION_MAX_CHARS = 34


def revision_for_tax_year(tax_year: int | None) -> F4562Revision:
    """Resolve the Form 4562 layout for ``tax_year``.

    Raises ValueError for a year with no verified field map — we never render a
    tax figure onto a layout we have not pinned against the actual PDF, because
    the Section B class rows MOVED between revisions (a silent wrong-row error).
    """
    if tax_year is None:
        raise ValueError(
            "Form 4562 requires a tax year — the Part III Section B class rows "
            f"differ between revisions. Supported: {SUPPORTED_FORM_YEARS}."
        )
    key = _YEAR_TO_REVISION.get(tax_year)
    if key is None:
        raise ValueError(
            f"No verified Form 4562 field map for TY{tax_year} "
            f"(supported: {SUPPORTED_FORM_YEARS}). Download the revision, re-run "
            "the positional field dump, and add a revision entry — do NOT reuse "
            "another year's rows: the Section B class rows shifted in 2025."
        )
    return F4562_REVISIONS[key]


def section_b_row(revision: F4562Revision, period: str) -> dict[str, str | None] | None:
    """Section B row for a GDS recovery period (e.g. ``"5"``, ``"27.5"``)."""
    return revision.section_b.get(period)


def section_c_row(revision: F4562Revision, period: str) -> dict[str, str | None] | None:
    """Section C row for an ADS recovery period.

    ADS periods that are not one of the form's printed rows fall to the
    "class life" row (line 20a) per the Form 4562 instructions for line 20.
    """
    return revision.section_c.get(period) or revision.section_c.get("class_life")


__all__ = [
    "DESCRIPTION_MAX_CHARS",
    "F4562Revision",
    "F4562_REVISIONS",
    "SUPPORTED_FORM_YEARS",
    "revision_for_tax_year",
    "section_b_row",
    "section_c_row",
]
