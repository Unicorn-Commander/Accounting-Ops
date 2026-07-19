"""IRS Form 1040 (U.S. Individual Income Tax Return) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1040.pdf``, **Tax Year 2025** revision, "Created 9/5/25")
with ``pypdf`` (``PdfReader().get_fields()``) and cross-referencing every
terminal field's widget /Rect against the printed line labels (a positional
audit — the IRS XFA tooltips are empty on this form). Form 1040 is a modern
XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the W-4P), so it MUST
be filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe walker), not
the older ``_fill_pdf_fields`` page-based filler.

Pattern mirrors ``w4p_field_map.py`` / ``form_1120_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.

This map covers identity + filing status + the common income / withholding /
tax / refund-or-owe lines (NOT every line on the form). It is the honest
"fill what the taxpayer/their docs give us onto the official form" surface for
the professional-substitute renderer — it does not compute a return.

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas). The
  IRS AcroForm boxes are plain-text, not numeric. Negative amounts use the
  form's accounting convention; the filler writes the caller's raw string.
- The IRS ``f{page}_{NN}`` numbering increments sequentially in document/tab
  order, so field numbers do NOT map 1:1 to line numbers — every name below was
  pinned positionally to its printed line.
- Income "b / taxable amount" columns: lines 2b & 3b sit in the far-right
  column (x=504); lines 4b/5b are the indented "Taxable amount" box (x=439);
  line 6b is the far-right box (x=504).
- Filing status is FIVE independent checkboxes in two on-screen columns that
  share base names but differ by parent path and on-state (left column
  ``Checkbox_ReadOrder[0].c1_8[*]`` with on-states /1 /2 /3 = Single / MFJ /
  MFS; right column ``c1_8[*]`` with on-states /4 /5 = HoH / QSS). Set exactly
  one; leave the others /Off (conservative).
- Conservative blank-fill: a line with no input value is simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f1040.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1040_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F1040_FIELDS: dict[str, str] = {
    # --- Identity (page 1 header) ---
    # Taxpayer first name + middle initial / last name. SSN is intentionally
    # NOT filled by the renderer (left blank for the taxpayer to enter).
    "first_name_mi":        f"{_P1}.f1_01[0]",
    "last_name":            f"{_P1}.f1_02[0]",
    # Spouse first name + middle initial / last name (joint returns).
    "spouse_first_name_mi": f"{_P1}.f1_04[0]",
    "spouse_last_name":     f"{_P1}.f1_05[0]",
    # Home address (number & street), apartment number, then city/state/ZIP.
    "home_address":         f"{_P1}.Address_ReadOrder[0].f1_20[0]",
    "apartment_no":         f"{_P1}.Address_ReadOrder[0].f1_21[0]",
    "city_town":            f"{_P1}.Address_ReadOrder[0].f1_22[0]",
    "state":                f"{_P1}.Address_ReadOrder[0].f1_23[0]",
    "zip_code":             f"{_P1}.Address_ReadOrder[0].f1_24[0]",
    # Foreign address (optional).
    "foreign_country":      f"{_P1}.Address_ReadOrder[0].f1_25[0]",
    "foreign_province":     f"{_P1}.Address_ReadOrder[0].f1_26[0]",
    "foreign_postal_code":  f"{_P1}.Address_ReadOrder[0].f1_27[0]",

    # --- Income (page 1) ---
    "wages_1a":             f"{_P1}.f1_47[0]",   # Line 1a — wages (W-2 box 1)
    "household_wages_1b":   f"{_P1}.f1_48[0]",   # Line 1b — household employee wages
    "tip_income_1c":        f"{_P1}.f1_49[0]",   # Line 1c — unreported tip income
    "medicaid_waiver_1d":   f"{_P1}.f1_50[0]",   # Line 1d — Medicaid waiver payments
    "dependent_care_1e":    f"{_P1}.f1_51[0]",   # Line 1e — taxable dependent care
    "adoption_benefits_1f": f"{_P1}.f1_52[0]",   # Line 1f — employer adoption benefits
    "wages_8919_1g":        f"{_P1}.f1_55[0]",   # Line 1g — Form 8919 wages
    "other_earned_1h":      f"{_P1}.f1_57[0]",   # Line 1h — other earned income (1i box)
    "total_wages_1z":       f"{_P1}.f1_59[0]",   # Line 1z — add lines 1a–1h
    "taxable_interest_2b":  f"{_P1}.f1_61[0]",   # Line 2b — taxable interest
    "ordinary_div_3b":      f"{_P1}.f1_63[0]",   # Line 3b — ordinary dividends
    "ira_distrib_4b":       f"{_P1}.f1_64[0]",   # Line 4b — IRA distributions (taxable)
    "pensions_5b":          f"{_P1}.f1_67[0]",   # Line 5b — pensions/annuities (taxable)
    "social_security_6b":   f"{_P1}.f1_69[0]",   # Line 6b — social security (taxable)
    "capital_gain_7":       f"{_P1}.f1_70[0]",   # Line 7 — capital gain or (loss)
    "additional_income_8":  f"{_P1}.f1_72[0]",   # Line 8 — additional income (Sch 1)
    "total_income_9":       f"{_P1}.f1_74[0]",   # Line 9 — total income
    "adjustments_10":       f"{_P1}.f1_75[0]",   # Line 10 — adjustments to income
    "agi_11":               f"{_P2}.f2_01[0]",   # Line 11 — adjusted gross income (11b on p2)

    # --- Tax and credits (page 2) ---
    "std_or_itemized_12":   f"{_P2}.f2_03[0]",   # Line 12 — standard or itemized deduction
    "qbi_deduction_13a":    f"{_P2}.f2_04[0]",   # Line 13a — QBI deduction
    "additional_deduct_13b": f"{_P2}.f2_05[0]",  # Line 13b — additional deductions (Sch 1-A)
    "total_deductions_14":  f"{_P2}.f2_06[0]",   # Line 14 — add 12e, 13a, 13b
    "taxable_income_15":    f"{_P2}.f2_08[0]",   # Line 15 — taxable income
    "tax_16":               f"{_P2}.f2_09[0]",   # Line 16 — tax
    "sch2_line3_17":        f"{_P2}.f2_10[0]",   # Line 17 — amount from Schedule 2, line 3
    "add_16_17_18":         f"{_P2}.f2_11[0]",   # Line 18 — add lines 16 and 17
    "ctc_19":               f"{_P2}.f2_12[0]",   # Line 19 — child tax credit / ODC
    "sch3_line8_20":        f"{_P2}.f2_13[0]",   # Line 20 — amount from Schedule 3, line 8
    "add_19_20_21":         f"{_P2}.f2_14[0]",   # Line 21 — add lines 19 and 20
    "subtract_21_22":       f"{_P2}.f2_15[0]",   # Line 22 — subtract line 21 from line 18
    "other_taxes_23":       f"{_P2}.f2_16[0]",   # Line 23 — other taxes (Schedule 2, line 21)
    # NOTE: Line 24 (total tax) has NO fillable AcroForm box on the TY2025
    # f1040.pdf (verified — the form omits a box for this computed line; the
    # filer's software/calculation writes it). It is therefore intentionally
    # absent from this map. "subtract_21_22" (line 22) and the Schedule-2 line
    # 23 carry-in are the nearest fillable tax-total figures.

    # --- Payments (page 2) ---
    "federal_withholding_25a": f"{_P2}.f2_17[0]",  # Line 25a — withholding from W-2
    "federal_withholding_25b": f"{_P2}.f2_18[0]",  # Line 25b — withholding from 1099
    "other_withholding_25c":   f"{_P2}.f2_19[0]",  # Line 25c — other-forms withholding
    "total_withholding_25d":   f"{_P2}.f2_20[0]",  # Line 25d — add 25a–25c
    "estimated_payments_26":   f"{_P2}.f2_21[0]",  # Line 26 — estimated tax payments
    "eic_27a":                 f"{_P2}.f2_23[0]",  # Line 27a — earned income credit
    "actc_28":                 f"{_P2}.f2_24[0]",  # Line 28 — additional child tax credit
    "total_payments_33":       f"{_P2}.f2_30[0]",  # Line 33 — total payments

    # --- Refund / amount you owe (page 2) ---
    "overpayment_34":          f"{_P2}.f2_31[0]",  # Line 34 — overpayment (refund)
    "refund_35a":              f"{_P2}.f2_34[0]",  # Line 35a — amount refunded to you
    "routing_number_35b":      f"{_P2}.RoutingNo[0].f2_32[0]",   # Line 35b — routing number
    "account_number_35d":      f"{_P2}.AccountNo[0].f2_33[0]",   # Line 35d — account number
    "applied_to_2026_36":      f"{_P2}.f2_36[0]",  # Line 36 — applied to next year's est. tax
    "amount_you_owe_37":       f"{_P2}.f2_35[0]",  # Line 37 — amount you owe
    "estimated_penalty_38":    f"{_P2}.f2_39[0]",  # Line 38 — estimated tax penalty
}

# ---------------------------------------------------------------------------
# Filing-status checkboxes: logical status -> (AcroForm field name, on-state).
# Exactly one is set; the others remain /Off (conservative). Left on-screen
# column lives under Checkbox_ReadOrder[0] (on-states /1 /2 /3); the right
# column lives directly under Page1[0] (on-states /4 /5). Verified positionally.
# ---------------------------------------------------------------------------
F1040_FILING_STATUS_FIELDS: dict[str, tuple[str, str]] = {
    "single": (f"{_P1}.Checkbox_ReadOrder[0].c1_8[0]", "1"),
    "mfj":    (f"{_P1}.Checkbox_ReadOrder[0].c1_8[1]", "2"),
    "mfs":    (f"{_P1}.Checkbox_ReadOrder[0].c1_8[2]", "3"),
    "hoh":    (f"{_P1}.c1_8[0]", "4"),
    "qss":    (f"{_P1}.c1_8[1]", "5"),
}

# ---------------------------------------------------------------------------
# Digital-asset question (page 1): Yes / No checkboxes. on-states /1 and /2.
# Not part of the dollar lines; exposed so the caller can record the taxpayer's
# answer when present. Conservative: left blank if not provided.
# ---------------------------------------------------------------------------
F1040_DIGITAL_ASSET_FIELDS: dict[str, tuple[str, str]] = {
    "yes": (f"{_P1}.c1_10[0]", "1"),
    "no":  (f"{_P1}.c1_10[1]", "2"),
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 1040 fields.
VALID_F1040_FIELDS: frozenset[str] = frozenset(
    list(F1040_FIELDS.values())
    + [v[0] for v in F1040_FILING_STATUS_FIELDS.values()]
    + [v[0] for v in F1040_DIGITAL_ASSET_FIELDS.values()]
)
