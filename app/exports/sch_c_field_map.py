"""IRS Schedule C (Form 1040) — Profit or Loss From Business — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1040sc.pdf``, **Tax Year 2025** revision, "Created 4/3/25")
with ``pypdf`` (``PdfReader().get_fields()``) and pinning every terminal field's
widget /Rect against the printed line labels extracted from the text layer (a
positional audit — this revision carries no XFA tooltips, so geometry is the
source of truth). Schedule C is a modern XFA-hybrid AcroForm
(``topmostSubform[0]`` naming, like Form 1040 / the W-4P), so it MUST be filled
via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that
bakes values in as a flattened, visible overlay), NOT the older page-based
``_fill_pdf_fields``.

Pattern mirrors ``f1040_field_map.py`` / ``w4p_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name,
plus small companion maps for the checkbox/radio groups.

This map covers identity + the header questions + every Part I income line +
every Part II expense line + the profit/home/at-risk lines + Part III (cost of
goods sold) + Part IV (vehicle) + the Part V other-expenses table. It is the
honest "fill what the taxpayer / their books give us onto the official form"
surface for the professional-substitute renderer — it does NOT compute the
schedule.

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas). The
  IRS AcroForm boxes are plain-text, not numeric. The filler writes the caller's
  raw string verbatim (conservative — never derives or zero-fills a line).
- The IRS ``f{page}_{NN}`` numbering increments in document/tab order, so field
  numbers do NOT map 1:1 to line numbers — every name below was pinned
  positionally to its printed line via the widget /Rect.
- 2025 ordering note (verified on this PDF): line 27a is "Energy efficient
  commercial bldgs deduction (attach Form 7205)" and line 27b is "Other expenses
  (from line 48)". Keys below are named by CONTENT so the meaning is unambiguous
  regardless of the a/b lettering.

Left blank for the human (NOT in this map)
------------------------------------------
The proprietor's SSN (page-1 header box ``f1_2[0]``), the business EIN
(line D comb box ``DComb[0].f1_6[0]``), and the signature are intentionally
omitted so the renderer can never fill them — they are wet-ink / identity items
the taxpayer must enter themselves.

CPA/EA verification note
------------------------
These names match the TY2025 f1040sc.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1040sc_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
SCH_C_FIELDS: dict[str, str] = {
    # --- Identity / business header (page 1) ---
    "name_of_proprietor":        f"{_P1}.f1_1[0]",   # Name of proprietor
    "principal_business":        f"{_P1}.f1_3[0]",   # A — principal business or profession
    "business_code":             f"{_P1}.BComb[0].f1_4[0]",  # B — 6-digit business code
    "business_name":             f"{_P1}.f1_5[0]",   # C — business name (blank if none)
    "business_address":          f"{_P1}.f1_7[0]",   # E — address (incl. suite/room no.)
    "business_city_state_zip":   f"{_P1}.f1_8[0]",   # E — city, town, state, ZIP
    "accounting_method_other":   f"{_P1}.f1_9[0]",   # F — "Other (specify)" text

    # --- Part I: Income ---
    "line1_gross_receipts":      f"{_P1}.f1_10[0]",  # 1 — gross receipts or sales
    "line2_returns_allowances":  f"{_P1}.f1_11[0]",  # 2 — returns and allowances
    "line3_net_receipts":        f"{_P1}.f1_12[0]",  # 3 — subtract line 2 from line 1
    "line4_cogs":                f"{_P1}.f1_13[0]",  # 4 — cost of goods sold (from line 42)
    "line5_gross_profit":        f"{_P1}.f1_14[0]",  # 5 — gross profit
    "line6_other_income":        f"{_P1}.f1_15[0]",  # 6 — other income
    "line7_gross_income":        f"{_P1}.f1_16[0]",  # 7 — gross income

    # --- Part II: Expenses (left column, lines 8–17) ---
    "line8_advertising":         f"{_P1}.Lines8-17[0].f1_17[0]",  # 8
    "line9_car_truck":           f"{_P1}.Lines8-17[0].f1_18[0]",  # 9 — car and truck
    "line10_commissions_fees":   f"{_P1}.Lines8-17[0].f1_19[0]",  # 10
    "line11_contract_labor":     f"{_P1}.Lines8-17[0].f1_20[0]",  # 11
    "line12_depletion":          f"{_P1}.Lines8-17[0].f1_21[0]",  # 12
    "line13_depreciation":       f"{_P1}.Lines8-17[0].f1_22[0]",  # 13 — depreciation/sec 179
    "line14_employee_benefits":  f"{_P1}.Lines8-17[0].f1_23[0]",  # 14
    "line15_insurance":          f"{_P1}.Lines8-17[0].f1_24[0]",  # 15 — insurance (non-health)
    "line16a_mortgage_interest": f"{_P1}.Lines8-17[0].f1_25[0]",  # 16a — interest, mortgage
    "line16b_other_interest":    f"{_P1}.Lines8-17[0].f1_26[0]",  # 16b — interest, other
    "line17_legal_professional": f"{_P1}.Lines8-17[0].f1_27[0]",  # 17

    # --- Part II: Expenses (right column, lines 18–27) ---
    "line18_office_expense":     f"{_P1}.Lines18-27[0].f1_28[0]",  # 18
    "line19_pension":            f"{_P1}.Lines18-27[0].f1_29[0]",  # 19 — pension/profit-sharing
    "line20a_rent_vehicles":     f"{_P1}.Lines18-27[0].f1_30[0]",  # 20a — rent: vehicles/equip
    "line20b_rent_other":        f"{_P1}.Lines18-27[0].f1_31[0]",  # 20b — rent: other property
    "line21_repairs":            f"{_P1}.Lines18-27[0].f1_32[0]",  # 21 — repairs and maintenance
    "line22_supplies":           f"{_P1}.Lines18-27[0].f1_33[0]",  # 22
    "line23_taxes_licenses":     f"{_P1}.Lines18-27[0].f1_34[0]",  # 23
    "line24a_travel":            f"{_P1}.Lines18-27[0].f1_35[0]",  # 24a — travel
    "line24b_meals":             f"{_P1}.Lines18-27[0].f1_36[0]",  # 24b — deductible meals
    "line25_utilities":          f"{_P1}.Lines18-27[0].f1_37[0]",  # 25
    "line26_wages":              f"{_P1}.Lines18-27[0].f1_38[0]",  # 26 — wages (less credits)
    # 2025 ordering: 27a = energy-efficient bldgs deduction, 27b = other expenses.
    "line27a_energy_efficient_bldgs": f"{_P1}.Lines18-27[0].f1_40[0]",  # 27a
    "line27b_other_expenses":    f"{_P1}.Lines18-27[0].f1_39[0]",  # 27b — other (from line 48)

    # --- Part II: Totals / profit (page 1) ---
    "line28_total_expenses":     f"{_P1}.f1_41[0]",  # 28 — add lines 8 through 27b
    "line29_tentative_profit":   f"{_P1}.f1_42[0]",  # 29 — tentative profit/(loss)
    "line30_home_business_use":  f"{_P1}.f1_45[0]",  # 30 — business use of home
    "line31_net_profit":         f"{_P1}.f1_46[0]",  # 31 — net profit or (loss)
    # Line 30 simplified-method square footage (a) home / (b) business-use part.
    "line30_simplified_total_sqft":    f"{_P1}.Line30_ReadOrder[0].f1_43[0]",
    "line30_simplified_business_sqft": f"{_P1}.Line30_ReadOrder[0].f1_44[0]",

    # --- Part III: Cost of Goods Sold (page 2) ---
    "cogs_line35_inventory_begin": f"{_P2}.f2_1[0]",  # 35 — beginning inventory
    "cogs_line36_purchases":       f"{_P2}.f2_2[0]",  # 36 — purchases (less personal use)
    "cogs_line37_labor":           f"{_P2}.f2_3[0]",  # 37 — cost of labor
    "cogs_line38_materials":       f"{_P2}.f2_4[0]",  # 38 — materials and supplies
    "cogs_line39_other_costs":     f"{_P2}.f2_5[0]",  # 39 — other costs
    "cogs_line40_total":           f"{_P2}.f2_6[0]",  # 40 — add lines 35–39
    "cogs_line41_inventory_end":   f"{_P2}.f2_7[0]",  # 41 — ending inventory
    "cogs_line42_cogs":            f"{_P2}.f2_8[0]",  # 42 — cost of goods sold

    # --- Part IV: Information on Your Vehicle (page 2) ---
    "vehicle_date_month":          f"{_P2}.f2_9[0]",   # 43 — placed in service: month
    "vehicle_date_day":            f"{_P2}.f2_10[0]",  # 43 — day
    "vehicle_date_year":           f"{_P2}.f2_11[0]",  # 43 — year
    "vehicle_miles_business":      f"{_P2}.f2_12[0]",  # 44a — business miles
    "vehicle_miles_commuting":     f"{_P2}.f2_13[0]",  # 44b — commuting miles
    "vehicle_miles_other":         f"{_P2}.f2_14[0]",  # 44c — other miles

    # --- Part V: Other Expenses table (page 2) — 9 description/amount rows ---
    "other_expense_1_desc":    f"{_P2}.PartVTable[0].Item1[0].f2_15[0]",
    "other_expense_1_amount":  f"{_P2}.PartVTable[0].Item1[0].f2_16[0]",
    "other_expense_2_desc":    f"{_P2}.PartVTable[0].Item2[0].f2_17[0]",
    "other_expense_2_amount":  f"{_P2}.PartVTable[0].Item2[0].f2_18[0]",
    "other_expense_3_desc":    f"{_P2}.PartVTable[0].Item3[0].f2_19[0]",
    "other_expense_3_amount":  f"{_P2}.PartVTable[0].Item3[0].f2_20[0]",
    "other_expense_4_desc":    f"{_P2}.PartVTable[0].Item4[0].f2_21[0]",
    "other_expense_4_amount":  f"{_P2}.PartVTable[0].Item4[0].f2_22[0]",
    "other_expense_5_desc":    f"{_P2}.PartVTable[0].Item5[0].f2_23[0]",
    "other_expense_5_amount":  f"{_P2}.PartVTable[0].Item5[0].f2_24[0]",
    "other_expense_6_desc":    f"{_P2}.PartVTable[0].Item6[0].f2_25[0]",
    "other_expense_6_amount":  f"{_P2}.PartVTable[0].Item6[0].f2_26[0]",
    "other_expense_7_desc":    f"{_P2}.PartVTable[0].Item7[0].f2_27[0]",
    "other_expense_7_amount":  f"{_P2}.PartVTable[0].Item7[0].f2_28[0]",
    "other_expense_8_desc":    f"{_P2}.PartVTable[0].Item8[0].f2_29[0]",
    "other_expense_8_amount":  f"{_P2}.PartVTable[0].Item8[0].f2_30[0]",
    "other_expense_9_desc":    f"{_P2}.PartVTable[0].Item9[0].f2_31[0]",
    "other_expense_9_amount":  f"{_P2}.PartVTable[0].Item9[0].f2_32[0]",
    "line48_total_other_expenses": f"{_P2}.f2_33[0]",  # 48 — total (-> line 27b)
}

# ---------------------------------------------------------------------------
# Line F — accounting method (radio): logical value -> (field name, on-state).
# Exactly one is set; the others remain /Off (conservative).
# ---------------------------------------------------------------------------
SCH_C_ACCOUNTING_METHOD_FIELDS: dict[str, tuple[str, str]] = {
    "cash":    (f"{_P1}.c1_1[0]", "1"),
    "accrual": (f"{_P1}.c1_1[1]", "2"),
    "other":   (f"{_P1}.c1_1[2]", "3"),
}

# ---------------------------------------------------------------------------
# Line 33 — method(s) used to value closing inventory. These are THREE
# independent checkboxes (each on-state /1); set whichever apply.
# ---------------------------------------------------------------------------
SCH_C_COGS_METHOD_FIELDS: dict[str, tuple[str, str]] = {
    "cost":                    (f"{_P2}.c2_1[0]", "1"),
    "lower_of_cost_or_market": (f"{_P2}.c2_2[0]", "1"),
    "other":                   (f"{_P2}.c2_3[0]", "1"),
}

# ---------------------------------------------------------------------------
# Line 32 — at-risk question (two mutually exclusive boxes).
# ---------------------------------------------------------------------------
SCH_C_AT_RISK_FIELDS: dict[str, tuple[str, str]] = {
    "all":  (f"{_P1}.c1_7[0]", "1"),  # 32a — all investment is at risk
    "some": (f"{_P1}.c1_7[1]", "2"),  # 32b — some investment is not at risk
}

# ---------------------------------------------------------------------------
# Standalone "check-or-not" boxes: logical key -> (field name, on-state).
# Only checked when the caller supplies a truthy value.
# ---------------------------------------------------------------------------
SCH_C_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 1 — statutory-employee box (income reported on W-2, box 13 checked).
    "statutory_employee": (f"{_P1}.Line1_ReadOrder[0].c1_6[0]", "1"),
    # Line H — started or acquired this business during the year.
    "started_business":   (f"{_P1}.c1_3[0]", "1"),
}

# ---------------------------------------------------------------------------
# Yes/No question groups: logical key -> {"yes"|"no": (field name, on-state)}.
# The page-1 header questions (G, I, J) use /Yes & /No on-states; the page-2
# questions (line 34, 45, 46, 47a, 47b) use /1 (Yes) & /2 (No). Verified per
# field via the widget /AP on-state names.
# ---------------------------------------------------------------------------
SCH_C_YESNO_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    # G — did you "materially participate"?
    "material_participation": {
        "yes": (f"{_P1}.c1_2[0]", "Yes"), "no": (f"{_P1}.c1_2[1]", "No"),
    },
    # I — did you make payments that would require filing Form(s) 1099?
    "made_1099_payments": {
        "yes": (f"{_P1}.c1_4[0]", "Yes"), "no": (f"{_P1}.c1_4[1]", "No"),
    },
    # J — if "Yes", did you or will you file required Form(s) 1099?
    "will_file_1099": {
        "yes": (f"{_P1}.c1_5[0]", "Yes"), "no": (f"{_P1}.c1_5[1]", "No"),
    },
    # 34 — any change in determining quantities, costs, or valuations?
    "cogs_inventory_change": {
        "yes": (f"{_P2}.c2_4[0]", "1"), "no": (f"{_P2}.c2_4[1]", "2"),
    },
    # 45 — vehicle available for personal use during off-duty hours?
    "vehicle_personal_use": {
        "yes": (f"{_P2}.c2_5[0]", "1"), "no": (f"{_P2}.c2_5[1]", "2"),
    },
    # 46 — another vehicle available for personal use?
    "vehicle_another_available": {
        "yes": (f"{_P2}.c2_6[0]", "1"), "no": (f"{_P2}.c2_6[1]", "2"),
    },
    # 47a — do you have evidence to support your deduction?
    "vehicle_evidence": {
        "yes": (f"{_P2}.c2_7[0]", "1"), "no": (f"{_P2}.c2_7[1]", "2"),
    },
    # 47b — if "Yes", is the evidence written?
    "vehicle_evidence_written": {
        "yes": (f"{_P2}.c2_8[0]", "1"), "no": (f"{_P2}.c2_8[1]", "2"),
    },
}


def _all_checkbox_field_names() -> list[str]:
    names: list[str] = []
    names += [v[0] for v in SCH_C_ACCOUNTING_METHOD_FIELDS.values()]
    names += [v[0] for v in SCH_C_COGS_METHOD_FIELDS.values()]
    names += [v[0] for v in SCH_C_AT_RISK_FIELDS.values()]
    names += [v[0] for v in SCH_C_CHECKBOX_FIELDS.values()]
    for group in SCH_C_YESNO_FIELDS.values():
        names += [v[0] for v in group.values()]
    return names


# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Schedule C fields.
VALID_SCH_C_FIELDS: frozenset[str] = frozenset(
    list(SCH_C_FIELDS.values()) + _all_checkbox_field_names()
)
