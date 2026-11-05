"""IRS Form 9465 (Payment Plan Request) — AcroForm field map.

Revision: **Rev. December 2026** (the current ``data/irs_forms/f9465.pdf``,
footer "Form 9465 (Rev. 12-2026) Created 7/14/26", header "(Rev. December
2026)", OMB No. 1545-0074, Cat. No. 14842Y — 1 page). Pinned by
``F9465_PINNED_REVISION``, ``F9465_PINNED_PAGE_COUNT``,
``F9465_PINNED_FIELD_COUNT`` and ``F9465_PINNED_SHA256``. A downloaded PDF
whose sha256 and printed revision both differ fails
``test_map_matches_source_acroform`` with "Form 9465 revision changed…"
before any field name is checked.

Field names were verified by dumping the official fillable PDF with ``pypdf``
(``PdfReader().get_fields()``) and pinning every terminal widget's /Rect
against the printed line captions extracted with pymupdf, then confirmed with
a rasterized marker render. Checkbox on-states are each widget's ``/AP``
``/N`` export value (both remaining boxes export ``/1``). Form 9465 is a
modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so it MUST be
filled via ``form_prefill._render_acroform_pdf``.

Ground truth (Rev. 12-2026)
---------------------------
One page. Title is now "Payment Plan Request" (the body heading is
"Long-Term Payment Plan Request"). ``get_fields()`` returns 40 entries:
32 terminal widgets (30 ``/Tx`` + 2 ``/Btn``) and 8 non-terminal parents.

Page 1 — Long-Term Payment Plan Request:
  f1_01 request is for Form(s)          f1_02 tax year(s) or period(s)
  1a: f1_03 first name/initial · f1_04 last name · f1_05 your SSN
      f1_06 spouse first name  · f1_07 spouse last name · f1_08 spouse SSN
      f1_09 street address · f1_10 apt · f1_11 city/state/ZIP (ONE box)
      f1_12 foreign country · f1_13 foreign province · f1_14 foreign postal
  1b: c1_1 address-is-new checkbox (on-state /1)
  2:  f1_15 sole-proprietorship name (no longer operating) · f1_16 EIN
  3:  PhoneNumber_Ln3_ReadOrder[0].f1_17 home phone
      BestTime_Ln3_ReadOrder[0].f1_18 best time to call
  4:  PhoneNumber_Ln4_ReadOrder[0].f1_19 work phone
      Ext_ReadOrder[0].f1_20 ext.
      f1_21 best time to call (this one is NOT under a ReadOrder parent)
  5-9: f1_22..f1_26 (total owed / additional balances / 5+6 sum / payment
      with this request / amount owed). Same captions as Rev. 9-2020.
  10: f1_27 amount you can pay each month. The old line-10 box (line 9 ÷ 72)
      is gone; that sentence is now printed instructions under this line,
      and the old 11a/11b pair is this one box. Key stays
      ``monthly_payment_11a``.
  11: f1_28 day you want the payment made (not later than the 28th). Was
      line 12. Key stays ``payment_day_12``.
  12a: RoutingNo[0].f1_29 routing number (was line 13a)
  12b: AccountNo[0].f1_30 account number (was line 13b)
  12c: c1_2 low-income, can't pay by debit (on-state /1). Was line 13c /
      c1_3. Direct child of Page1 — the Rev. 9-2020 BulletedList nesting
      is gone, and so is the 433-F bullet that used to own c1_2.

No fillable signature or date (those lines are printed only). No
account-type (checking/savings) choice. No payroll-deduction box.

Lines removed on this revision (keys left unmapped)
---------------------------------------------------
A missing printed line has no widget whose /Rect sits on that caption, so
the key is NOT in the maps. ``F9465_UNMAPPED_KEYS`` is that list. Callers
that still send these keys get a conservative skip (nothing is written).

  - ``divided_by_72_10`` — no ÷72 entry box. Line 10 is the monthly amount.
  - ``revised_monthly_payment_11b`` — line 11b removed. An existing plan's
    total proposed payment goes in line 10 (``monthly_payment_11a``).
  - ``cannot_increase_11b_433f`` — the Form 433-F bullet is gone.
  - ``payroll_deduction_14`` — line 14 (Form 2159) is gone.
  - All of old Part II (the whole second page): ``county_15``,
    ``marital_single_16a`` / ``marital_married_16a``,
    ``share_expenses_yes_16b`` / ``share_expenses_no_16b``,
    ``dependents_17``, ``age_65_older_18``, the line 19 and 21 pay-frequency
    choices, ``net_income_20``, ``spouse_net_income_22``, ``vehicles_23``,
    ``car_payments_24``, the line 25 health-insurance choices,
    ``health_premiums_25c``, the line 26 court-order choices,
    ``court_payments_26c``, ``dependent_care_27``.

Conventions mirror ``f4868_field_map.py``: ``dict[str, str]`` of logical key
-> fully-qualified AcroForm field name; each checkbox carries its own
on-state. Dollar amounts are plain strings. Conservative blank-fill: a line
with no input value is simply not written.

Taxpayer-completed fields
-------------------------
Bank routing/account numbers are sensitive payment credentials. Like SSNs,
these are intentionally LEFT BLANK by this renderer even if values were
supplied — they are identity/payment items the taxpayer must enter themselves
to ensure security and accuracy of banking details. They stay in the map for
documentation, but ``TAXPAYER_COMPLETED_KEYS`` gates them off.

History — Rev. September 2020
-----------------------------
The previous ``data/irs_forms/f9465.pdf`` was "Form 9465 (Rev. 9-2020)",
OMB 1545-0074, Cat. No. 14842Y, 2 pages, titled "Installment Agreement
Request". Terminal names were unpadded (``f1_1`` … ``f1_9``, then ``f1_10``
…). Kept here so a diff against that revision is readable. Do not fill a
Rev. 12-2026 PDF with these names.

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
      with request / line 9 − line 8 / line 9 ÷ 72)
  11a/11b: f1_28 / f1_29 (proposed / revised monthly payment)
  bullet after 11b: BulletedList[0].Bullet1[0].c1_2 (can't-increase-to-line-10
      → attach Form 433-F)
  12: f1_30 payment day (not later than the 28th)
  13a/13b: f1_31 routing number · f1_32 account number (direct debit)
  13c: c1_3 low-income can't-make-debit-payments
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

Dropped legacy keys (no plausible target on Rev. 9-2020, and still none)
-----------------------------------------------------------------------
``account_type`` (no such box), ``city``/``state``/``zip`` (single combined
box → ``city_state_zip``), ``name``/``spouse_name`` (split first/last),
``phone`` (form has separate home/work phones), ``household_status`` /
``employment_status`` / ``payment_method`` (not on this form),
``related_return`` / ``other_agreement`` / ``consent`` (not on this form).
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_LN3_PHONE = f"{_P1}.PhoneNumber_Ln3_ReadOrder[0]"
_LN3_TIME = f"{_P1}.BestTime_Ln3_ReadOrder[0]"
_LN4_PHONE = f"{_P1}.PhoneNumber_Ln4_ReadOrder[0]"
_LN4_EXT = f"{_P1}.Ext_ReadOrder[0]"
_ROUTING = f"{_P1}.RoutingNo[0]"
_ACCOUNT = f"{_P1}.AccountNo[0]"

# Printed footer string, page count, ``PdfReader.get_fields()`` count (parents
# included), and sha256 of the PDF this map was verified against. The download
# script records the same revision. Tests fail closed when a new PDF matches
# neither the sha256 nor this revision string.
F9465_PINNED_REVISION = "Form 9465 (Rev. 12-2026)"
F9465_PINNED_PAGE_COUNT = 1
F9465_PINNED_FIELD_COUNT = 40
F9465_PINNED_SHA256 = (
    "2c1253f4902d2ae20a3041d04b64e206f618cbd9d4500ae0aa3d859fc063191a"
)

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F9465_FIELDS: dict[str, str] = {
    # --- Header: what this request covers ---
    "request_forms": f"{_P1}.f1_01[0]",  # "This request is for Form(s)…"
    "tax_periods": f"{_P1}.f1_02[0]",  # "Enter tax year(s) or period(s) involved"

    # --- Line 1a: Taxpayer identification ---
    "first_name": f"{_P1}.f1_03[0]",  # Your first name and initial
    "last_name": f"{_P1}.f1_04[0]",  # Last name
    "ssn": f"{_P1}.f1_05[0]",  # Your SSN (taxpayer-completed; never filled)
    "spouse_first_name": f"{_P1}.f1_06[0]",  # Spouse's first name and initial
    "spouse_last_name": f"{_P1}.f1_07[0]",  # Spouse's last name
    "spouse_ssn": f"{_P1}.f1_08[0]",  # Spouse's SSN (taxpayer-completed; never filled)
    "address": f"{_P1}.f1_09[0]",  # Current address (number and street)
    "apt": f"{_P1}.f1_10[0]",  # Apt. number
    "city_state_zip": f"{_P1}.f1_11[0]",  # City, state, and ZIP (one box)
    "foreign_country": f"{_P1}.f1_12[0]",  # Foreign country name
    "foreign_province": f"{_P1}.f1_13[0]",  # Foreign province/state/county
    "foreign_postal_code": f"{_P1}.f1_14[0]",  # Foreign postal code

    # --- Line 2: Defunct sole proprietorship ---
    "business_name_2": f"{_P1}.f1_15[0]",  # Name of the business (no longer operating)
    "business_ein_2": f"{_P1}.f1_16[0]",  # Employer identification number (EIN)

    # --- Lines 3-4: Phones (several leaves sit under a ReadOrder parent) ---
    "home_phone_3": f"{_LN3_PHONE}.f1_17[0]",  # Line 3 — home phone number
    "best_time_call_3": f"{_LN3_TIME}.f1_18[0]",  # Line 3 — best time for us to call
    "work_phone_4": f"{_LN4_PHONE}.f1_19[0]",  # Line 4 — work phone number
    "work_ext_4": f"{_LN4_EXT}.f1_20[0]",  # Line 4 — extension
    "best_time_call_4": f"{_P1}.f1_21[0]",  # Line 4 — best time (not nested)

    # --- Lines 5-9: the amount column (captions unchanged from Rev. 9-2020) ---
    "total_owed_5": f"{_P1}.f1_22[0]",  # Line 5 — total amount owed
    "additional_bal_6": f"{_P1}.f1_23[0]",  # Line 6 — additional balances due
    "total_liability_7": f"{_P1}.f1_24[0]",  # Line 7 — add lines 5 and 6
    "payment_with_request_8": f"{_P1}.f1_25[0]",  # Line 8 — payment with this request
    "amount_owed_9": f"{_P1}.f1_26[0]",  # Line 9 — line 7 minus line 8

    # Printed line 10. Was line 11a. The old ÷72 box is not a field anymore.
    "monthly_payment_11a": f"{_P1}.f1_27[0]",
    # Printed line 11. Was line 12. Day of the month, 1-28.
    "payment_day_12": f"{_P1}.f1_28[0]",

    # --- Printed lines 12a/12b (were 13a/13b). Taxpayer-completed. ---
    "routing_number": f"{_ROUTING}.f1_29[0]",  # Line 12a — routing number
    "account_number": f"{_ACCOUNT}.f1_30[0]",  # Line 12b — account number
}

# ---------------------------------------------------------------------------
# Checkboxes: logical key -> (AcroForm field name, on-state).
# On-states are the widget's /AP /N export value with the leading slash
# stripped. This revision has two checkboxes, each exporting exactly /1.
# ---------------------------------------------------------------------------
F9465_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    "address_new_1b": (f"{_P1}.c1_1[0]", "1"),  # 1b — address new since last return
    # Printed line 12c (was 13c). On-state /1 from /AP /N. Not nested.
    "low_income_no_debit_13c": (f"{_P1}.c1_2[0]", "1"),
}

# Identity/Payment items the renderer never auto-fills, even if supplied — the
# taxpayer enters these themselves for security.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    "ssn", "spouse_ssn", "routing_number", "account_number",
})

# Rev. 9-2020 keys whose line is not on Rev. 12-2026. Documented, never written.
F9465_UNMAPPED_KEYS: frozenset[str] = frozenset({
    "divided_by_72_10",
    "revised_monthly_payment_11b",
    "cannot_increase_11b_433f",
    "payroll_deduction_14",
    "county_15",
    "marital_single_16a",
    "marital_married_16a",
    "share_expenses_yes_16b",
    "share_expenses_no_16b",
    "dependents_17",
    "age_65_older_18",
    "paid_weekly_19",
    "paid_biweekly_19",
    "paid_monthly_19",
    "paid_twice_month_19",
    "net_income_20",
    "spouse_paid_weekly_21",
    "spouse_paid_biweekly_21",
    "spouse_paid_monthly_21",
    "spouse_paid_twice_month_21",
    "spouse_net_income_22",
    "vehicles_23",
    "car_payments_24",
    "health_insurance_yes_25a",
    "health_insurance_no_25a",
    "premiums_deducted_yes_25b",
    "premiums_deducted_no_25b",
    "health_premiums_25c",
    "court_payments_yes_26a",
    "court_payments_no_26a",
    "court_deducted_yes_26b",
    "court_deducted_no_26b",
    "court_payments_26c",
    "dependent_care_27",
})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 9465 fields.
VALID_F9465_FIELDS: frozenset[str] = frozenset(
    list(F9465_FIELDS.values())
    + [v[0] for v in F9465_CHECKBOX_FIELDS.values()]
)
