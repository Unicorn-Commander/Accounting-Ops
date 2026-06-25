"""CA Form 540 (California Resident Income Tax Return) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/state_forms/ca_540_2024.pdf``, **Tax Year 2024**, from ftb.ca.gov) with
``pypdf`` (``PdfReader().get_fields()``) and pinning every terminal field's
widget ``/Rect`` to its printed line by cross-referencing the page text layer
(a positional audit). The 2024 Form 540 is a 6-page fillable AcroForm with FLAT
field names (no XFA / no ``topmostSubform`` hierarchy):

  * Dollar/amount and identity lines are ``/Tx`` text fields named ``"540-NNNN"``
    (e.g. ``"540-2025"``). These are filled through
    ``form_prefill._render_acroform_pdf`` (the values are set on ``/V`` and BAKED
    as a flattened, visible overlay so they render in every viewer/printer).
  * Independent checkboxes are ``/Btn`` fields named ``"540-NNNN CB"`` with the
    single on-state ``/Yes``. These also go through ``_render_acroform_pdf``
    (one widget per field → one baked "X").
  * Filing status (and a few other either/or choices) are each a SINGLE ``/Btn``
    RADIO group named ``"540-NNNN RB"`` whose option widgets ALL share the
    parent's qualified name. Routing such a name through the value-overlay would
    therefore stamp an "X" on EVERY option (the W-4P / 1040 forms avoid this
    because their statuses are independent checkbox fields). So radio groups are
    NOT placed in the value map; ``ca_540_fill`` marks the chosen option via a
    per-option coordinate overlay, and this module exposes each option's widget
    ``/Rect`` (+ page index) in ``CA_540_RADIO_FIELDS``.

Conventions
-----------
- Dollar amounts are written as plain strings ("12500" — no $/commas; the CA
  boxes are plain text). The renderer writes the caller's raw string verbatim.
- The taxpayer's / spouse's / dependents' SSN-or-ITIN fields (540-1007,
  540-1012, 540-2005, 540-2009, 540-2013, and the repeated header SSN 540-2002)
  and the wet-ink signature block (Side 6) are INTENTIONALLY NOT in this map —
  they are identity / signature items the taxpayer must enter themselves.
- This map covers identity + filing status + exemptions + the common income /
  tax / credit / payment / refund-or-owe lines a preparer fills. It does NOT
  cover every line (e.g. the 18 individual voluntary-contribution fund boxes are
  omitted; only their total, line 110, is mapped). It is a transcribe-the-given-
  figures-onto-the-official-form surface — it does NOT compute a return.

CPA/EA verification note
------------------------
Names match the TY2024 ``2024-540.pdf`` as distributed on ftb.ca.gov. If a later
revision (or a prior-year ``ca_540_{year}.pdf``) is dropped into
``data/state_forms/``, re-run the dump and re-pin any changed names/rects.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
CA_540_FIELDS: dict[str, str] = {
    # --- Identity / address (Side 1) ---
    "first_name":            "540-1003",
    "middle_initial":        "540-1004",
    "last_name":             "540-1005",
    "suffix":                "540-1006",
    "spouse_first_name":     "540-1008",
    "spouse_middle_initial": "540-1009",
    "spouse_last_name":      "540-1010",
    "spouse_suffix":         "540-1011",
    "additional_info":       "540-1013",   # "Additional information (see instructions)"
    "pba_code":              "540-1014",   # Principal Business Activity code box
    "street_address":        "540-1015",   # number & street or PO box
    "apt_no":                "540-1016",
    "pmb_no":                "540-1017",   # PMB / private mailbox
    "city":                  "540-1018",
    "state":                 "540-1019",
    "zip_code":              "540-1020",
    "foreign_country":       "540-1021",
    "foreign_province":      "540-1022",
    "foreign_postal_code":   "540-1023",
    "dob_taxpayer":          "540-1024",   # Your Date of Birth
    "dob_spouse":            "540-1025",   # Spouse/RDP Date of Birth
    "prior_name_taxpayer":   "540-1026",
    "prior_name_spouse":     "540-1027",

    # --- Exemptions (Side 1, lines 7-9; count box + computed $ box each) ---
    "exemptions_personal_count":  "540-1041",  # line 7 — number (1 or 2)
    "exemptions_personal_amount": "540-1042",  # line 7 — count x $149
    "exemptions_blind_count":     "540-1043",  # line 8 — number
    "exemptions_blind_amount":    "540-1044",  # line 8 — count x $149
    "exemptions_senior_count":    "540-1045",  # line 9 — number
    "exemptions_senior_amount":   "540-1046",  # line 9 — count x $149

    # --- Repeated header name (Sides 2-6). The matching SSN header (540-2002)
    #     is intentionally NOT filled. Setting this draws the name on every
    #     continuation page (one shared field, a widget per page). ---
    "your_name_header":      "540-2001",

    # --- Dependents (Side 2, line 10; SSNs 540-2005/2009/2013 NOT filled) ---
    "dependent1_first_name":   "540-2003",
    "dependent1_last_name":    "540-2004",
    "dependent1_relationship": "540-2006",
    "dependent2_first_name":   "540-2007",
    "dependent2_last_name":    "540-2008",
    "dependent2_relationship": "540-2010",
    "dependent3_first_name":   "540-2011",
    "dependent3_last_name":    "540-2012",
    "dependent3_relationship": "540-2014",
    "line10_dependent_count":  "540-2015",  # total dependent exemptions — number
    "line10_dependent_amount": "540-2016",  # count x $461
    "line11_exemption_amount": "540-2017",  # add line 7 through line 10

    # --- Taxable income (Side 2, lines 12-19) ---
    "line12_state_wages":     "540-2018",  # state wages from W-2 box 16
    "line13_federal_agi":     "540-2019",  # federal AGI (1040 line 11)
    "line14_ca_subtractions": "540-2020",  # Sch CA (540) Part I line 27 col B
    "line15_subtotal":        "540-2021",  # line 13 - line 14
    "line16_ca_additions":    "540-2022",  # Sch CA (540) Part I line 27 col C
    "line17_ca_agi":          "540-2023",  # California adjusted gross income
    "line18_deductions":      "540-2024",  # larger of CA itemized OR standard
    "line19_taxable_income":  "540-2025",  # line 17 - line 18

    # --- Tax (Side 2, lines 31-35) ---
    "line31_tax":                 "540-2030",
    "line32_exemption_credits":   "540-2031",
    "line33_subtract_32_from_31": "540-2032",
    "line34_tax":                 "540-2035",
    "line35_total_tax":           "540-2036",

    # --- Special credits (Side 2, lines 40, 43, 44) ---
    "line40_child_dependent_care_credit": "540-2037",
    "line43_credit_name":   "540-2038",
    "line43_credit_code":   "540-2039",
    "line43_credit_amount": "540-2040",
    "line44_credit_name":   "540-2041",
    "line44_credit_code":   "540-2042",
    "line44_credit_amount": "540-2043",

    # --- Credits / other taxes / payments (Side 3, lines 45-97) ---
    "line45_credits_over_two":  "540-3003",  # > 2 credits (attach Sch P)
    "line46_renters_credit":    "540-3004",
    "line47_total_credits":     "540-3005",
    "line48_tax_less_credits":  "540-3006",
    "line61_amt":               "540-3007",  # Alternative Minimum Tax (Sch P)
    "line62_mental_health_tax": "540-3008",
    "line63_other_taxes":       "540-3009",
    "line64_total_tax":         "540-3010",
    "line71_ca_withholding":    "540-3011",
    "line72_estimated_payments": "540-3012",
    "line73_withholding_592b_593": "540-3013",
    "line75_eitc":              "540-3015",  # Earned Income Tax Credit
    "line76_yctc":              "540-3016",  # Young Child Tax Credit
    "line77_fytc":              "540-3017",  # Foster Youth Tax Credit
    "line78_total_payments":    "540-3018",
    "line91_use_tax":           "540-3019",
    "line92_isr_penalty":       "540-3022",  # Individual Shared Responsibility
    "line93_payments_balance":  "540-3023",
    "line94_use_tax_balance":   "540-3024",
    "line95_payments_after_isr": "540-3025",
    "line96_isr_penalty_balance": "540-3026",
    "line97_overpaid_tax":      "540-3027",

    # --- Overpaid / tax due / contributions total (Side 4, lines 98-110) ---
    "line98_applied_to_2025_est": "540-4003",
    "line99_overpaid_available":  "540-4004",
    "line100_tax_due":            "540-4005",
    "line110_total_contributions": "540-4024",

    # --- Amount you owe / refund / direct deposit (Side 5, lines 111-115) ---
    "line111_amount_you_owe":         "540-5002",
    "line112_interest_late_penalties": "540-5003",
    "line113_underpayment_penalty":   "540-5005",
    "line114_total_amount_due":       "540-5006",
    "line115_refund":                 "540-5007",
    "direct_deposit_1_routing": "540-5009",
    "direct_deposit_1_account": "540-5011",
    "direct_deposit_1_amount":  "540-5008",
    "direct_deposit_2_routing": "540-5013",
    "direct_deposit_2_account": "540-5015",
    "direct_deposit_2_amount":  "540-5012",
}

# ---------------------------------------------------------------------------
# Independent checkboxes: logical key -> (AcroForm field name, on-state).
# Each is a standalone /Btn (one widget) → marked through the value map; the
# renderer writes the on-state and one baked "X" lands on its box. Conservative:
# only checked when the caller passes a truthy value.
# ---------------------------------------------------------------------------
CA_540_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    "amended_return":                 ("540-1001 CB", "Yes"),  # top of Side 1
    "filing_status_differs_federal":  ("540-1035 CB", "Yes"),  # CA != federal status
    "can_be_claimed_dependent":       ("540-1040 CB", "Yes"),  # line 6
    "full_year_health_coverage":      ("540-3021 CB", "Yes"),  # line 92 coverage box
}

# ---------------------------------------------------------------------------
# Radio groups (single /Btn whose option widgets share one qualified name).
# logical key -> { option value -> (page_index, (x0, y0, x1, y1)) }.
# The renderer draws an "X" centered in the chosen option's widget rect (the
# only correct way to mark exactly one option of a shared-name radio through the
# flatten-and-bake pipeline). Rects are in PDF user space (612x792, origin
# bottom-left), copied verbatim from the verified widget dump.
# ---------------------------------------------------------------------------
CA_540_RADIO_FIELDS: dict[str, dict[str, tuple[int, tuple[float, float, float, float]]]] = {
    # Filing status (Side 1, line 1-5) — exactly one.
    "filing_status": {
        "single": (0, (74.7, 261.7, 91.0, 278.4)),
        "mfj":    (0, (74.6, 237.5, 91.1, 254.5)),  # Married/RDP filing jointly
        "mfs":    (0, (74.5, 189.4, 91.0, 206.6)),  # Married/RDP filing separately
        "hoh":    (0, (247.0, 261.4, 263.7, 278.4)),  # Head of household
        "qss":    (0, (247.0, 237.4, 263.9, 254.4)),  # Qualifying surviving spouse/RDP
    },
    # Use Tax — "If line 91 is zero, check if:" (Side 3).
    "use_tax_zero_reason": {
        "no_use_tax_owed":        (2, (182.4, 261.4, 199.1, 278.4)),
        "paid_directly_to_cdtfa": (2, (305.0, 261.4, 321.7, 278.4)),
    },
    # Direct-deposit account types (Side 5).
    "direct_deposit_1_type": {
        "checking": (4, (153.6, 466.6, 170.5, 483.5)),
        "savings":  (4, (153.6, 442.5, 170.5, 459.5)),
    },
    "direct_deposit_2_type": {
        "checking": (4, (153.7, 394.4, 170.4, 411.5)),
        "savings":  (4, (153.7, 370.5, 170.3, 387.5)),
    },
    # Third-party designee — "allow another person to discuss this return?" (Side 6).
    "third_party_designee": {
        "yes": (5, (448.7, 464.8, 465.4, 481.8)),
        "no":  (5, (499.1, 464.8, 515.8, 481.8)),
    },
}

# The radio groups' parent AcroForm field names — documentation + VALID set.
# (These names are NEVER written into the value map; see the module docstring.)
CA_540_RADIO_FIELD_NAMES: dict[str, str] = {
    "filing_status":         "540-1036 RB",
    "use_tax_zero_reason":   "540-3020 RB",
    "direct_deposit_1_type": "540-5010 RB",
    "direct_deposit_2_type": "540-5014 RB",
    "third_party_designee":  "540-6008 RB",
}

# Every AcroForm field name this map knows — used by tests + the render-time
# guard to validate that we only ever touch known Form 540 fields.
VALID_CA_540_FIELDS: frozenset[str] = frozenset(
    list(CA_540_FIELDS.values())
    + [v[0] for v in CA_540_CHECKBOX_FIELDS.values()]
    + list(CA_540_RADIO_FIELD_NAMES.values())
)
