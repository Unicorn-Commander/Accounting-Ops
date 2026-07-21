"""IRS Form 843 (Claim for Refund and Request for Abatement) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f843.pdf``, **Rev. December 2024**) with ``pypdf``
(``PdfReader.get_fields()`` for names/types/on-states) and pinning every
terminal widget's /Rect against the printed captions with ``pymupdf``.
Form 843 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so it
MUST be filled via ``form_prefill._render_acroform_pdf``.

Layout (Rev. 12-2024 — a full redesign vs. the older 8-line revision)
---------------------------------------------------------------------
Page 1:
  - "Check the box below that indicates your reason for filing Form 843":
    an exclusive 17-position group ``c1_1[0..16]`` (on-states /1../17) grouped
    under the printed headings Tax / Penalty / Interest / Other, with an
    "Other (specify)" text line ``f1_1``.
  - Identity block: name ``f1_2``, SSN ``f1_3``, spouse name ``f1_4``, spouse
    SSN ``f1_5``, address ``f1_6``, apt ``f1_7``, city ``f1_8``, state
    ``f1_9``, ZIP ``f1_10``, EIN ``f1_11``, foreign country/province/postal
    ``f1_12..f1_14``, name+address shown on return if different ``f1_15``,
    daytime phone ``f1_16``.
  - Line 1: tax period beginning ``f1_17`` / ending ``f1_18`` (MM/DD/YYYY).
  - Line 2: amount to be refunded or abated ``f1_19``.
  - Line 3: date(s) of payment(s), twelve boxes a-l ``f1_20..f1_31``.
  - Line 4: type of tax — independent checkboxes a-g: Employment ``c1_2``,
    Estate ``c1_3``, Gift ``c1_4``, Excise ``c1_5``, Income ``c1_6``, Fee
    ``c1_7``, Civil penalty ``c1_8`` (on-state /1 each).
Page 2:
  - Line 5: type of return filed — independent checkboxes a-n: 706, 709, 940,
    941, 943, 944, 945, 990-PF, 1040, 1120, 4720, CT-2, BPD Fee, Other
    (``c2_1..c2_14``, on-state /1 each) + "Other (specify)" text ``f2_1``.
  - Line 6: IRC section on which a penalty is based — text ``f2_2``.
  - Line 7: reason for the request — exclusive group ``c2_15[0..3]``
    (on-states /1../4): a IRS errors/delays interest, b erroneous written
    advice, c reasonable cause, d none of the above.
  - Line 8: explanation — big multiline ``ExplainWhy[0].f2_3[0]``.
  - Signature area: taxpayer IP PIN ``f2_4``, spouse IP PIN ``f2_5``.
  - Paid preparer block: name ``f2_6``, self-employed check ``c2_16``, PTIN
    ``f2_7``, firm name ``f2_8``, firm EIN ``f2_9``, firm address ``f2_10``,
    phone ``f2_11``.

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Dollar amounts are written as plain
strings. Conservative blank-fill: a line with no input value is simply not
written.

Dropped legacy logical keys (no plausible target on Rev. 12-2024)
-----------------------------------------------------------------
  - ``city_state_zip`` — the revision splits these into ``city`` / ``state`` /
    ``zip`` (f1_8/f1_9/f1_10).
  - ``ssn_ein`` — split into ``ssn`` (f1_3) and ``ein`` (f1_11).
  - ``tax_type_other`` — line 4 has no "Other" box on this revision (a-g only).
  - ``penalty_irc_6651/6654/6655`` — the penalty IRC section is now free-text
    line 6 (``irc_section`` -> f2_2), not checkboxes.

CPA/EA verification note
------------------------
These names match the f843.pdf (Rev. December 2024) as distributed on irs.gov.
If a later PDF is dropped into ``data/irs_forms/``, re-run the dump and re-pin
any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F843_FIELDS: dict[str, str] = {
    # --- Reason-for-filing "Other (specify)" text (bottom of the 17-box list)
    "filing_reason_other_text": f"{_P1}.f1_1[0]",

    # --- Identity block ---
    "name":                   f"{_P1}.f1_2[0]",   # Name(s)
    "ssn":                    f"{_P1}.f1_3[0]",   # Social security number
    "spouse_name":            f"{_P1}.f1_4[0]",   # Name of spouse (joint return)
    "spouse_ssn":             f"{_P1}.f1_5[0]",   # Spouse's SSN
    "address":                f"{_P1}.f1_6[0]",   # Address (number and street / P.O. box)
    "apt_no":                 f"{_P1}.f1_7[0]",   # Apt., room, or suite no.
    "city":                   f"{_P1}.f1_8[0]",   # City, town, or post office
    "state":                  f"{_P1}.f1_9[0]",   # State
    "zip":                    f"{_P1}.f1_10[0]",  # ZIP code
    "ein":                    f"{_P1}.f1_11[0]",  # Employer ID number (EIN)
    "foreign_country":        f"{_P1}.f1_12[0]",  # Foreign country name
    "foreign_province":       f"{_P1}.f1_13[0]",  # Foreign province/state/county
    "foreign_postal_code":    f"{_P1}.f1_14[0]",  # Foreign postal code
    "name_address_on_return": f"{_P1}.f1_15[0]",  # Name/address on return if different
    "daytime_phone":          f"{_P1}.f1_16[0]",  # Daytime telephone number

    # --- Line 1: tax period ---
    "period_begin": f"{_P1}.f1_17[0]",  # Beginning date (MM/DD/YYYY)
    "period_end":   f"{_P1}.f1_18[0]",  # Ending date (MM/DD/YYYY)

    # --- Line 2: amount ---
    "amount": f"{_P1}.f1_19[0]",  # Amount to be refunded or abated ($)

    # --- Line 3: date(s) of payment(s), boxes a-l ---
    "payment_date_1":  f"{_P1}.f1_20[0]",   # a
    "payment_date_2":  f"{_P1}.f1_21[0]",   # b
    "payment_date_3":  f"{_P1}.f1_22[0]",   # c
    "payment_date_4":  f"{_P1}.f1_23[0]",   # d
    "payment_date_5":  f"{_P1}.f1_24[0]",   # e
    "payment_date_6":  f"{_P1}.f1_25[0]",   # f
    "payment_date_7":  f"{_P1}.f1_26[0]",   # g
    "payment_date_8":  f"{_P1}.f1_27[0]",   # h
    "payment_date_9":  f"{_P1}.f1_28[0]",   # i
    "payment_date_10": f"{_P1}.f1_29[0]",   # j
    "payment_date_11": f"{_P1}.f1_30[0]",   # k
    "payment_date_12": f"{_P1}.f1_31[0]",   # l

    # --- Page 2 ---
    "return_other_text": f"{_P2}.f2_1[0]",  # Line 5n — Other (specify)
    "irc_section":       f"{_P2}.f2_2[0]",  # Line 6 — IRC section penalty based on
    "explanation":       f"{_P2}.ExplainWhy[0].f2_3[0]",  # Line 8 — explanation
    "ip_pin":            f"{_P2}.f2_4[0]",  # Taxpayer Identity Protection PIN
    "spouse_ip_pin":     f"{_P2}.f2_5[0]",  # Spouse Identity Protection PIN

    # --- Paid preparer block ---
    "preparer_name": f"{_P2}.f2_6[0]",   # Print/Type preparer's name
    "preparer_ptin": f"{_P2}.f2_7[0]",   # PTIN
    "firm_name":     f"{_P2}.f2_8[0]",   # Firm's name
    "firm_ein":      f"{_P2}.f2_9[0]",   # Firm's EIN
    "firm_address":  f"{_P2}.f2_10[0]",  # Firm's address
    "firm_phone":    f"{_P2}.f2_11[0]",  # Phone no.
}

# ---------------------------------------------------------------------------
# Checkboxes: logical key -> (fully-qualified AcroForm field name, on-state).
#
# The reason-for-filing group (c1_1[0..16]) and the line-7 reason group
# (c2_15[0..3]) are EXCLUSIVE one-of groups on the printed form: each position
# is its own terminal /Btn field, but its on-state is the position number
# (/1../17 and /1../4 respectively) — set at most ONE key per group.
# All other boxes are independent with on-states ["/1", "/Off"].
# ---------------------------------------------------------------------------
F843_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # --- Reason for filing Form 843 (top of page 1; check exactly one) ---
    # Tax
    "filing_reason_tax_other":                (f"{_P1}.c1_1[0]", "1"),   # tax other than income/estate/gift
    "filing_reason_tax_no_other_form":        (f"{_P1}.c1_1[1]", "2"),   # tax claimable only on Form 843
    "filing_reason_excess_fica_one_employer": (f"{_P1}.c1_1[2]", "3"),   # excess SS/Medicare/RRTA, one employer
    "filing_reason_excess_tier2_rrta":        (f"{_P1}.c1_1[3]", "4"),   # excess tier 2 RRTA, multiple employers
    "filing_reason_fica_withheld_in_error":   (f"{_P1}.c1_1[4]", "5"),   # SS/Medicare/RRTA withheld in error
    "filing_reason_tier1_rrta_employee_rep":  (f"{_P1}.c1_1[5]", "6"),   # tier 1 RRTA, employee representative
    # Penalty
    "filing_reason_penalty_reasonable_cause": (f"{_P1}.c1_1[6]", "7"),   # penalty — reasonable cause
    "filing_reason_penalty_6672_tfrp":        (f"{_P1}.c1_1[7]", "8"),   # section 6672 Trust Fund Recovery Penalty
    "filing_reason_penalty_6695a_appraisal":  (f"{_P1}.c1_1[8]", "9"),   # section 6695A incorrect appraisals
    "filing_reason_penalty_6715_dyed_fuel":   (f"{_P1}.c1_1[9]", "10"),  # section 6715 misuse of dyed fuel
    "filing_reason_6404f_written_advice":     (f"{_P1}.c1_1[10]", "11"), # section 6404(f) erroneous written advice
    # Interest
    "filing_reason_interest_irs_error_delay": (f"{_P1}.c1_1[11]", "12"), # section 6404(e)(1) IRS error or delay
    "filing_reason_net_interest_rate_zero":   (f"{_P1}.c1_1[12]", "13"), # Rev. Proc. 2000-26 net rate of zero
    # Other
    "filing_reason_print_notice_unreadable":  (f"{_P1}.c1_1[13]", "14"), # unable to read standard print notice
    "filing_reason_bpd_fee":                  (f"{_P1}.c1_1[14]", "15"), # branded prescription drug fee
    "filing_reason_health_insurance_fee":     (f"{_P1}.c1_1[15]", "16"), # annual fee on health insurance providers
    "filing_reason_other":                    (f"{_P1}.c1_1[16]", "17"), # Other (specify) -> filing_reason_other_text

    # --- Line 4: type of tax or fee (independent boxes a-g) ---
    "tax_type_employment":    (f"{_P1}.c1_2[0]", "1"),
    "tax_type_estate":        (f"{_P1}.c1_3[0]", "1"),
    "tax_type_gift":          (f"{_P1}.c1_4[0]", "1"),
    "tax_type_excise":        (f"{_P1}.c1_5[0]", "1"),
    "tax_type_income":        (f"{_P1}.c1_6[0]", "1"),
    "tax_type_fee":           (f"{_P1}.c1_7[0]", "1"),
    "tax_type_civil_penalty": (f"{_P1}.c1_8[0]", "1"),

    # --- Line 5: type of return filed (independent boxes a-n) ---
    "return_706":     (f"{_P2}.c2_1[0]", "1"),
    "return_709":     (f"{_P2}.c2_2[0]", "1"),
    "return_940":     (f"{_P2}.c2_3[0]", "1"),
    "return_941":     (f"{_P2}.c2_4[0]", "1"),
    "return_943":     (f"{_P2}.c2_5[0]", "1"),
    "return_944":     (f"{_P2}.c2_6[0]", "1"),
    "return_945":     (f"{_P2}.c2_7[0]", "1"),
    "return_990pf":   (f"{_P2}.c2_8[0]", "1"),
    "return_1040":    (f"{_P2}.c2_9[0]", "1"),
    "return_1120":    (f"{_P2}.c2_10[0]", "1"),
    "return_4720":    (f"{_P2}.c2_11[0]", "1"),
    "return_ct2":     (f"{_P2}.c2_12[0]", "1"),
    "return_bpd_fee": (f"{_P2}.c2_13[0]", "1"),
    "return_other":   (f"{_P2}.c2_14[0]", "1"),  # -> return_other_text

    # --- Line 7: reason for the request (check exactly one) ---
    "reason_irs_error":        (f"{_P2}.c2_15[0]", "1"),  # a — IRS errors or delays (interest)
    "reason_written_advice":   (f"{_P2}.c2_15[1]", "2"),  # b — erroneous written advice from the IRS
    "reason_reasonable_cause": (f"{_P2}.c2_15[2]", "3"),  # c — reasonable cause / other reason allowed
    "reason_none_of_above":    (f"{_P2}.c2_15[3]", "4"),  # d — none of the above

    # --- Paid preparer ---
    "preparer_self_employed": (f"{_P2}.c2_16[0]", "1"),
}

# Identity items the renderer never auto-fills, even if supplied. Form 843 is a
# standalone claim: the SSN/EIN identity block is REQUIRED for the IRS to
# process it, so (unlike the 4868/1120 renderers) nothing is withheld here —
# the caller's provenance-checked identity inputs are written as given.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset()

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 843 fields.
VALID_F843_FIELDS: frozenset[str] = frozenset(
    list(F843_FIELDS.values())
    + [v[0] for v in F843_CHECKBOX_FIELDS.values()]
)
