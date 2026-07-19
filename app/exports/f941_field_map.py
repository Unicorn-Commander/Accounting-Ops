"""IRS Form 941 (Employer's QUARTERLY Federal Tax Return) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f941.pdf``, **Rev. March 2026**, "Created 4/1/25") with
``pypdf`` and pinning every terminal widget's /Rect against the printed line
labels (a positional audit — the IRS XFA tooltips are empty on this form). Each
Part-1 line label's baseline y-coordinate was matched to its field row's box
y-coordinate, and money-line dollar/cent boxes were separated by their x-column.

Form 941 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the
W-4P / 1040), so it MUST be filled via ``form_prefill._render_acroform_pdf`` (the
XFA-safe /Fields-tree walker that bakes values in as a flattened, visible
overlay), NOT the older page-based ``_fill_pdf_fields``.

Pattern mirrors ``w4p_field_map.py`` / ``f1040_field_map.py``: a ``dict[str, str]``
of logical key -> the fully-qualified AcroForm field name.

Money-line layout
-----------------
Every dollar amount on Form 941 is split into TWO adjacent boxes: a whole-dollars
box and a 2-digit cents box (the printed "." sits between them). So each money
logical key has an entry in BOTH ``FORM_941_FIELDS`` (the dollars box) and
``FORM_941_CENTS_FIELDS`` (the cents box). The renderer places the whole-dollar
portion of a caller-supplied amount in the first and the 2-digit cents in the
second — pure formatting of a provided figure, never a computation. A count
(line 1) and the routing/account/identity fields are single plain-text boxes and
appear only in ``FORM_941_FIELDS``.

EIN / signature policy
----------------------
The Employer Identification Number (EIN appears in the entity area and atop pages
2 & 3 and on the 941-V voucher) and every wet-ink signature/signature-date box are
intentionally NOT mapped here — they are identity / wet-ink items the filer must
enter and sign themselves. The renderer therefore never writes them.

Lines covered: the full Part 1 tax computation (lines 1-15e), Part 2 deposit
schedule + monthly liability (line 16), Part 3 business status (lines 17-18),
Part 4 third-party designee, the Part 5 print-name/title/phone + Paid Preparer
block, and the 941-V payment voucher. This is the honest "fill what the
employer / their payroll records give us onto the official form" surface — it
does not compute the return.

CPA/EA verification note
------------------------
These names match the Rev. 3-2026 f941.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f941_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Qualified-name prefixes (verified against the pypdf /Fields-tree dump).
# ---------------------------------------------------------------------------
_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"
_P3 = "topmostSubform[0].Page3[0]"
_ENT = f"{_P1}.Header[0].EntityArea[0]"
_RFQ = f"{_P1}.Header[0].ReportForQuarter[0]"
_AGG = f"{_P1}.Header[0].AggregateReturn[0]"

# ---------------------------------------------------------------------------
# Plain-text + whole-dollar fields: logical key -> AcroForm field name.
# For money lines this is the WHOLE-DOLLARS box (cents box in FORM_941_CENTS_FIELDS).
# ---------------------------------------------------------------------------
FORM_941_FIELDS: dict[str, str] = {
    # --- Entity area (page 1 header) — EIN intentionally omitted (filer enters) ---
    "trade_name":      f"{_ENT}.f1_4[0]",   # Trade name (if any)
    "address_street":  f"{_ENT}.f1_5[0]",   # Number / Street / Suite (single box)
    "city":            f"{_ENT}.f1_6[0]",
    "state":           f"{_ENT}.f1_7[0]",
    "zip":             f"{_ENT}.f1_8[0]",
    "foreign_country":  f"{_ENT}.f1_9[0]",
    "foreign_province": f"{_ENT}.f1_10[0]",
    "foreign_postal":   f"{_ENT}.f1_11[0]",

    # --- Part 1 (page 1) ---
    "line1_num_employees":          f"{_P1}.f1_12[0]",   # Line 1 — count (no cents)
    "line2_wages":                  f"{_P1}.f1_13[0]",   # Line 2 — wages, tips, other comp
    "line3_fed_income_tax_withheld": f"{_P1}.f1_15[0]",  # Line 3 — federal income tax withheld
    "line5a_taxable_ss_wages":      f"{_P1}.f1_17[0]",   # Line 5a Col 1 — taxable SS wages
    "line5a_ss_tax":                f"{_P1}.f1_19[0]",   # Line 5a Col 2 — (wages x 0.124)
    "line5b_taxable_ss_tips":       f"{_P1}.f1_21[0]",   # Line 5b Col 1 — taxable SS tips
    "line5b_ss_tips_tax":           f"{_P1}.f1_23[0]",   # Line 5b Col 2 — (tips x 0.124)
    "line5c_medicare_wages_tips":   f"{_P1}.f1_25[0]",   # Line 5c Col 1 — Medicare wages & tips
    "line5c_medicare_tax":          f"{_P1}.f1_27[0]",   # Line 5c Col 2 — (x 0.029)
    "line5d_addl_medicare_wages":   f"{_P1}.f1_29[0]",   # Line 5d Col 1 — Add'l Medicare wages
    "line5d_addl_medicare_tax":     f"{_P1}.f1_31[0]",   # Line 5d Col 2 — (x 0.009)
    "line5e_total_ss_medicare":     f"{_P1}.f1_33[0]",   # Line 5e — total SS & Medicare taxes
    "line5f_section_3121q":         f"{_P1}.f1_35[0]",   # Line 5f — Section 3121(q) Notice
    "line6_total_before_adjustments": f"{_P1}.f1_37[0]", # Line 6 — total taxes before adj
    "line7_fractions_of_cents":     f"{_P1}.f1_39[0]",   # Line 7 — fractions-of-cents adj
    "line8_sick_pay":               f"{_P1}.f1_41[0]",   # Line 8 — sick-pay adjustment
    "line9_tips_group_life":        f"{_P1}.f1_43[0]",   # Line 9 — tips & group-term life adj
    "line10_total_after_adjustments": f"{_P1}.f1_45[0]", # Line 10 — total taxes after adj
    "line11_small_biz_credit":      f"{_P1}.f1_47[0]",   # Line 11 — small-business research credit
    "line12_total_after_credits":   f"{_P1}.f1_49[0]",   # Line 12 — total after adj & credits
    "line13_total_deposits":        f"{_P1}.f1_51[0]",   # Line 13 — total deposits this quarter
    "line14_balance_due":           f"{_P1}.f1_53[0]",   # Line 14 — balance due
    "line15a_overpayment":          f"{_P1}.f1_55[0]",   # Line 15a — overpayment
    "line15c_routing_number":       f"{_P1}.RoutingNo[0].f1_57[0]",   # Line 15c — routing # (text)
    "line15e_account_number":       f"{_P1}.AccountNo[0].f1_58[0]",   # Line 15e — account # (text)

    # --- Part 2 (page 2) — monthly schedule depositor liability ---
    "line16_month1_liability":  f"{_P2}.f2_1[0]",   # Month 1
    "line16_month2_liability":  f"{_P2}.f2_3[0]",   # Month 2
    "line16_month3_liability":  f"{_P2}.f2_5[0]",   # Month 3
    "line16_total_liability":   f"{_P2}.f2_7[0]",   # Total liability for quarter (= line 12)

    # --- Part 3 (page 2) — business status ---
    "line17_final_date":  f"{_P2}.f2_9[0]",   # Line 17 — final date wages paid (if closed)

    # --- Part 4 (page 2) — third-party designee ---
    "designee_name":   f"{_P2}.f2_10[0]",
    "designee_phone":  f"{_P2}.f2_11[0]",
    "designee_pin":    f"{_P2}.f2_12[0]",   # 5-digit PIN

    # --- Part 5 (page 2) — sign-here block (signature itself is wet-ink/blank) ---
    "sign_print_name":  f"{_P2}.f2_13[0]",   # "Print your name here"
    "sign_print_title": f"{_P2}.f2_14[0]",   # "Print your title here"
    "sign_best_phone":  f"{_P2}.f2_15[0]",   # "Best daytime phone"

    # --- Paid Preparer Use Only (page 2) — signature/date/EIN are blank ---
    "preparer_name":          f"{_P2}.f2_16[0]",
    "preparer_ptin":          f"{_P2}.f2_17[0]",
    "preparer_firm_name":     f"{_P2}.f2_18[0]",   # f2_19 (firm EIN) left blank by policy
    "preparer_firm_address":  f"{_P2}.f2_20[0]",
    "preparer_firm_phone":    f"{_P2}.f2_21[0]",
    "preparer_firm_city":     f"{_P2}.f2_22[0]",
    "preparer_firm_state":    f"{_P2}.f2_23[0]",
    "preparer_firm_zip":      f"{_P2}.f2_24[0]",

    # --- 941-V payment voucher (page 3) — EIN box left blank by policy ---
    "voucher_amount_paid":     f"{_P3}.f4_2[0]",   # Box 2 — amount of payment (dollars)
    "voucher_address":         f"{_P3}.f4_5[0]",   # Box 4 — address
    "voucher_city_state_zip":  f"{_P3}.f4_6[0]",   # Box 4 — city, state, ZIP
}

# ---------------------------------------------------------------------------
# Cents boxes: money logical key -> the 2-digit cents AcroForm field name. Every
# key here is also in FORM_941_FIELDS (the dollars box). The renderer writes the
# fractional part of a supplied amount here; if the amount has no cents it stays
# blank (conservative).
# ---------------------------------------------------------------------------
FORM_941_CENTS_FIELDS: dict[str, str] = {
    "line2_wages":                  f"{_P1}.f1_14[0]",
    "line3_fed_income_tax_withheld": f"{_P1}.f1_16[0]",
    "line5a_taxable_ss_wages":      f"{_P1}.f1_18[0]",
    "line5a_ss_tax":                f"{_P1}.f1_20[0]",
    "line5b_taxable_ss_tips":       f"{_P1}.f1_22[0]",
    "line5b_ss_tips_tax":           f"{_P1}.f1_24[0]",
    "line5c_medicare_wages_tips":   f"{_P1}.f1_26[0]",
    "line5c_medicare_tax":          f"{_P1}.f1_28[0]",
    "line5d_addl_medicare_wages":   f"{_P1}.f1_30[0]",
    "line5d_addl_medicare_tax":     f"{_P1}.f1_32[0]",
    "line5e_total_ss_medicare":     f"{_P1}.f1_34[0]",
    "line5f_section_3121q":         f"{_P1}.f1_36[0]",
    "line6_total_before_adjustments": f"{_P1}.f1_38[0]",
    "line7_fractions_of_cents":     f"{_P1}.f1_40[0]",
    "line8_sick_pay":               f"{_P1}.f1_42[0]",
    "line9_tips_group_life":        f"{_P1}.f1_44[0]",
    "line10_total_after_adjustments": f"{_P1}.f1_46[0]",
    "line11_small_biz_credit":      f"{_P1}.f1_48[0]",
    "line12_total_after_credits":   f"{_P1}.f1_50[0]",
    "line13_total_deposits":        f"{_P1}.f1_52[0]",
    "line14_balance_due":           f"{_P1}.f1_54[0]",
    "line15a_overpayment":          f"{_P1}.f1_56[0]",
    "line16_month1_liability":      f"{_P2}.f2_2[0]",
    "line16_month2_liability":      f"{_P2}.f2_4[0]",
    "line16_month3_liability":      f"{_P2}.f2_6[0]",
    "line16_total_liability":       f"{_P2}.f2_8[0]",
    "voucher_amount_paid":          f"{_P3}.f4_3[0]",
}

# ---------------------------------------------------------------------------
# Employer name — written to ALL three name boxes (page 1 entity area, page 2
# header, 941-V voucher business name) from the single logical key "name".
# ---------------------------------------------------------------------------
FORM_941_NAME_FIELDS: list[str] = [
    f"{_ENT}.f1_3[0]",                      # Page 1 — Name (not your trade name)
    f"{_P2}.Name_ReadOrder[0].f1_3[0]",     # Page 2 — Name (not your trade name)
    f"{_P3}.f1_3[0]",                       # 941-V — business name
]

# ---------------------------------------------------------------------------
# Quarter — written to BOTH the page-1 header "Report for this Quarter" checkbox
# AND the 941-V "Tax Period" circle, from the single logical key "quarter"
# (value "1".."4"). Each is a separate /Btn with its own on-state.
# ---------------------------------------------------------------------------
FORM_941_QUARTER_FIELDS: dict[str, list[tuple[str, str]]] = {
    "1": [(f"{_RFQ}.c1_1[0]", "1"), (f"{_P3}.Line3_ReadOrder[0].c4_1[0]", "1")],
    "2": [(f"{_RFQ}.c1_1[1]", "2"), (f"{_P3}.Line3_ReadOrder[0].c4_1[1]", "2")],
    "3": [(f"{_RFQ}.c1_1[2]", "3"), (f"{_P3}.Line3_ReadOrder[0].c4_1[2]", "3")],
    "4": [(f"{_RFQ}.c1_1[3]", "4"), (f"{_P3}.Line3_ReadOrder[0].c4_1[3]", "4")],
}

# ---------------------------------------------------------------------------
# Single (boolean) checkboxes: logical key -> (field name, on-state). The
# renderer sets these when the input value is truthy ("true"/"1"/"yes"/"x").
# ---------------------------------------------------------------------------
FORM_941_SINGLE_CHECKBOXES: dict[str, tuple[str, str]] = {
    # Line 4 — no wages subject to SS/Medicare; check and go to line 6.
    "line4_no_wages_subject":  (f"{_P1}.c1_3[0]", "1"),
    # Line 17 — business closed / stopped paying wages.
    "line17_business_closed":  (f"{_P2}.c2_2[0]", "1"),
    # Line 18 — seasonal employer.
    "line18_seasonal":         (f"{_P2}.c2_3[0]", "1"),
    # Paid preparer — "Check if you're self-employed".
    "preparer_self_employed":  (f"{_P2}.c2_5[0]", "1"),
}

# ---------------------------------------------------------------------------
# Mutually-exclusive choice checkboxes: logical key -> {choice: (field, on)}.
# Exactly one choice is set; the others stay /Off (conservative).
# ---------------------------------------------------------------------------
# Line 15b — what to do with an overpayment.
FORM_941_LINE15B_FIELDS: dict[str, tuple[str, str]] = {
    "apply_to_next_return": (f"{_P1}.c1_4[0]", "1"),
    "send_refund":          (f"{_P1}.c1_4[1]", "2"),
}
# Line 15d — bank account type for the refund.
FORM_941_LINE15D_ACCOUNT_TYPE: dict[str, tuple[str, str]] = {
    "checking": (f"{_P1}.c1_5[0]", "1"),
    "savings":  (f"{_P1}.c1_5[1]", "2"),
}
# Line 16 — deposit schedule (Part 2).
FORM_941_DEPOSIT_SCHEDULE_FIELDS: dict[str, tuple[str, str]] = {
    "line12_under_2500": (f"{_P2}.c2_1[0]", "1"),
    "monthly":           (f"{_P2}.c2_1[1]", "2"),
    "semiweekly":        (f"{_P2}.c2_1[2]", "3"),
}
# Part 4 — third-party designee yes / no.
FORM_941_DESIGNEE_FIELDS: dict[str, tuple[str, str]] = {
    "yes": (f"{_P2}.c2_4[0]", "1"),
    "no":  (f"{_P2}.c2_4[1]", "2"),
}
# Aggregate-return filers only — type of filer.
FORM_941_AGGREGATE_FILER_FIELDS: dict[str, tuple[str, str]] = {
    "section_3504_agent": (f"{_AGG}.c1_2[0]", "1"),
    "cpeo":               (f"{_AGG}.c1_2[1]", "2"),
    "other_third_party":  (f"{_AGG}.c1_2[2]", "3"),
}

# ---------------------------------------------------------------------------
# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 941 fields.
# ---------------------------------------------------------------------------
def _all_choice_fields() -> list[str]:
    out: list[str] = []
    for m in (
        FORM_941_LINE15B_FIELDS,
        FORM_941_LINE15D_ACCOUNT_TYPE,
        FORM_941_DEPOSIT_SCHEDULE_FIELDS,
        FORM_941_DESIGNEE_FIELDS,
        FORM_941_AGGREGATE_FILER_FIELDS,
    ):
        out += [v[0] for v in m.values()]
    out += [v[0] for v in FORM_941_SINGLE_CHECKBOXES.values()]
    for targets in FORM_941_QUARTER_FIELDS.values():
        out += [t[0] for t in targets]
    return out


VALID_941_FIELDS: frozenset[str] = frozenset(
    list(FORM_941_FIELDS.values())
    + list(FORM_941_CENTS_FIELDS.values())
    + list(FORM_941_NAME_FIELDS)
    + _all_choice_fields()
)
