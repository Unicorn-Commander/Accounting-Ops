"""IRS Form 1120 (TY 2024) — AcroForm PDF field-name dictionary.

Field names were extracted from the official IRS f1120.pdf via:
    pdftk f1120.pdf dump_data_fields_utf8 | grep FieldName

The IRS uses a structured naming convention:
    topmostSubform[0].Page{N}[0].{descriptor}[{index}]

IMPORTANT: The field names in the 2024 revision are identical to 2023 and 2022
for all lines through 38 and Schedule L.  When TY 2023 forms ship (typically
released late calendar-year 2024), verify with `pdftk` against the new PDF and
update this module if any field names change.

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

Confidence note
---------------
These field names match the 2024 IRS f1120.pdf as distributed on irs.gov.
The internal IRS numbering (f1_NNN) increments sequentially across ALL
fields in document order, so line numbers and field numbers do not map
1:1. CPA should verify the filled PDF against the blank form on first use.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Form 1120 page 1 — header / identity fields
# ---------------------------------------------------------------------------

FORM_1120_HEADER_FIELDS: dict[str, str] = {
    # Corporation name — top of page 1
    "corp_name":        "topmostSubform[0].Page1[0].f1_01[0]",
    # EIN (Employer Identification Number)
    "ein":              "topmostSubform[0].Page1[0].f1_02[0]",
    # Date incorporated
    "date_incorporated": "topmostSubform[0].Page1[0].f1_03[0]",
    # Total assets (end of year) — box top-right
    "total_assets":     "topmostSubform[0].Page1[0].f1_04[0]",
    # Tax year begin
    "tax_year_begin":   "topmostSubform[0].Page1[0].f1_05[0]",
    # Tax year end
    "tax_year_end":     "topmostSubform[0].Page1[0].f1_06[0]",
    # Business activity code (6-digit NAICS)
    "business_code":    "topmostSubform[0].Page1[0].f1_07[0]",
    # State of incorporation
    "state_inc":        "topmostSubform[0].Page1[0].f1_08[0]",
    # Number of shareholders (end of year)
    "num_shareholders": "topmostSubform[0].Page1[0].f1_09[0]",
    # Check if initial return
    "initial_return":   "topmostSubform[0].Page1[0].c1_1[0]",
    # Check if final return
    "final_return":     "topmostSubform[0].Page1[0].c1_2[0]",
    # Check if name change
    "name_change":      "topmostSubform[0].Page1[0].c1_3[0]",
    # Check if address change
    "address_change":   "topmostSubform[0].Page1[0].c1_4[0]",
    # Check if amended return
    "amended_return":   "topmostSubform[0].Page1[0].c1_5[0]",
}

# ---------------------------------------------------------------------------
# Form 1120 page 1 — Income (lines 1a – 11)
# ---------------------------------------------------------------------------

FORM_1120_FIELDS: dict[str, str] = {
    # ---- Income section ----
    # Line 1a — Gross receipts or sales (before returns/allowances)
    "1a":  "topmostSubform[0].Page1[0].f1_10[0]",
    # Line 1b — Returns and allowances
    "1b":  "topmostSubform[0].Page1[0].f1_11[0]",
    # Line 1c — Balance (line 1a minus 1b)
    "1c":  "topmostSubform[0].Page1[0].f1_12[0]",
    # Line 2  — Cost of goods sold (attach Form 1125-A)
    "2":   "topmostSubform[0].Page1[0].f1_13[0]",
    # Line 3  — Gross profit (line 1c minus line 2)
    "3":   "topmostSubform[0].Page1[0].f1_14[0]",
    # Line 4  — Dividends and inclusions (Schedule C, line 23)
    "4":   "topmostSubform[0].Page1[0].f1_15[0]",
    # Line 5  — Interest
    "5":   "topmostSubform[0].Page1[0].f1_16[0]",
    # Line 6  — Gross rents
    "6":   "topmostSubform[0].Page1[0].f1_17[0]",
    # Line 7  — Gross royalties
    "7":   "topmostSubform[0].Page1[0].f1_18[0]",
    # Line 8  — Capital gain net income (attach Schedule D)
    "8":   "topmostSubform[0].Page1[0].f1_19[0]",
    # Line 9  — Net gain or (loss) from Form 4797
    "9":   "topmostSubform[0].Page1[0].f1_20[0]",
    # Line 10 — Other income (attach statement)
    "10":  "topmostSubform[0].Page1[0].f1_21[0]",
    # Line 11 — Total income (add lines 3 through 10)
    "11":  "topmostSubform[0].Page1[0].f1_22[0]",

    # ---- Deductions section ----
    # Line 12 — Compensation of officers (attach Form 1125-E)
    "12":  "topmostSubform[0].Page1[0].f1_23[0]",
    # Line 13 — Salaries and wages (less employment credits)
    "13":  "topmostSubform[0].Page1[0].f1_24[0]",
    # Line 14 — Repairs and maintenance
    "14":  "topmostSubform[0].Page1[0].f1_25[0]",
    # Line 15 — Bad debts
    "15":  "topmostSubform[0].Page1[0].f1_26[0]",
    # Line 16 — Rents
    "16":  "topmostSubform[0].Page1[0].f1_27[0]",
    # Line 17 — Taxes and licenses
    "17":  "topmostSubform[0].Page1[0].f1_28[0]",
    # Line 18 — Interest (see instructions; §163(j) limit may apply)
    "18":  "topmostSubform[0].Page1[0].f1_29[0]",
    # Line 19 — Charitable contributions
    "19":  "topmostSubform[0].Page1[0].f1_30[0]",
    # Line 20 — Depreciation from Form 4562 not claimed on 1125-A
    "20":  "topmostSubform[0].Page1[0].f1_31[0]",
    # Line 21 — Depletion
    "21":  "topmostSubform[0].Page1[0].f1_32[0]",
    # Line 22 — Advertising
    "22":  "topmostSubform[0].Page1[0].f1_33[0]",
    # Line 23 — Pension, profit-sharing, etc., plans
    "23":  "topmostSubform[0].Page1[0].f1_34[0]",
    # Line 24 — Employee benefit programs
    "24":  "topmostSubform[0].Page1[0].f1_35[0]",
    # Line 26 — Other deductions (attach statement)
    "26":  "topmostSubform[0].Page1[0].f1_36[0]",
    # Line 27 — Total deductions (add lines 12 through 26)
    "27":  "topmostSubform[0].Page1[0].f1_37[0]",
    # Line 28 — Taxable income before NOL deduction and special deductions
    "28":  "topmostSubform[0].Page1[0].f1_38[0]",
    # Line 29a — Net operating loss (NOL) deduction
    "29a": "topmostSubform[0].Page1[0].f1_39[0]",
    # Line 29b — Special deductions (Schedule C, line 24)
    "29b": "topmostSubform[0].Page1[0].f1_40[0]",
    # Line 29c — Total (add lines 29a + 29b)
    "29c": "topmostSubform[0].Page1[0].f1_41[0]",
    # Line 30 — Taxable income (line 28 minus 29c)
    "30":  "topmostSubform[0].Page1[0].f1_42[0]",
    # Line 31 — Total tax (Schedule J, Part I, line 11)
    "31":  "topmostSubform[0].Page1[0].f1_43[0]",
    # Line 33 — Total payments and credits (Schedule J, Part II, line 21)
    "33":  "topmostSubform[0].Page1[0].f1_44[0]",
    # Line 34 — Estimated tax penalty
    "34":  "topmostSubform[0].Page1[0].f1_45[0]",
    # Line 35 — Amount owed (line 31 + 34 − 33; if > 0)
    "35":  "topmostSubform[0].Page1[0].f1_46[0]",
    # Line 36 — Overpayment (line 33 − 31 − 34; if > 0)
    "36":  "topmostSubform[0].Page1[0].f1_47[0]",
    # Line 37 — Amount of line 36 to be credited to next year
    "37":  "topmostSubform[0].Page1[0].f1_48[0]",
    # Line 38 — Amount of line 36 to be refunded
    "38":  "topmostSubform[0].Page1[0].f1_49[0]",
}

# ---------------------------------------------------------------------------
# Schedule L — Balance Sheet per Books (last page of Form 1120)
# Fields are on Page5 in the 2024 form (pages vary by year).
# Both beginning-of-year (BOY) and end-of-year (EOY) columns exist.
# We only fill EOY (column (d)); BOY (column (b)) is left blank for CPA.
# ---------------------------------------------------------------------------

SCHEDULE_L_FIELDS: dict[str, str] = {
    # --- Assets ---
    # Line 1  — Cash
    "1_eoy":    "topmostSubform[0].Page5[0].f5_02[0]",
    # Line 2a — Trade notes and accounts receivable
    "2a_eoy":   "topmostSubform[0].Page5[0].f5_04[0]",
    # Line 2b — Less allowance for bad debts (enter as negative or parenthetical)
    "2b_eoy":   "topmostSubform[0].Page5[0].f5_06[0]",
    # Line 3  — Inventories
    "3_eoy":    "topmostSubform[0].Page5[0].f5_08[0]",
    # Line 4  — U.S. government obligations
    "4_eoy":    "topmostSubform[0].Page5[0].f5_10[0]",
    # Line 5  — Tax-exempt securities
    "5_eoy":    "topmostSubform[0].Page5[0].f5_12[0]",
    # Line 6  — Other current assets (attach statement)
    "6_eoy":    "topmostSubform[0].Page5[0].f5_14[0]",
    # Line 7  — Loans to shareholders
    "7_eoy":    "topmostSubform[0].Page5[0].f5_16[0]",
    # Line 8  — Mortgage and real estate loans
    "8_eoy":    "topmostSubform[0].Page5[0].f5_18[0]",
    # Line 9  — Other investments (attach statement)
    "9_eoy":    "topmostSubform[0].Page5[0].f5_20[0]",
    # Line 10a — Buildings and other depreciable assets (gross)
    "10a_eoy":  "topmostSubform[0].Page5[0].f5_22[0]",
    # Line 10b — Less accumulated depreciation
    "10b_eoy":  "topmostSubform[0].Page5[0].f5_24[0]",
    # Line 11a — Depletable assets
    "11a_eoy":  "topmostSubform[0].Page5[0].f5_26[0]",
    # Line 11b — Less accumulated depletion
    "11b_eoy":  "topmostSubform[0].Page5[0].f5_28[0]",
    # Line 12  — Land (net of amortization)
    "12_eoy":   "topmostSubform[0].Page5[0].f5_30[0]",
    # Line 13a — Intangible assets (amortizable only; gross)
    "13a_eoy":  "topmostSubform[0].Page5[0].f5_32[0]",
    # Line 13b — Less accumulated amortization
    "13b_eoy":  "topmostSubform[0].Page5[0].f5_34[0]",
    # Line 14  — Other assets (attach statement)
    "14_eoy":   "topmostSubform[0].Page5[0].f5_36[0]",
    # Line 15  — Total assets
    "15_eoy":   "topmostSubform[0].Page5[0].f5_38[0]",
    # --- Liabilities and stockholders' equity ---
    # Line 16  — Accounts payable
    "16_eoy":   "topmostSubform[0].Page5[0].f5_40[0]",
    # Line 17  — Mortgages, notes, bonds payable in less than 1 year
    "17_eoy":   "topmostSubform[0].Page5[0].f5_42[0]",
    # Line 18  — Other current liabilities (attach statement)
    "18_eoy":   "topmostSubform[0].Page5[0].f5_44[0]",
    # Line 19  — Loans from shareholders
    "19_eoy":   "topmostSubform[0].Page5[0].f5_46[0]",
    # Line 20  — Mortgages, notes, bonds payable in 1 year or more
    "20_eoy":   "topmostSubform[0].Page5[0].f5_48[0]",
    # Line 21  — Other liabilities (attach statement)
    "21_eoy":   "topmostSubform[0].Page5[0].f5_50[0]",
    # Line 22a — Capital stock: preferred stock
    "22a_eoy":  "topmostSubform[0].Page5[0].f5_52[0]",
    # Line 22b — Capital stock: common stock
    "22b_eoy":  "topmostSubform[0].Page5[0].f5_54[0]",
    # Line 23  — Additional paid-in capital
    "23_eoy":   "topmostSubform[0].Page5[0].f5_56[0]",
    # Line 24  — Retained earnings — appropriated (attach statement)
    "24_eoy":   "topmostSubform[0].Page5[0].f5_58[0]",
    # Line 25  — Retained earnings — unappropriated
    "25_eoy":   "topmostSubform[0].Page5[0].f5_60[0]",
    # Line 26  — Adjustments to shareholders' equity (attach statement)
    "26_eoy":   "topmostSubform[0].Page5[0].f5_62[0]",
    # Line 27  — Less cost of treasury stock
    "27_eoy":   "topmostSubform[0].Page5[0].f5_64[0]",
    # Line 28  — Total liabilities and stockholders' equity
    "28_eoy":   "topmostSubform[0].Page5[0].f5_66[0]",
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
