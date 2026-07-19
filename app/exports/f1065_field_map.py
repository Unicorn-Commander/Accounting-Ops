"""IRS Form 1065 (U.S. Return of Partnership Income) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1065.pdf``, **Tax Year 2025** revision, "Created 11/25/25")
with ``pypdf`` (``PdfReader().get_fields()``) and cross-referencing every
terminal field's widget /Rect against the printed line labels (a positional
audit — the IRS XFA tooltips are empty on this form). Form 1065 is a modern
XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the W-4P / 1040), so it
MUST be filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe walker),
not the older page-based ``_fill_pdf_fields``.

Pattern mirrors ``f1040_field_map.py`` / ``w4p_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.

Scope
-----
This map covers **page 1** (the return face): the header identity block, the
common income lines 1a–8, the deduction lines 9–22, the tax-and-payment lines
23–32, plus the page-1 checkboxes (box G initial/final/etc., box H accounting
method, box J, box K, the direct-deposit account type, and the "may the IRS
discuss" question). It does NOT cover Schedule B (page 2-3 yes/no questions),
Schedule K (page 4-5), or the Analysis / Schedule L / M-1 / M-2 (page 6) — those
are out of scope for the page-1 professional-substitute fill surface. It is the
honest "fill what the partnership / their books give us onto the official form"
surface; it does not compute a return.

Key conventions
---------------
- Dollar amounts are written as plain strings ("125000" — no $ or commas). The
  IRS AcroForm boxes are plain-text, not numeric. A loss uses the form's
  parenthetical/negative convention; the filler writes the caller's raw string.
- The IRS ``f1_NN`` numbering increments in document/tab order, so field numbers
  do NOT map 1:1 to line numbers — every name below was pinned positionally to
  its printed line.
- The partnership **EIN** (field ``...Page1[0].f1_14[0]``) and the
  signature/date are intentionally NOT in this fill map — they are identity /
  wet-ink items the filer completes themselves (conservative, like the 1040 SSN).
- Box H (accounting method) is a 3-way mutually exclusive group sharing base name
  ``c1_6`` (on-states /1 /2 /3 = Cash / Accrual / Other). The 32c account type
  (``c1_10``, /1 /2 = Checking / Savings) and the "may the IRS discuss" question
  (``c1_11``, /1 /2 = Yes / No) are likewise small radio groups. Set exactly one
  of a group; leave the rest /Off.
- The box-G return-type flags (initial/final/name-change/address-change/amended),
  box J, and box K are INDEPENDENT booleans (each its own /Btn, on-state /1).
- Conservative blank-fill: a line/box with no input value is simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f1065.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1065_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
# The header name/address block lives under a nested read-order subform.
_HDR = f"{_P1}.HeaderAddress_ReadOrder[0].CalendarName_ReadOrder[0]"
# Boxes A–C (left of the header) live under their own subform.
_ABC = f"{_P1}.LinesA-C[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F1065_FIELDS: dict[str, str] = {
    # --- Header identity (page-1 top block) ---
    # Partnership name + mailing address. EIN (f1_14) is intentionally omitted —
    # it is left blank for the filer (see module docstring).
    "partnership_name":             f"{_HDR}.f1_04[0]",
    "address_number_street":        f"{_HDR}.f1_05[0]",
    "address_room_suite":           f"{_HDR}.f1_06[0]",
    "city":                         f"{_HDR}.f1_07[0]",
    "state":                        f"{_HDR}.f1_08[0]",
    "country":                      f"{_HDR}.f1_09[0]",
    "zip_code":                     f"{_HDR}.f1_10[0]",
    # Boxes A / B / C (left column).
    "principal_business_activity":  f"{_ABC}.f1_11[0]",   # A
    "principal_product_service":    f"{_ABC}.f1_12[0]",   # B
    "business_code_number":         f"{_ABC}.f1_13[0]",   # C
    # Boxes E / F (right column).
    "date_business_started":        f"{_P1}.f1_15[0]",    # E
    "total_assets":                 f"{_P1}.f1_16[0]",    # F (dollar amount)
    # Box H — "Other (specify)" accounting-method free text (the checkbox that
    # selects "Other" is in F1065_ACCOUNTING_METHOD_FIELDS).
    "accounting_method_other_specify": f"{_P1}.f1_17[0]",
    # Box I — number of Schedules K-1 attached.
    "num_schedules_k1":             f"{_P1}.f1_18[0]",

    # --- Income (lines 1a–8) ---
    "gross_receipts_1a":            f"{_P1}.f1_19[0]",    # Line 1a
    "returns_allowances_1b":        f"{_P1}.f1_20[0]",    # Line 1b
    "balance_1c":                   f"{_P1}.f1_21[0]",    # Line 1c (1a − 1b)
    "cogs_2":                       f"{_P1}.f1_22[0]",    # Line 2 — cost of goods sold (Form 1125-A)
    "gross_profit_3":               f"{_P1}.f1_23[0]",    # Line 3 — gross profit (1c − 2)
    "ordinary_income_other_p_e_t_4": f"{_P1}.f1_24[0]",   # Line 4 — income from other partnerships/estates/trusts
    "net_farm_profit_5":            f"{_P1}.f1_25[0]",    # Line 5 — net farm profit (loss) (Sch F)
    "net_gain_4797_6":              f"{_P1}.f1_26[0]",    # Line 6 — net gain (loss) from Form 4797
    "other_income_7":               f"{_P1}.f1_27[0]",    # Line 7 — other income (loss)
    "total_income_8":               f"{_P1}.f1_28[0]",    # Line 8 — total income (loss), lines 3–7

    # --- Deductions (lines 9–22) ---
    "salaries_wages_9":             f"{_P1}.f1_29[0]",    # Line 9 — salaries & wages (not to partners)
    "guaranteed_payments_10":       f"{_P1}.f1_30[0]",    # Line 10 — guaranteed payments to partners
    "repairs_maintenance_11":       f"{_P1}.f1_31[0]",    # Line 11 — repairs and maintenance
    "bad_debts_12":                 f"{_P1}.f1_32[0]",    # Line 12 — bad debts
    "rent_13":                      f"{_P1}.f1_33[0]",    # Line 13 — rent
    "taxes_licenses_14":            f"{_P1}.f1_34[0]",    # Line 14 — taxes and licenses
    "interest_15":                  f"{_P1}.f1_35[0]",    # Line 15 — interest
    "depreciation_16a":             f"{_P1}.f1_36[0]",    # Line 16a — depreciation (Form 4562)
    "depreciation_1125a_16b":       f"{_P1}.f1_37[0]",    # Line 16b — less depreciation on Form 1125-A
    "depreciation_balance_16c":     f"{_P1}.f1_38[0]",    # Line 16c — balance (16a − 16b)
    "depletion_17":                 f"{_P1}.f1_39[0]",    # Line 17 — depletion
    "retirement_plans_18":          f"{_P1}.f1_40[0]",    # Line 18 — retirement plans, etc.
    "employee_benefit_programs_19": f"{_P1}.f1_41[0]",    # Line 19 — employee benefit programs
    "energy_efficient_bldg_20":     f"{_P1}.f1_42[0]",    # Line 20 — energy efficient commercial bldgs (Form 7205)
    "other_deductions_21":          f"{_P1}.f1_43[0]",    # Line 21 — other deductions
    "total_deductions_22":          f"{_P1}.f1_44[0]",    # Line 22 — total deductions, lines 9–21

    # --- Tax and Payment (lines 23–32) ---
    "ordinary_business_income_23":  f"{_P1}.f1_45[0]",    # Line 23 — ordinary business income (loss) (8 − 22)
    "interest_lookback_completed_24": f"{_P1}.f1_46[0]",  # Line 24 — look-back, completed long-term contracts
    "interest_lookback_forecast_25": f"{_P1}.f1_47[0]",   # Line 25 — look-back, income forecast method
    "bba_aar_imputed_underpayment_26": f"{_P1}.f1_48[0]", # Line 26 — BBA AAR imputed underpayment
    "other_taxes_27":               f"{_P1}.f1_49[0]",    # Line 27 — other taxes
    "total_balance_due_28":         f"{_P1}.f1_50[0]",    # Line 28 — total balance due (24–27)
    "elective_payment_election_29": f"{_P1}.f1_51[0]",    # Line 29 — elective payment election (Form 3800)
    "payment_30":                   f"{_P1}.f1_52[0]",    # Line 30 — payment
    "amount_owed_31":               f"{_P1}.f1_53[0]",    # Line 31 — amount owed
    "overpayment_32a":              f"{_P1}.f1_54[0]",    # Line 32a — overpayment
    "routing_number_32b":           f"{_P1}.RoutingNo[0].f1_55[0]",   # Line 32b — routing number
    "account_number_32d":           f"{_P1}.AccountNo[0].f1_56[0]",   # Line 32d — account number
}

# ---------------------------------------------------------------------------
# Box G — return-type flags. INDEPENDENT checkboxes (each its own /Btn,
# on-state /1). Box J and box K are likewise independent. Logical key ->
# (AcroForm field name, on-state). A truthy input value sets the box.
# ---------------------------------------------------------------------------
F1065_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # G (1)–(5)
    "initial_return":         (f"{_P1}.c1_1[0]", "1"),
    "final_return":           (f"{_P1}.c1_2[0]", "1"),
    "name_change":            (f"{_P1}.c1_3[0]", "1"),
    "address_change":         (f"{_P1}.c1_4[0]", "1"),
    "amended_return":         (f"{_P1}.c1_5[0]", "1"),
    # J — Schedules C and M-3 attached
    "schedules_c_m3_attached": (f"{_P1}.c1_7[0]", "1"),
    # K (1) / (2)
    "aggregated_activities_465": (f"{_P1}.c1_8[0]", "1"),
    "grouped_activities_469":    (f"{_P1}.c1_9[0]", "1"),
}

# ---------------------------------------------------------------------------
# Box H — accounting method. Mutually exclusive group sharing base name c1_6
# (on-states /1 /2 /3). Set exactly one; leave the others /Off.
# ---------------------------------------------------------------------------
F1065_ACCOUNTING_METHOD_FIELDS: dict[str, tuple[str, str]] = {
    "cash":    (f"{_P1}.c1_6[0]", "1"),
    "accrual": (f"{_P1}.c1_6[1]", "2"),
    "other":   (f"{_P1}.c1_6[2]", "3"),
}

# ---------------------------------------------------------------------------
# "May the IRS discuss this return with the preparer shown below?" — Yes / No
# (base name c1_11, on-states /1 /2).
# ---------------------------------------------------------------------------
F1065_IRS_DISCUSS_FIELDS: dict[str, tuple[str, str]] = {
    "yes": (f"{_P1}.c1_11[0]", "1"),
    "no":  (f"{_P1}.c1_11[1]", "2"),
}

# ---------------------------------------------------------------------------
# Line 32c — direct-deposit account type: Checking / Savings (base name c1_10,
# on-states /1 /2).
# ---------------------------------------------------------------------------
F1065_ACCOUNT_TYPE_FIELDS: dict[str, tuple[str, str]] = {
    "checking": (f"{_P1}.c1_10[0]", "1"),
    "savings":  (f"{_P1}.c1_10[1]", "2"),
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 1065 fields.
VALID_F1065_FIELDS: frozenset[str] = frozenset(
    list(F1065_FIELDS.values())
    + [v[0] for v in F1065_CHECKBOX_FIELDS.values()]
    + [v[0] for v in F1065_ACCOUNTING_METHOD_FIELDS.values()]
    + [v[0] for v in F1065_IRS_DISCUSS_FIELDS.values()]
    + [v[0] for v in F1065_ACCOUNT_TYPE_FIELDS.values()]
)
