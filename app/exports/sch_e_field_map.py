"""IRS Schedule E (Form 1040) — Supplemental Income and Loss — AcroForm field map.

Schedule E reports income/loss from rental real estate, royalties, partnerships,
S corporations, estates, trusts, and REMICs. Field names were verified by
dumping the official fillable PDF (``data/irs_forms/f1040se.pdf``) with ``pypdf``
(``PdfReader().get_fields()`` + a /Fields-tree walk to terminal fields) and
cross-referencing every terminal field's widget /Rect against the printed line
labels and column positions (a positional audit — the IRS XFA tooltips are empty
on this form). EVERY name below was re-validated to exist as a real terminal
field of the expected /FT (``/Tx`` text vs ``/Btn`` checkbox) on the distributed
PDF; see ``tests/smoke/test_sch_e.py::TestFieldMap::test_every_mapped_name_real``.

Schedule E is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like
the W-4P and Form 1040), so it MUST be filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe walker that bakes the values
in as a flattened, visible overlay), NOT the older page-based ``_fill_pdf_fields``.

Pattern mirrors ``w4p_field_map.py`` / ``f1040_field_map.py``: a
``dict[str, str]`` of logical key -> fully-qualified AcroForm field name, plus a
checkbox map and a yes/no-question map of ``(name, on_state)`` tuples.

Layout model
------------
- Part I (page 1) is a THREE-COLUMN table for up to three properties A / B / C.
  Per-line column fields are sibling text fields under a ``Table_*`` / ``Line*``
  subform. Logical keys use a trailing ``_a`` / ``_b`` / ``_c`` property suffix
  (e.g. ``rents_3_a``, ``mortgage_interest_12_b``). The single-column summary
  lines 23a-e, 24, 25 and 26 have no suffix.
- Part II (page 2) lists partnerships / S corporations: rows A and B are mapped
  (the common one-or-two-K-1 case) with name / type / EIN, the three flag
  checkboxes, and the (g)-(k) passive / nonpassive amount columns, plus the
  carry totals on lines 30, 31 and 32.
- Part III maps one estate/trust row (A) plus the line 35 / 36 / 37 totals;
  Part IV maps the line 39 REMIC total; Part V maps lines 40-43.

Conventions
-----------
- Dollar amounts are plain strings ("24000" — no ``$`` or commas). The IRS
  AcroForm boxes are plain text. Losses follow the form's parentheses
  convention; the filler writes the caller's raw string verbatim.
- The IRS ``f{page}_{NN}`` numbering increments in document/tab order, so field
  numbers do NOT map 1:1 to printed line numbers — every name was pinned
  positionally to its line + column.
- Conservative blank-fill: a line with no input value is simply not written.

Taxpayer-completed fields
-------------------------
The taxpayer's SSN (``f1_2`` on page 1, ``f2_2`` on page 2) is intentionally NOT
exposed for filling — it is left blank for the taxpayer. Schedule E itself has no
signature line; it attaches to the taxpayer's signed Form 1040.

CPA/EA verification note
------------------------
These names match the ``f1040se.pdf`` distributed on irs.gov at build time. The
Part I/II/III/IV face field names are stable across recent revisions. If a later
revision (or a prior-year ``f1040se_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# Property column suffixes for Part I (A / B / C) and Part II rows (A / B).
_COLS = ("a", "b", "c")


def _build_text_fields() -> dict[str, str]:
    """Assemble the logical-key -> fully-qualified /Tx field-name map.

    The repetitive multi-column blocks (Part I income/expenses for properties
    A/B/C, Part II rows A/B) are built with loops keyed off the exact base field
    numbers read from the PDF dump; every produced name is independently
    re-validated against the real PDF by the smoke test.
    """
    f: dict[str, str] = {}

    # --- Identity (page 1 + page 2 headers) ---
    # Name(s) shown on return. SSN is intentionally NOT mapped (taxpayer enters).
    f["name_shown"] = f"{_P1}.f1_1[0]"        # page 1 header name
    f["name_shown_p2"] = f"{_P2}.f2_1[0]"     # page 2 header name

    # --- Part I property identity (line 1a address, 1b type, line 2 days) ---
    for i, c in enumerate(_COLS):
        U = c.upper()
        f[f"property_{c}_address"] = f"{_P1}.Table_Line1a[0].Row{U}[0].f1_{3 + i}[0]"
        f[f"property_{c}_type"] = f"{_P1}.Table_Line1b[0].Row{U}[0].f1_{6 + i}[0]"
        # Line 2: fair rental days, personal use days (QJV checkbox is separate).
        f[f"property_{c}_fair_rental_days"] = f"{_P1}.Table_Line2[0].Row{U}[0].f1_{9 + 2 * i}[0]"
        f[f"property_{c}_personal_use_days"] = f"{_P1}.Table_Line2[0].Row{U}[0].f1_{10 + 2 * i}[0]"

    # --- Part I income (lines 3-4) — Table_Income; cols A/B/C consecutive ---
    for base, ln, b in (("rents_3", 3, 16), ("royalties_4", 4, 19)):
        for col, c in enumerate(_COLS):
            f[f"{base}_{c}"] = f"{_P1}.Table_Income[0].Line{ln}[0].f1_{b + col}[0]"

    # --- Part I expenses (lines 5-22) — Table_Expenses; cols A/B/C consecutive.
    # Each tuple: (logical base, printed line no, col-A field number). Line 19's
    # amount columns start at f1_65 (f1_64 is its "Other - describe" text label).
    expense_lines = (
        ("advertising_5", 5, 22),
        ("auto_travel_6", 6, 25),
        ("cleaning_maintenance_7", 7, 28),
        ("commissions_8", 8, 31),
        ("insurance_9", 9, 34),
        ("legal_professional_10", 10, 37),
        ("management_fees_11", 11, 40),
        ("mortgage_interest_12", 12, 43),
        ("other_interest_13", 13, 46),
        ("repairs_14", 14, 49),
        ("supplies_15", 15, 52),
        ("taxes_16", 16, 55),
        ("utilities_17", 17, 58),
        ("depreciation_18", 18, 61),
        ("other_expense_19", 19, 65),
        ("total_expenses_20", 20, 68),
        ("income_loss_21", 21, 71),       # line 21 (subtract 20 from 3/4)
        ("deductible_loss_22", 22, 74),   # line 22 (deductible RRE loss)
    )
    for base, ln, b in expense_lines:
        for col, c in enumerate(_COLS):
            f[f"{base}_{c}"] = f"{_P1}.Table_Expenses[0].Line{ln}[0].f1_{b + col}[0]"
    # Line 19 "Other" expense description (single text label, not a column).
    f["other_expense_19_desc"] = f"{_P1}.Table_Expenses[0].Line19[0].f1_64[0]"

    # --- Part I single-column summary totals (lines 23a-e, 24, 25, 26) ---
    f["line_23a_total_rents"] = f"{_P1}.f1_77[0]"
    f["line_23b_total_royalties"] = f"{_P1}.f1_78[0]"
    f["line_23c_total_mortgage_interest"] = f"{_P1}.f1_79[0]"
    f["line_23d_total_depreciation"] = f"{_P1}.f1_80[0]"
    f["line_23e_total_expenses"] = f"{_P1}.f1_81[0]"
    f["line_24_total_income"] = f"{_P1}.f1_82[0]"
    f["line_25_total_losses"] = f"{_P1}.f1_83[0]"
    f["line_26_total_rre_royalty"] = f"{_P1}.f1_84[0]"

    # --- Part II partnerships / S corporations (line 28 rows A & B) ---
    for i, c in enumerate(("a", "b")):
        U = c.upper()
        f[f"partnership_{c}_name"] = f"{_P2}.Table_Line28a-f[0].Row{U}[0].f2_{3 + 3 * i}[0]"
        f[f"partnership_{c}_type"] = f"{_P2}.Table_Line28a-f[0].Row{U}[0].f2_{4 + 3 * i}[0]"  # "P" or "S"
        f[f"partnership_{c}_ein"] = f"{_P2}.Table_Line28a-f[0].Row{U}[0].f2_{5 + 3 * i}[0]"
        base = 15 + 5 * i  # (g)-(k) amount columns for this row
        f[f"partnership_{c}_passive_loss_g"] = f"{_P2}.Table_Line28g-k[0].Row{U}[0].f2_{base + 0}[0]"
        f[f"partnership_{c}_passive_income_h"] = f"{_P2}.Table_Line28g-k[0].Row{U}[0].f2_{base + 1}[0]"
        f[f"partnership_{c}_nonpassive_loss_i"] = f"{_P2}.Table_Line28g-k[0].Row{U}[0].f2_{base + 2}[0]"
        f[f"partnership_{c}_sec179_j"] = f"{_P2}.Table_Line28g-k[0].Row{U}[0].f2_{base + 3}[0]"
        f[f"partnership_{c}_nonpassive_income_k"] = f"{_P2}.Table_Line28g-k[0].Row{U}[0].f2_{base + 4}[0]"
    # Part II carry totals.
    f["line_30_partnership_income"] = f"{_P2}.f2_45[0]"   # add cols (h) and (k) of 29a
    f["line_31_partnership_losses"] = f"{_P2}.f2_46[0]"   # add cols (g),(i),(j) of 29b
    f["line_32_partnership_total"] = f"{_P2}.f2_47[0]"    # combine 30 and 31

    # --- Part III estates and trusts (line 33 row A) + totals 35-37 ---
    f["estate_a_name"] = f"{_P2}.Table_Line33a-b[0].RowA[0].f2_48[0]"
    f["estate_a_ein"] = f"{_P2}.Table_Line33a-b[0].RowA[0].f2_49[0]"
    f["estate_a_passive_deduction_c"] = f"{_P2}.Table_Line33c-f[0].RowA[0].f2_52[0]"
    f["estate_a_passive_income_d"] = f"{_P2}.Table_Line33c-f[0].RowA[0].f2_53[0]"
    f["estate_a_deduction_loss_e"] = f"{_P2}.Table_Line33c-f[0].RowA[0].f2_54[0]"
    f["estate_a_other_income_f"] = f"{_P2}.Table_Line33c-f[0].RowA[0].f2_55[0]"
    f["line_35_estate_income"] = f"{_P2}.f2_68[0]"        # add cols (d) and (f) of 34a
    f["line_36_estate_losses"] = f"{_P2}.f2_69[0]"        # add cols (c) and (e) of 34b
    f["line_37_estate_total"] = f"{_P2}.f2_70[0]"         # combine 35 and 36

    # --- Part IV REMICs (line 39 total) ---
    f["line_39_remic_total"] = f"{_P2}.f2_76[0]"          # combine cols (d) and (e)

    # --- Part V summary (lines 40-43) ---
    f["line_40_farm_rental"] = f"{_P2}.f2_77[0]"          # net farm rental (Form 4835)
    f["line_41_total_income_loss"] = f"{_P2}.f2_78[0]"    # combine 26, 32, 37, 39, 40
    f["line_42_farming_fishing"] = f"{_P2}.Line42_ReadOrder[0].f2_79[0]"
    f["line_43_real_estate_professional"] = f"{_P2}.f2_80[0]"

    return f


# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
SCHEDULE_E_FIELDS: dict[str, str] = _build_text_fields()

# Single boolean checkboxes: logical key -> (AcroForm field name, on-state).
# Set when the caller supplies a truthy value for the key. QJV = qualified joint
# venture (line 2); the Part II flags are the line-28 (c) foreign, (e) basis-
# computation-required and (f) not-at-risk boxes.
SCHEDULE_E_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    "property_a_qjv": (f"{_P1}.Table_Line2[0].RowA[0].c1_3[0]", "1"),
    "property_b_qjv": (f"{_P1}.Table_Line2[0].RowB[0].c1_4[0]", "1"),
    "property_c_qjv": (f"{_P1}.Table_Line2[0].RowC[0].c1_5[0]", "1"),
    "partnership_a_foreign": (f"{_P2}.Table_Line28a-f[0].RowA[0].c2_2[0]", "1"),
    "partnership_a_basis_required": (f"{_P2}.Table_Line28a-f[0].RowA[0].c2_3[0]", "1"),
    "partnership_a_not_at_risk": (f"{_P2}.Table_Line28a-f[0].RowA[0].c2_4[0]", "1"),
    "partnership_b_foreign": (f"{_P2}.Table_Line28a-f[0].RowB[0].c2_5[0]", "1"),
    "partnership_b_basis_required": (f"{_P2}.Table_Line28a-f[0].RowB[0].c2_6[0]", "1"),
    "partnership_b_not_at_risk": (f"{_P2}.Table_Line28a-f[0].RowB[0].c2_7[0]", "1"),
}

# Yes/No questions: logical key -> {"yes": (name, on), "no": (name, on)}.
# Line A: did you make payments requiring Form 1099? Line B: did/will you file
# them? Line 27: are you reporting a prior-year-unallowed / at-risk / basis loss?
SCHEDULE_E_YESNO_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "line_a_1099_required": {
        "yes": (f"{_P1}.c1_1[0]", "1"),
        "no": (f"{_P1}.c1_1[1]", "2"),
    },
    "line_b_1099_filed": {
        "yes": (f"{_P1}.c1_2[0]", "1"),
        "no": (f"{_P1}.c1_2[1]", "2"),
    },
    "line_27_prior_year_loss": {
        "yes": (f"{_P2}.c2_1[0]", "1"),
        "no": (f"{_P2}.c2_1[1]", "2"),
    },
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Schedule E fields.
VALID_SCHEDULE_E_FIELDS: frozenset[str] = frozenset(
    list(SCHEDULE_E_FIELDS.values())
    + [v[0] for v in SCHEDULE_E_CHECKBOX_FIELDS.values()]
    + [opt[0] for q in SCHEDULE_E_YESNO_FIELDS.values() for opt in q.values()]
)
