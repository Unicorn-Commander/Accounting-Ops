"""IRS Form 9465 (Installment Agreement Request) — AcroForm field map.

Revision: **Rev. September 2020** (the current ``data/irs_forms/f9465.pdf``,
"Form 9465 (Rev. 9-2020)", OMB 1545-0074, Cat. No. 14842Y — 2 pages).

Field names were verified by dumping the official fillable PDF with ``pypdf``
(``PdfReader().get_fields()``) and pinning every terminal widget's /Rect
against the printed line captions extracted with pymupdf, then confirmed with
a rasterized marker render. Form 9465 is a modern XFA-hybrid AcroForm
(``topmostSubform[0]`` naming), so it MUST be filled via
``form_prefill._render_acroform_pdf``.

Ground truth (Rev. 9-2020)
--------------------------
Page 1 — Part I, Installment Agreement Request:
  f1_1  request is for Form(s)         f1_2  tax year(s)/period(s)
  1a: f1_3 first name/initial · f1_4 last name · f1_5 your SSN
      f1_6 spouse first name  · f1_7 spouse last name · f1_8 spouse SSN
      f1_9 street address · f1_10 apt · f1_11 city/state/ZIP (ONE box)
      f1_12 foreign country · f1_13 foreign province · f1_14 foreign postal
  1b: c1_1 address-is-new checkbox
  2:  f1_15 business name · f1_16 business EIN
  3:  f1_17 home phone · f1_18 best time to call
  4:  f1_19 work phone · f1_20 ext. · f1_21 best time to call
  5-10: f1_22..f1_27 (total owed / additional balances / 5+6 sum / payment
      with request / amount owed 7-8 / line 9 ÷ 72)
  11a/11b: f1_28 / f1_29 (proposed / revised monthly payment)
  bullet after 11b: c1_2 (can't-increase-to-line-10 → attach Form 433-F);
      NOTE its qualified name nests under ``BulletedList[0].Bullet1[0]``.
  12: f1_30 payment day (not later than the 28th)
  13a/13b: f1_31 routing number · f1_32 account number (direct debit)
  13c: c1_3 low-income can't-make-debit-payments (fee reimbursement)
  14: c1_4 payroll deduction (attach Form 2159)

Page 2 — Part II, Additional Information:
  15: f2_1 county of primary residence
  16a: c2_1[0]=/1 single · c2_1[1]=/2 married
  16b: c2_2[0]=/1 yes · c2_2[1]=/2 no (share household expenses)
  17: f2_2 dependents · 18: f2_3 household members 65+
  19: c2_3[0..3]=/1../4 pay frequency (weekly / every 2 weeks / monthly /
      twice a month) · 20: f2_4 net income per pay period
  21: c2_4[0..3]=/1../4 spouse pay frequency · 22: f2_5 spouse net income
  23: f2_6 vehicles owned · 24: f2_7 car payments per month
  25a: c2_5[0]/[1] health insurance y/n · 25b: c2_6[0]/[1] premiums
      deducted y/n · 25c: f2_8 monthly premiums
  26a: c2_7[0]/[1] court-ordered payments y/n · 26b: c2_8[0]/[1] deducted
      from paycheck y/n · 26c: f2_9 monthly court-ordered amount
  27: f2_10 child/dependent care paid per month

There is NO account-type (checking/savings) choice on this revision, and no
fillable signature/date fields (signature happens on the printed form).

Conventions mirror ``f4868_field_map.py``: ``dict[str, str]`` of logical key
-> fully-qualified AcroForm field name; radio-style choices are modeled as
per-choice checkbox entries carrying each widget's own on-state. Dollar
amounts are plain strings. Conservative blank-fill: a line with no input
value is simply not written.

Taxpayer-completed fields
-------------------------
Bank routing/account numbers are sensitive payment credentials. Like SSNs,
these are intentionally LEFT BLANK by this renderer even if values were
supplied — they are identity/payment items the taxpayer must enter themselves
to ensure security and accuracy of banking details. They stay in the map for
documentation, but ``TAXPAYER_COMPLETED_KEYS`` gates them off.

Dropped legacy keys (no plausible target on Rev. 9-2020)
--------------------------------------------------------
``account_type`` (no such box), ``city``/``state``/``zip`` (single combined
box → ``city_state_zip``), ``name``/``spouse_name`` (split first/last),
``phone`` (form has separate home/work phones), ``household_status`` /
``employment_status`` / ``payment_method`` (not on this form — the real
Part II asks marital status 16a, shared expenses 16b, pay frequency 19/21,
health insurance 25, court-ordered payments 26), ``related_return`` /
``other_agreement`` / ``consent`` (not on this form — the real checkboxes
are 1b / the 433-F bullet / 13c / 14).
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F9465_FIELDS: dict[str, str] = {
    # --- Header: what this request covers ---
    "request_forms":     f"{_P1}.f1_1[0]",   # "This request is for Form(s)…" (e.g. "1040")
    "tax_periods":       f"{_P1}.f1_2[0]",   # "Enter tax year(s) or period(s) involved"

    # --- Line 1a: Taxpayer identification ---
    "first_name":        f"{_P1}.f1_3[0]",   # Your first name and initial
    "last_name":         f"{_P1}.f1_4[0]",   # Last name
    "ssn":               f"{_P1}.f1_5[0]",   # Your SSN (taxpayer-completed; never filled)
    "spouse_first_name": f"{_P1}.f1_6[0]",   # Spouse's first name and initial
    "spouse_last_name":  f"{_P1}.f1_7[0]",   # Spouse's last name
    "spouse_ssn":        f"{_P1}.f1_8[0]",   # Spouse's SSN (taxpayer-completed; never filled)
    "address":           f"{_P1}.f1_9[0]",   # Current address (number and street)
    "apt":               f"{_P1}.f1_10[0]",  # Apt. number
    "city_state_zip":    f"{_P1}.f1_11[0]",  # City, town or post office, state, and ZIP (one box)
    "foreign_country":   f"{_P1}.f1_12[0]",  # Foreign country name
    "foreign_province":  f"{_P1}.f1_13[0]",  # Foreign province/state/county
    "foreign_postal_code": f"{_P1}.f1_14[0]",  # Foreign postal code

    # --- Line 2: Defunct business ---
    "business_name_2":   f"{_P1}.f1_15[0]",  # Name of your business (no longer operating)
    "business_ein_2":    f"{_P1}.f1_16[0]",  # Employer identification number (EIN)

    # --- Lines 3-4: Phones ---
    "home_phone_3":      f"{_P1}.f1_17[0]",  # Line 3 — home phone number
    "best_time_call_3":  f"{_P1}.f1_18[0]",  # Line 3 — best time for us to call
    "work_phone_4":      f"{_P1}.f1_19[0]",  # Line 4 — work phone number
    "work_ext_4":        f"{_P1}.f1_20[0]",  # Line 4 — extension
    "best_time_call_4":  f"{_P1}.f1_21[0]",  # Line 4 — best time for us to call

    # --- Lines 5-12: Financials ---
    "total_owed_5":      f"{_P1}.f1_22[0]",  # Line 5 — total amount owed per return(s)/notice(s)
    "additional_bal_6":  f"{_P1}.f1_23[0]",  # Line 6 — additional balances due
    "total_liability_7": f"{_P1}.f1_24[0]",  # Line 7 — add lines 5 and 6
    "payment_with_request_8": f"{_P1}.f1_25[0]",  # Line 8 — payment made with this request
    "amount_owed_9":     f"{_P1}.f1_26[0]",  # Line 9 — line 7 minus line 8
    "divided_by_72_10":  f"{_P1}.f1_27[0]",  # Line 10 — line 9 ÷ 72
    "monthly_payment_11a": f"{_P1}.f1_28[0]",  # Line 11a — amount you can pay each month
    "revised_monthly_payment_11b": f"{_P1}.f1_29[0]",  # Line 11b — revised monthly payment
    "payment_day_12":    f"{_P1}.f1_30[0]",  # Line 12 — payment day each month (1-28)

    # --- Line 13: Direct debit (taxpayer-completed; never filled) ---
    "routing_number":    f"{_P1}.f1_31[0]",  # Line 13a — routing number
    "account_number":    f"{_P1}.f1_32[0]",  # Line 13b — account number

    # --- Part II (page 2) ---
    "county_15":         f"{_P2}.f2_1[0]",   # Line 15 — county of primary residence
    "dependents_17":     f"{_P2}.f2_2[0]",   # Line 17 — dependents claimable this year
    "age_65_older_18":   f"{_P2}.f2_3[0]",   # Line 18 — household members 65 or older
    "net_income_20":     f"{_P2}.f2_4[0]",   # Line 20 — net income per pay period
    "spouse_net_income_22": f"{_P2}.f2_5[0]",  # Line 22 — spouse's net income per pay period
    "vehicles_23":       f"{_P2}.f2_6[0]",   # Line 23 — vehicles owned
    "car_payments_24":   f"{_P2}.f2_7[0]",   # Line 24 — car payments per month
    "health_premiums_25c": f"{_P2}.f2_8[0]",  # Line 25c — monthly health insurance premiums
    "court_payments_26c": f"{_P2}.f2_9[0]",  # Line 26c — monthly court-ordered payments
    "dependent_care_27": f"{_P2}.f2_10[0]",  # Line 27 — child/dependent care per month
}

# ---------------------------------------------------------------------------
# Checkboxes / radio choices: logical key -> (AcroForm field name, on-state).
# Radio-style questions are one entry PER CHOICE because each XFA kid is its
# own terminal field with its own on-state (/1, /2, /3, /4). A truthy input
# on a key sets exactly that widget; the caller picks at most one per group.
# ---------------------------------------------------------------------------
F9465_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # --- Page 1 ---
    "address_new_1b":  (f"{_P1}.c1_1[0]", "1"),  # 1b — address new since last return
    # Bullet after 11b — can't increase 11b to line 10; attach Form 433-F.
    # NOTE the nested container in the qualified name.
    "cannot_increase_11b_433f": (f"{_P1}.BulletedList[0].Bullet1[0].c1_2[0]", "1"),
    "low_income_no_debit_13c": (f"{_P1}.c1_3[0]", "1"),  # 13c — low-income, can't debit
    "payroll_deduction_14":    (f"{_P1}.c1_4[0]", "1"),  # 14 — payroll deduction (Form 2159)

    # --- Page 2, Part II ---
    "marital_single_16a":      (f"{_P2}.c2_1[0]", "1"),  # 16a — single
    "marital_married_16a":     (f"{_P2}.c2_1[1]", "2"),  # 16a — married
    "share_expenses_yes_16b":  (f"{_P2}.c2_2[0]", "1"),  # 16b — share household expenses: yes
    "share_expenses_no_16b":   (f"{_P2}.c2_2[1]", "2"),  # 16b — no
    "paid_weekly_19":          (f"{_P2}.c2_3[0]", "1"),  # 19 — paid once a week
    "paid_biweekly_19":        (f"{_P2}.c2_3[1]", "2"),  # 19 — once every 2 weeks
    "paid_monthly_19":         (f"{_P2}.c2_3[2]", "3"),  # 19 — once a month
    "paid_twice_month_19":     (f"{_P2}.c2_3[3]", "4"),  # 19 — twice a month
    "spouse_paid_weekly_21":   (f"{_P2}.c2_4[0]", "1"),  # 21 — spouse paid once a week
    "spouse_paid_biweekly_21": (f"{_P2}.c2_4[1]", "2"),  # 21 — once every 2 weeks
    "spouse_paid_monthly_21":  (f"{_P2}.c2_4[2]", "3"),  # 21 — once a month
    "spouse_paid_twice_month_21": (f"{_P2}.c2_4[3]", "4"),  # 21 — twice a month
    "health_insurance_yes_25a": (f"{_P2}.c2_5[0]", "1"),  # 25a — health insurance: yes
    "health_insurance_no_25a":  (f"{_P2}.c2_5[1]", "2"),  # 25a — no
    "premiums_deducted_yes_25b": (f"{_P2}.c2_6[0]", "1"),  # 25b — premiums deducted: yes
    "premiums_deducted_no_25b":  (f"{_P2}.c2_6[1]", "2"),  # 25b — no
    "court_payments_yes_26a":  (f"{_P2}.c2_7[0]", "1"),  # 26a — court-ordered payments: yes
    "court_payments_no_26a":   (f"{_P2}.c2_7[1]", "2"),  # 26a — no
    "court_deducted_yes_26b":  (f"{_P2}.c2_8[0]", "1"),  # 26b — deducted from paycheck: yes
    "court_deducted_no_26b":   (f"{_P2}.c2_8[1]", "2"),  # 26b — no
}

# Identity/Payment items the renderer never auto-fills, even if supplied — the
# taxpayer enters these themselves for security.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    "ssn", "spouse_ssn", "routing_number", "account_number"
})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 9465 fields.
VALID_F9465_FIELDS: frozenset[str] = frozenset(
    list(F9465_FIELDS.values())
    + [v[0] for v in F9465_CHECKBOX_FIELDS.values()]
)
