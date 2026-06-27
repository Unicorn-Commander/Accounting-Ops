"""IRS Form 1120 (TY 2024) — AcroForm PDF field-name dictionary.

Field names were re-pinned (2026-06) directly against the official
``data/irs_forms/f1120_2024.pdf`` ("Form 1120 (2025)" calendar-year revision) by
walking every terminal widget with ``pypdf`` and matching each field's /Rect to
its printed line label (a positional audit; ``pdftk … dump_data_fields_utf8``
yields the same names). An earlier hand-built draft of this map pointed at
``Page1[0].f1_01[0]``-style names that do not exist on the shipped PDF — the
header, line 1a, and the whole of Schedule L therefore filled NOTHING. Every
value below is now a real field on f1120_2024.pdf, so the renderer's
conservative ``_set_acroform_values`` walker actually lands them.

The IRS uses a structured naming convention with read-order sub-groups:
    topmostSubform[0].Page{N}[0].{SubGroup}[0].{descriptor}[{index}]
e.g. the corporation name is
``topmostSubform[0].Page1[0].NameFieldsReadOrder[0].f1_4[0]`` and the item-E
status checkboxes are ``…Page1[0].c1_6[0]``…``c1_9[0]``. Schedule L lives on
``Page6[0]`` (the 6th PDF page), NOT ``Page5[0]``, and uses structured row names
``…Page6[0].Table_SchL_Assets[0].Line{N}[0].f6_{k}[0]``.

When a later-year ``f1120_{year}.pdf`` is dropped into ``data/irs_forms/``,
re-run the positional dump and re-pin any changed names before use.

Usage
-----
    from app.exports.form_1120_field_map import (
        FORM_1120_FIELDS,
        SCHEDULE_L_FIELDS,
        FORM_1120_HEADER_FIELDS,
        FORM_1125E_FIELDS,
    )

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas).
  The IRS AcroForm fields are plain-text boxes, not numeric.
- Checkbox fields are "1" (checked) or "Off" / "" (unchecked).
- Negative amounts on the official form use parentheses per accounting
  convention; the filler writes the raw string from the caller (e.g. "(500)").
- Lines left blank (not in our data) are simply not written to the PDF.

Logical keys with NO field on Form 1120 page 1
----------------------------------------------
``FORM_1120_HEADER_FIELDS`` keeps three logical keys that Form 1120 (unlike the
S-corp 1120-S) does NOT have a page-1 box for: ``business_code`` and
``state_inc`` (1120 has no NAICS / state-of-incorporation box) and
``amended_return`` (an 1120 amends via Form 1120-X, so there is no page-1 amended
checkbox). To keep the logical-key set stable for the renderer and
``form_field_specs`` while never writing to the WRONG box, those three map to a
deliberate sentinel field name (``__NOT_ON_FORM_1120__``) that matches no widget
on the PDF — the walker simply skips it, so the value stays blank (conservative).
``num_shareholders`` IS kept and likewise sentinel-mapped (1120 reports stock on
Schedule K / Schedule G, not a page-1 count box).

Confidence note
---------------
These field names match the f1120_2024.pdf as distributed on irs.gov, verified
positionally. The internal IRS numbering (f1_NN / f6_NN) increments in document
order, so line numbers and field numbers do not map 1:1 — each name was pinned to
its printed line. CPA should still verify the filled PDF against the blank form
on first use.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Form 1120 page 1 — header / identity fields
# ---------------------------------------------------------------------------

# Sentinel for logical keys that have NO corresponding field on Form 1120 page 1
# (see the module docstring). It matches no widget on the PDF, so the renderer's
# field-name walker silently skips it and the value is left blank (conservative —
# never write to the wrong box just to satisfy a key).
_NOT_ON_FORM = "topmostSubform[0].Page1[0].__NOT_ON_FORM_1120__[0]"

FORM_1120_HEADER_FIELDS: dict[str, str] = {
    # Corporation name — top of page 1 (Name read-order sub-group).
    "corp_name":        "topmostSubform[0].Page1[0].NameFieldsReadOrder[0].f1_4[0]",
    # B — EIN (Employer Identification Number), right column.
    "ein":              "topmostSubform[0].Page1[0].f1_11[0]",
    # C — Date incorporated, right column.
    "date_incorporated": "topmostSubform[0].Page1[0].f1_12[0]",
    # D — Total assets (see instructions), right column ("$" box).
    "total_assets":     "topmostSubform[0].Page1[0].f1_13[0]",
    # Tax-year "beginning … / ending …" boxes at the very top (PgHeader group).
    "tax_year_begin":   "topmostSubform[0].Page1[0].PgHeader[0].f1_1[0]",
    "tax_year_end":     "topmostSubform[0].Page1[0].PgHeader[0].f1_2[0]",
    # Business activity code (NAICS) — NOT a Form 1120 page-1 field (1120-S item
    # B only). Kept as a logical key; sentinel-mapped so it stays blank.
    "business_code":    _NOT_ON_FORM,
    # State of incorporation — NOT a Form 1120 page-1 field. Sentinel-mapped.
    "state_inc":        _NOT_ON_FORM,
    # Number of shareholders — NOT a Form 1120 page-1 field (1120-S item I only;
    # 1120 reports ownership on Schedule G / Schedule K). Sentinel-mapped.
    "num_shareholders": _NOT_ON_FORM,
    # E (1) — Check if initial return.
    "initial_return":   "topmostSubform[0].Page1[0].c1_6[0]",
    # E (2) — Check if final return.
    "final_return":     "topmostSubform[0].Page1[0].c1_7[0]",
    # E (3) — Check if name change.
    "name_change":      "topmostSubform[0].Page1[0].c1_8[0]",
    # E (4) — Check if address change.
    "address_change":   "topmostSubform[0].Page1[0].c1_9[0]",
    # Amended return — Form 1120 has NO page-1 amended checkbox (amend via Form
    # 1120-X). Kept as a logical key; sentinel-mapped so it stays blank.
    "amended_return":   _NOT_ON_FORM,
}

# ---------------------------------------------------------------------------
# Form 1120 page 1 — Income (lines 1a – 11)
# ---------------------------------------------------------------------------

FORM_1120_FIELDS: dict[str, str] = {
    # ---- Income section (re-pinned: income fields start at f1_14, after the
    #      header's f1_4–f1_13) ----
    # Line 1a — Gross receipts or sales (before returns/allowances)
    "1a":  "topmostSubform[0].Page1[0].f1_14[0]",
    # Line 1b — Returns and allowances
    "1b":  "topmostSubform[0].Page1[0].f1_15[0]",
    # Line 1c — Balance (line 1a minus 1b)
    "1c":  "topmostSubform[0].Page1[0].f1_16[0]",
    # Line 2  — Cost of goods sold (attach Form 1125-A)
    "2":   "topmostSubform[0].Page1[0].f1_17[0]",
    # Line 3  — Gross profit (line 1c minus line 2)
    "3":   "topmostSubform[0].Page1[0].f1_18[0]",
    # Line 4  — Dividends and inclusions (Schedule C, line 23)
    "4":   "topmostSubform[0].Page1[0].f1_19[0]",
    # Line 5  — Interest
    "5":   "topmostSubform[0].Page1[0].f1_20[0]",
    # Line 6  — Gross rents
    "6":   "topmostSubform[0].Page1[0].f1_21[0]",
    # Line 7  — Gross royalties
    "7":   "topmostSubform[0].Page1[0].f1_22[0]",
    # Line 8  — Capital gain net income (attach Schedule D)
    "8":   "topmostSubform[0].Page1[0].f1_23[0]",
    # Line 9  — Net gain or (loss) from Form 4797
    "9":   "topmostSubform[0].Page1[0].f1_24[0]",
    # Line 10 — Other income (attach statement)
    "10":  "topmostSubform[0].Page1[0].f1_25[0]",
    # Line 11 — Total income (add lines 3 through 10)
    "11":  "topmostSubform[0].Page1[0].f1_26[0]",

    # ---- Deductions section ----
    # Line 12 — Compensation of officers (attach Form 1125-E)
    "12":  "topmostSubform[0].Page1[0].f1_27[0]",
    # Line 13 — Salaries and wages (less employment credits)
    "13":  "topmostSubform[0].Page1[0].f1_28[0]",
    # Line 14 — Repairs and maintenance
    "14":  "topmostSubform[0].Page1[0].f1_29[0]",
    # Line 15 — Bad debts
    "15":  "topmostSubform[0].Page1[0].f1_30[0]",
    # Line 16 — Rents
    "16":  "topmostSubform[0].Page1[0].f1_31[0]",
    # Line 17 — Taxes and licenses
    "17":  "topmostSubform[0].Page1[0].f1_32[0]",
    # Line 18 — Interest (see instructions; §163(j) limit may apply)
    "18":  "topmostSubform[0].Page1[0].f1_33[0]",
    # Line 19 — Charitable contributions
    "19":  "topmostSubform[0].Page1[0].f1_34[0]",
    # Line 20 — Depreciation from Form 4562 not claimed on 1125-A
    "20":  "topmostSubform[0].Page1[0].f1_35[0]",
    # Line 21 — Depletion
    "21":  "topmostSubform[0].Page1[0].f1_36[0]",
    # Line 22 — Advertising
    "22":  "topmostSubform[0].Page1[0].f1_37[0]",
    # Line 23 — Pension, profit-sharing, etc., plans
    "23":  "topmostSubform[0].Page1[0].f1_38[0]",
    # Line 24 — Employee benefit programs
    "24":  "topmostSubform[0].Page1[0].f1_39[0]",
    # Line 25 — Energy efficient commercial buildings deduction (attach Form
    #           7205) — ADDED on the TY2023+ revision (the prior draft skipped it).
    "25":  "topmostSubform[0].Page1[0].f1_40[0]",
    # Line 26 — Other deductions (attach statement)
    "26":  "topmostSubform[0].Page1[0].f1_41[0]",
    # Line 27 — Total deductions (add lines 12 through 26)
    "27":  "topmostSubform[0].Page1[0].f1_42[0]",
    # Line 28 — Taxable income before NOL deduction and special deductions
    "28":  "topmostSubform[0].Page1[0].f1_43[0]",
    # Line 29a — Net operating loss (NOL) deduction
    "29a": "topmostSubform[0].Page1[0].f1_44[0]",
    # Line 29b — Special deductions (Schedule C, line 24)
    "29b": "topmostSubform[0].Page1[0].f1_45[0]",
    # Line 29c — Total (add lines 29a + 29b)
    "29c": "topmostSubform[0].Page1[0].f1_46[0]",
    # Line 30 — Taxable income (line 28 minus 29c)
    "30":  "topmostSubform[0].Page1[0].f1_47[0]",
    # Line 31 — Total tax (Schedule J, Part I, line 12)
    "31":  "topmostSubform[0].Page1[0].f1_48[0]",
    # Line 32 — First installment of section 1062 applicable net tax liability
    #           (added on the TY2023+ revision).
    "32":  "topmostSubform[0].Page1[0].f1_49[0]",
    # Line 33 — Total payments, credits, and section 1062 net tax liability
    "33":  "topmostSubform[0].Page1[0].f1_50[0]",
    # Line 34 — Estimated tax penalty (check if Form 2220 attached)
    "34":  "topmostSubform[0].Page1[0].f1_51[0]",
    # Line 35 — Amount owed (line 33 < lines 31 + 32 + 34; enter amount owed)
    "35":  "topmostSubform[0].Page1[0].f1_52[0]",
    # Line 36 — Overpayment (line 33 > lines 31 + 32 + 34; enter overpayment)
    "36":  "topmostSubform[0].Page1[0].f1_53[0]",
    # Line 37 — Amount of line 36 to be credited to next year's estimated tax
    "37":  "topmostSubform[0].Page1[0].f1_54[0]",
    # Line 38 — Amount of line 36 to be refunded
    "38":  "topmostSubform[0].Page1[0].f1_55[0]",
}

# ---------------------------------------------------------------------------
# Schedule L — Balance Sheet per Books. On f1120_2024.pdf this is on Page6[0]
# (the 6th PDF page), in structured table rows
# ``Table_SchL_Assets[0].Line{N}[0].f6_k[0]`` /
# ``Table_SchL_Liabilities[0].Line{N}[0].f6_k[0]``.
#
# Each line exposes 4 columns: (a) BOY detail, (b) BOY total, (c) EOY detail,
# (d) EOY total. We fill the END-OF-YEAR column only (BOY is left blank for CPA):
#   * single-amount lines -> column (d), the EOY total;
#   * gross/less detail lines (2a, 2b, 10a, 10b, 11a, 11b, 13a, 13b, 27) -> column
#     (c), the EOY detail box, since those keys carry the detail value.
# Field names below were re-pinned positionally against the real PDF.
# ---------------------------------------------------------------------------

SCHEDULE_L_FIELDS: dict[str, str] = {
    # --- Assets ---
    # Line 1  — Cash (EOY total)
    "1_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line1[0].f6_4[0]",
    # Line 2a — Trade notes and accounts receivable (EOY detail)
    "2a_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line2a[0].f6_7[0]",
    # Line 2b — Less allowance for bad debts (EOY detail; enter parenthetical)
    "2b_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line2b[0].f6_11[0]",
    # Line 3  — Inventories (EOY total)
    "3_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line3[0].f6_16[0]",
    # Line 4  — U.S. government obligations (EOY total)
    "4_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line4[0].f6_20[0]",
    # Line 5  — Tax-exempt securities (EOY total)
    "5_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line5[0].f6_24[0]",
    # Line 6  — Other current assets (EOY total)
    "6_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line6[0].f6_28[0]",
    # Line 7  — Loans to shareholders (EOY total)
    "7_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line7[0].f6_32[0]",
    # Line 8  — Mortgage and real estate loans (EOY total)
    "8_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line8[0].f6_36[0]",
    # Line 9  — Other investments (EOY total)
    "9_eoy":    "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line9[0].f6_40[0]",
    # Line 10a — Buildings and other depreciable assets, gross (EOY detail)
    "10a_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line10a[0].f6_43[0]",
    # Line 10b — Less accumulated depreciation (EOY detail)
    "10b_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line10b[0].f6_47[0]",
    # Line 11a — Depletable assets, gross (EOY detail)
    "11a_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line11a[0].f6_51[0]",
    # Line 11b — Less accumulated depletion (EOY detail)
    "11b_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line11b[0].f6_55[0]",
    # Line 12  — Land (net of amortization) (EOY total)
    "12_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line12[0].f6_60[0]",
    # Line 13a — Intangible assets (amortizable only), gross (EOY detail)
    "13a_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line13a[0].f6_63[0]",
    # Line 13b — Less accumulated amortization (EOY detail)
    "13b_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line13b[0].f6_67[0]",
    # Line 14  — Other assets (EOY total)
    "14_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line14[0].f6_72[0]",
    # Line 15  — Total assets (EOY total)
    "15_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Assets[0].Line15[0].f6_76[0]",
    # --- Liabilities and shareholders' equity ---
    # Line 16  — Accounts payable (EOY total)
    "16_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line16[0].f6_80[0]",
    # Line 17  — Mortgages, notes, bonds payable in less than 1 year (EOY total)
    "17_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line17[0].f6_84[0]",
    # Line 18  — Other current liabilities (EOY total)
    "18_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line18[0].f6_88[0]",
    # Line 19  — Loans from shareholders (EOY total)
    "19_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line19[0].f6_92[0]",
    # Line 20  — Mortgages, notes, bonds payable in 1 year or more (EOY total)
    "20_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line20[0].f6_96[0]",
    # Line 21  — Other liabilities (EOY total)
    "21_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line21[0].f6_100[0]",
    # Line 22a — Capital stock: preferred stock (EOY total)
    "22a_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line22a[0].f6_104[0]",
    # Line 22b — Capital stock: common stock (EOY total)
    "22b_eoy":  "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line22b[0].f6_108[0]",
    # Line 23  — Additional paid-in capital (EOY total)
    "23_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line23[0].f6_112[0]",
    # Line 24  — Retained earnings — appropriated (EOY total)
    "24_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line24[0].f6_116[0]",
    # Line 25  — Retained earnings — unappropriated (EOY total)
    "25_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line25[0].f6_120[0]",
    # Line 26  — Adjustments to shareholders' equity (EOY total)
    "26_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line26[0].f6_124[0]",
    # Line 27  — Less cost of treasury stock (EOY detail; enter parenthetical)
    "27_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line27[0].f6_127[0]",
    # Line 28  — Total liabilities and shareholders' equity (EOY total)
    "28_eoy":   "topmostSubform[0].Page6[0].Table_SchL_Liabilities[0].Line28[0].f6_132[0]",
}

# ---------------------------------------------------------------------------
# Form 1125-E — Compensation of Officers (separate PDF: f1125e.pdf)
# Required when total receipts ≥ $500,000.
# Up to 6 officer rows are provided in the standard form.
# ---------------------------------------------------------------------------

FORM_1125E_FIELDS: dict[str, str] = {
    # ---- Header ----
    "corp_name":   "topmostSubform[0].Page1[0].f1_1[0]",
    "ein":         "topmostSubform[0].Page1[0].f1_2[0]",
    "tax_year":    "topmostSubform[0].Page1[0].f1_3[0]",

    # ---- Officer row 1 ----
    "officer1_name":        "topmostSubform[0].Page1[0].f1_4[0]",
    "officer1_ssn":         "topmostSubform[0].Page1[0].f1_5[0]",
    "officer1_title":       "topmostSubform[0].Page1[0].f1_6[0]",
    "officer1_pct_time":    "topmostSubform[0].Page1[0].f1_7[0]",  # % of time devoted
    "officer1_pct_stock_c": "topmostSubform[0].Page1[0].f1_8[0]",  # % stock owned common
    "officer1_pct_stock_p": "topmostSubform[0].Page1[0].f1_9[0]",  # % stock owned preferred
    "officer1_amount":      "topmostSubform[0].Page1[0].f1_10[0]", # compensation $

    # ---- Officer row 2 ----
    "officer2_name":        "topmostSubform[0].Page1[0].f1_11[0]",
    "officer2_ssn":         "topmostSubform[0].Page1[0].f1_12[0]",
    "officer2_title":       "topmostSubform[0].Page1[0].f1_13[0]",
    "officer2_pct_time":    "topmostSubform[0].Page1[0].f1_14[0]",
    "officer2_pct_stock_c": "topmostSubform[0].Page1[0].f1_15[0]",
    "officer2_pct_stock_p": "topmostSubform[0].Page1[0].f1_16[0]",
    "officer2_amount":      "topmostSubform[0].Page1[0].f1_17[0]",

    # ---- Officer row 3 ----
    "officer3_name":        "topmostSubform[0].Page1[0].f1_18[0]",
    "officer3_ssn":         "topmostSubform[0].Page1[0].f1_19[0]",
    "officer3_title":       "topmostSubform[0].Page1[0].f1_20[0]",
    "officer3_pct_time":    "topmostSubform[0].Page1[0].f1_21[0]",
    "officer3_pct_stock_c": "topmostSubform[0].Page1[0].f1_22[0]",
    "officer3_pct_stock_p": "topmostSubform[0].Page1[0].f1_23[0]",
    "officer3_amount":      "topmostSubform[0].Page1[0].f1_24[0]",

    # ---- Officer row 4 ----
    "officer4_name":        "topmostSubform[0].Page1[0].f1_25[0]",
    "officer4_ssn":         "topmostSubform[0].Page1[0].f1_26[0]",
    "officer4_title":       "topmostSubform[0].Page1[0].f1_27[0]",
    "officer4_pct_time":    "topmostSubform[0].Page1[0].f1_28[0]",
    "officer4_pct_stock_c": "topmostSubform[0].Page1[0].f1_29[0]",
    "officer4_pct_stock_p": "topmostSubform[0].Page1[0].f1_30[0]",
    "officer4_amount":      "topmostSubform[0].Page1[0].f1_31[0]",

    # ---- Officer row 5 ----
    "officer5_name":        "topmostSubform[0].Page1[0].f1_32[0]",
    "officer5_ssn":         "topmostSubform[0].Page1[0].f1_33[0]",
    "officer5_title":       "topmostSubform[0].Page1[0].f1_34[0]",
    "officer5_pct_time":    "topmostSubform[0].Page1[0].f1_35[0]",
    "officer5_pct_stock_c": "topmostSubform[0].Page1[0].f1_36[0]",
    "officer5_pct_stock_p": "topmostSubform[0].Page1[0].f1_37[0]",
    "officer5_amount":      "topmostSubform[0].Page1[0].f1_38[0]",

    # ---- Officer row 6 ----
    "officer6_name":        "topmostSubform[0].Page1[0].f1_39[0]",
    "officer6_ssn":         "topmostSubform[0].Page1[0].f1_40[0]",
    "officer6_title":       "topmostSubform[0].Page1[0].f1_41[0]",
    "officer6_pct_time":    "topmostSubform[0].Page1[0].f1_42[0]",
    "officer6_pct_stock_c": "topmostSubform[0].Page1[0].f1_43[0]",
    "officer6_pct_stock_p": "topmostSubform[0].Page1[0].f1_44[0]",
    "officer6_amount":      "topmostSubform[0].Page1[0].f1_45[0]",

    # ---- Totals ----
    # Line 1 — Total compensation of officers
    "total_officer_comp":   "topmostSubform[0].Page1[0].f1_46[0]",
    # Line 2 — Compensation of officers claimed on Form 1125-A or elsewhere
    "comp_elsewhere":       "topmostSubform[0].Page1[0].f1_47[0]",
    # Line 3 — Subtract line 2 from line 1. Enter result here and on Form 1120, line 12
    "net_officer_comp":     "topmostSubform[0].Page1[0].f1_48[0]",
}

# ---------------------------------------------------------------------------
# Mapping from our Form 1120 line numbers → Schedule L field key suffixes.
# Used by the filler to route balance-sheet line amounts to the right fields.
# ---------------------------------------------------------------------------

SCHEDULE_L_LINE_TO_FIELD: dict[str, str] = {
    "1":   "1_eoy",
    "2a":  "2a_eoy",
    "2b":  "2b_eoy",
    "3":   "3_eoy",
    "4":   "4_eoy",
    "5":   "5_eoy",
    "6":   "6_eoy",
    "7":   "7_eoy",
    "8":   "8_eoy",
    "9":   "9_eoy",
    "10a": "10a_eoy",
    "10b": "10b_eoy",
    "11a": "11a_eoy",
    "11b": "11b_eoy",
    "12":  "12_eoy",
    "13a": "13a_eoy",
    "13b": "13b_eoy",
    "14":  "14_eoy",
    "15":  "15_eoy",
    "16":  "16_eoy",
    "17":  "17_eoy",
    "18":  "18_eoy",
    "19":  "19_eoy",
    "20":  "20_eoy",
    "21":  "21_eoy",
    "22a": "22a_eoy",
    "22b": "22b_eoy",
    "23":  "23_eoy",
    "24":  "24_eoy",
    "25":  "25_eoy",
    "26":  "26_eoy",
    "27":  "27_eoy",
    "28":  "28_eoy",
}
