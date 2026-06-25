"""IRS Form 1120-S (U.S. Income Tax Return for an S Corporation) — AcroForm map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1120s.pdf``, **Tax Year 2025** revision, "For calendar year
2025") with ``pypdf`` and cross-referencing every terminal field's widget /Rect
against the printed line labels (a positional audit — the IRS XFA tooltips are
empty on this form). Form 1120-S is a modern XFA-hybrid AcroForm
(``topmostSubform[0]`` field naming, like the W-4P / 1040), so it MUST be filled
via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that
bakes flattened, visible values), NOT the older page-based ``_fill_pdf_fields``.

Pattern mirrors ``f1040_field_map.py`` / ``w4p_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name,
plus checkbox maps of logical key -> ``(field name, on-state)``.

Scope
-----
This map covers **page 1 — the return face**: the identity header, the income
lines (1a–6), the deduction lines (7–22), and the Tax-and-Payments lines
(23a–28b) plus the page-1 checkboxes (C / G / H / J / line 25 / line 28d). It is
the honest "fill what the client / their books give us onto the official form"
surface — it does NOT compute a return and does NOT map the supporting schedules
(Schedule B "Other Information", Schedule K "Shareholders' Pro Rata Share Items",
the per-shareholder Schedule K-1, or Schedule L / M-1 / M-2 on pages 2–5). Those
remain blank for the preparer until mapped.

Left intentionally BLANK by the renderer (for the human to enter / sign)
----------------------------------------------------------------------
The corporation's **EIN** (item D, ``topmostSubform[0].Page1[0].f1_13[0]``), the
**officer signature / title** (``...f1_56[0]``), the "May the IRS discuss this
return with the preparer" authorization (``...c1_11[*]``), and the entire **paid
preparer block** (``...f1_57[0]``–``...f1_62[0]``, ``...c1_12[0]``) are NOT in
the fillable maps below. They are wet-ink / identity / preparer items the human
completes themselves — consistent with the W-4P (SSN-for-human) and 1040
(SSN/signature omitted) renderers.

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas). The
  IRS AcroForm boxes are plain-text, not numeric. Negative amounts use the
  form's accounting convention; the filler writes the caller's raw string.
- The IRS ``f1_NN`` numbering increments in document/tab order, so field numbers
  do NOT map 1:1 to line numbers — every name below was pinned positionally to
  its printed line.
- Conservative blank-fill: a line with no input value is simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f1120s.pdf as distributed on irs.gov. The TY2023+
revisions renumbered the form (line 19 "Energy efficient commercial buildings
deduction", line 20 "Other deductions", line 21 total, line 22 ordinary income,
lines 23–24 tax/payments) and added the direct-deposit-of-refund block on line
28 — verified against this dump. If a later revision (or a prior-year
``f1120s_{year}.pdf``) is dropped into ``data/irs_forms/``, re-run the dump and
re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
# Sub-group prefixes the IRS form uses for the header's read-order regions.
_DN = f"{_P1}.Date_Name_ReadOrder[0]"   # name / address read-order block
_ABC = f"{_P1}.ABC[0]"                   # items A / B / C (left margin) block

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
FORM_1120S_FIELDS: dict[str, str] = {
    # --- Tax year (the form pre-prints "For calendar year 2025"; these are the
    #     fiscal-year "beginning … ending …" boxes, usually blank for calendar
    #     filers) ---
    "tax_year_begin":            f"{_DN}.f1_1[0]",
    "tax_year_end":              f"{_DN}.f1_2[0]",
    "tax_year_end_yy":           f"{_DN}.f1_3[0]",   # trailing "20__" of ending date

    # --- Identity header ---
    "corp_name":                 f"{_DN}.f1_4[0]",     # Name of corporation
    "s_election_date":           f"{_ABC}.f1_11[0]",   # A — S election effective date
    "number_street":             f"{_DN}.f1_5[0]",     # Number, street
    "room_suite":                f"{_DN}.f1_6[0]",     # Room or suite no.
    "business_activity_code":    f"{_ABC}.f1_12[0]",   # B — business activity code (NAICS)
    "date_incorporated":         f"{_P1}.f1_14[0]",    # E — date incorporated
    "city":                      f"{_DN}.f1_7[0]",     # City or town
    "state":                     f"{_DN}.f1_8[0]",     # State or province
    "country":                   f"{_DN}.f1_9[0]",     # Country (foreign address)
    "zip":                       f"{_DN}.f1_10[0]",    # ZIP or foreign postal code
    "total_assets":              f"{_P1}.f1_15[0]",    # F — total assets ($)
    "num_shareholders":          f"{_P1}.f1_16[0]",    # I — number of shareholders

    # --- Income (lines 1–6) ---
    "line_1a_gross_receipts":      f"{_P1}.f1_17[0]",  # 1a — gross receipts or sales
    "line_1b_returns_allowances":  f"{_P1}.f1_18[0]",  # 1b — less returns and allowances
    "line_1c_net_receipts":        f"{_P1}.f1_19[0]",  # 1c — balance (1a − 1b)
    "line_2_cogs":                 f"{_P1}.f1_20[0]",  # 2  — cost of goods sold (1125-A)
    "line_3_gross_profit":         f"{_P1}.f1_21[0]",  # 3  — gross profit (1c − 2)
    "line_4_net_gain_4797":        f"{_P1}.f1_22[0]",  # 4  — net gain (loss) from Form 4797
    "line_5_other_income":         f"{_P1}.f1_23[0]",  # 5  — other income (loss)
    "line_6_total_income":         f"{_P1}.f1_24[0]",  # 6  — total income (loss); add 3–5

    # --- Deductions (lines 7–22) ---
    "line_7_officer_comp":         f"{_P1}.f1_25[0]",  # 7  — compensation of officers (1125-E)
    "line_8_salaries_wages":       f"{_P1}.f1_26[0]",  # 8  — salaries and wages
    "line_9_repairs":              f"{_P1}.f1_27[0]",  # 9  — repairs and maintenance
    "line_10_bad_debts":           f"{_P1}.f1_28[0]",  # 10 — bad debts
    "line_11_rents":               f"{_P1}.f1_29[0]",  # 11 — rents
    "line_12_taxes_licenses":      f"{_P1}.f1_30[0]",  # 12 — taxes and licenses
    "line_13_interest":            f"{_P1}.f1_31[0]",  # 13 — interest
    "line_14_depreciation":        f"{_P1}.f1_32[0]",  # 14 — depreciation (Form 4562)
    "line_15_depletion":           f"{_P1}.f1_33[0]",  # 15 — depletion
    "line_16_advertising":         f"{_P1}.f1_34[0]",  # 16 — advertising
    "line_17_pension_plans":       f"{_P1}.f1_35[0]",  # 17 — pension, profit-sharing plans
    "line_18_employee_benefits":   f"{_P1}.f1_36[0]",  # 18 — employee benefit programs
    "line_19_energy_efficient":    f"{_P1}.f1_37[0]",  # 19 — energy efficient bldgs (7205)
    "line_20_other_deductions":    f"{_P1}.f1_38[0]",  # 20 — other deductions
    "line_21_total_deductions":    f"{_P1}.f1_39[0]",  # 21 — total deductions; add 7–20
    "line_22_ordinary_income":     f"{_P1}.f1_40[0]",  # 22 — ordinary business income (loss)

    # --- Tax and Payments (lines 23–28) ---
    "line_23a_passive_lifo_tax":   f"{_P1}.f1_41[0]",  # 23a — excess net passive / LIFO tax
    "line_23b_tax_sch_d":          f"{_P1}.f1_42[0]",  # 23b — tax from Schedule D
    "line_23c_total_tax":          f"{_P1}.f1_43[0]",  # 23c — add 23a and 23b
    "line_24a_estimated_payments": f"{_P1}.f1_44[0]",  # 24a — est. payments + prior overpmt
    "line_24b_form_7004":          f"{_P1}.f1_45[0]",  # 24b — tax deposited with Form 7004
    "line_24c_fuels_credit":       f"{_P1}.f1_46[0]",  # 24c — fuels credit (Form 4136)
    "line_24d_elective_payment":   f"{_P1}.f1_47[0]",  # 24d — elective payment (Form 3800)
    "line_24z_total_payments":     f"{_P1}.f1_48[0]",  # 24z — add 24a–24d
    "line_25_estimated_tax_penalty": f"{_P1}.f1_49[0]",  # 25 — estimated tax penalty
    "line_26_amount_owed":         f"{_P1}.f1_50[0]",  # 26 — amount owed
    "line_27_overpayment":         f"{_P1}.f1_51[0]",  # 27 — overpayment
    "line_28a_credited_to_est_tax": f"{_P1}.f1_52[0]", # 28a — credited to next year est. tax
    "line_28b_refunded":           f"{_P1}.f1_53[0]",  # 28b — refunded
    "line_28c_routing_number":     f"{_P1}.Routing_Number[0].f1_54[0]",  # 28c — routing no.
    "line_28e_account_number":     f"{_P1}.Account_Number[0].f1_55[0]",  # 28e — account no.
}

# ---------------------------------------------------------------------------
# Simple boolean checkboxes: logical key -> (AcroForm field name, on-state).
# Each is independent (more than one of H may be checked); the renderer sets the
# box only when the logical key is present with a truthy value. on-state has NO
# leading slash (the walker adds it: "1" -> /1).
# ---------------------------------------------------------------------------
FORM_1120S_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    "sch_m3_attached":        (f"{_ABC}.c1_1[0]", "1"),  # C — Schedule M-3 attached
    "final_return":           (f"{_P1}.c1_3[0]", "1"),   # H(1) — final return
    "name_change":            (f"{_P1}.c1_4[0]", "1"),   # H(2) — name change
    "address_change":         (f"{_P1}.c1_5[0]", "1"),   # H(3) — address change
    "amended_return":         (f"{_P1}.c1_6[0]", "1"),   # H(4) — amended return
    "s_election_termination": (f"{_P1}.c1_7[0]", "1"),   # H(5) — S election termination/revocation
    "aggregated_activities":  (f"{_P1}.c1_8[0]", "1"),   # J(1) — aggregated activities (§465)
    "grouped_activities":     (f"{_P1}.c1_8[1]", "1"),   # J(2) — grouped activities (§469)
    "form_2220_attached":     (f"{_P1}.c1_9[0]", "1"),   # line 25 — Form 2220 attached
}

# ---------------------------------------------------------------------------
# Item G — "Is the corporation electing to be an S corporation beginning with
# this tax year?" Yes/No (mutually exclusive). Set via the "electing_s_corp"
# input ("yes" / "no"). on-states /1 (Yes) and /2 (No).
# ---------------------------------------------------------------------------
FORM_1120S_ELECTION_FIELDS: dict[str, tuple[str, str]] = {
    "yes": (f"{_P1}.c1_2[0]", "1"),
    "no":  (f"{_P1}.c1_2[1]", "2"),
}

# ---------------------------------------------------------------------------
# Line 28d — direct-deposit account type Checking/Savings (mutually exclusive).
# Set via the "deposit_type" input ("checking" / "savings"). on-states /1 / /2.
# ---------------------------------------------------------------------------
FORM_1120S_DEPOSIT_TYPE_FIELDS: dict[str, tuple[str, str]] = {
    "checking": (f"{_P1}.c1_10[0]", "1"),
    "savings":  (f"{_P1}.c1_10[1]", "2"),
}

# Every AcroForm field name these maps can write — used by tests + the
# render-time provenance guard to validate that we only ever touch known
# Form 1120-S fields.
VALID_FORM_1120S_FIELDS: frozenset[str] = frozenset(
    list(FORM_1120S_FIELDS.values())
    + [v[0] for v in FORM_1120S_CHECKBOX_FIELDS.values()]
    + [v[0] for v in FORM_1120S_ELECTION_FIELDS.values()]
    + [v[0] for v in FORM_1120S_DEPOSIT_TYPE_FIELDS.values()]
)
