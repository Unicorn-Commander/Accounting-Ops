"""IRS Schedule A (Form 1040) — Itemized Deductions — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1040sa.pdf``, **Tax Year 2025** revision, "Created 11/20/25",
footer "Schedule A (Form 1040) 2025") with ``pypdf`` and cross-referencing every
terminal field's widget /Rect against the printed line labels (a positional
audit — the IRS XFA tooltips are empty on this form). Schedule A is a single-page
XFA-hybrid AcroForm, but unlike the 1040/W-4P it uses the ``form1[0].Page1[0]``
root naming (not ``topmostSubform[0]``). It is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that bakes
each value in as a flattened, visible overlay), NOT the page-based filler.

Pattern mirrors ``f1040_field_map.py`` / ``w4p_field_map.py``: a ``dict[str, str]``
of logical key -> the fully-qualified AcroForm field name, plus a checkbox map and
a ``VALID_*`` frozenset used by tests + the render-time provenance guard.

Schedule A is an ATTACHMENT to Form 1040 (it carries the itemized-deduction total
to Form 1040 line 12). This map is the honest "fill what the taxpayer / their
documents give us onto the official form" surface — it transcribes figures the
caller supplies; it does NOT compute the deduction.

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas); the IRS
  AcroForm boxes are plain text. The filler writes the caller's raw string.
- The IRS ``f1_NN`` numbering increments in document/tab order, so field numbers
  do NOT map 1:1 to line numbers — every name below was pinned positionally to its
  printed line. (E.g. line 8b's amount box ``f1_17`` precedes its payee-info box
  ``f1_16`` in tab order.)
- "Far-right column" totals (lines 4, 7, 10, 14, 15, 16-amount, 17) live in a
  separate money column (widget x≈504-576); the per-section detail lines live in
  the inner money column (x≈410-481). Both are plain text fields here.
- The TY2025 line 5e SALT cap printed on this revision is **$40,000 ($20,000 if
  married filing separately)** (the OBBBA-raised cap). The map does not enforce
  the cap — it transcribes whatever line 5e figure the caller provides.
- Conservative blank-fill: a line with no input value is simply not written.

Intentionally NOT in the writable map
--------------------------------------
- ``f1_2[0]`` — "Your social security number" (top-right). The taxpayer's SSN is
  a wet-ink/identity item LEFT BLANK for the taxpayer; the renderer never writes
  it. Documented here for completeness only.
- ``f1_19[0]`` — line 8d "Reserved for future use" (formerly mortgage insurance
  premiums). Not a line a preparer fills; omitted by design.

CPA/EA verification note
------------------------
These names match the TY2025 f1040sa.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1040sa_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the positional dump and re-pin any changed names.
"""

from __future__ import annotations

_P1 = "form1[0].Page1[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F1040SA_FIELDS: dict[str, str] = {
    # --- Identity (page-1 header) ---
    # "Name(s) shown on Form 1040 or 1040-SR". SSN (f1_2) is intentionally NOT
    # filled (left for the taxpayer) — see module docstring.
    "taxpayer_name":                 f"{_P1}.f1_1[0]",

    # --- Medical and Dental Expenses (lines 1-4) ---
    "medical_dental_1":              f"{_P1}.f1_3[0]",   # Line 1 — medical/dental expenses
    "agi_2":                         f"{_P1}.Line2_ReadOrder[0].f1_4[0]",  # Line 2 — AGI (Form 1040 line 11)
    "medical_agi_pct_3":             f"{_P1}.f1_5[0]",   # Line 3 — line 2 x 7.5%
    "medical_deduction_4":           f"{_P1}.f1_6[0]",   # Line 4 — line 1 - line 3 (far-right column)

    # --- Taxes You Paid (lines 5a-7) ---
    "salt_income_or_sales_5a":       f"{_P1}.f1_7[0]",   # Line 5a — state/local income OR general sales taxes
    "salt_real_estate_5b":           f"{_P1}.f1_8[0]",   # Line 5b — state/local real estate taxes
    "salt_personal_property_5c":     f"{_P1}.f1_9[0]",   # Line 5c — state/local personal property taxes
    "salt_subtotal_5d":              f"{_P1}.f1_10[0]",  # Line 5d — add 5a through 5c
    "salt_deduction_5e":             f"{_P1}.f1_11[0]",  # Line 5e — smaller of 5d or the cap ($40k / $20k MFS for TY2025)
    "other_taxes_type_6":            f"{_P1}.f1_12[0]",  # Line 6 — "List type" description (wide box)
    "other_taxes_6":                 f"{_P1}.f1_13[0]",  # Line 6 — other taxes amount
    "total_taxes_7":                 f"{_P1}.f1_14[0]",  # Line 7 — add 5e and 6 (far-right column)

    # --- Interest You Paid (lines 8a-10) ---
    "mortgage_interest_1098_8a":     f"{_P1}.f1_15[0]",  # Line 8a — home mortgage interest/points on Form 1098
    "mortgage_interest_no_1098_8b":  f"{_P1}.f1_17[0]",  # Line 8b — mortgage interest NOT on Form 1098 (amount)
    "mortgage_payee_8b":             f"{_P1}.Line8b_ReadOrder[0].f1_16[0]",  # Line 8b — payee name/ID/address (wide box)
    "points_not_reported_8c":        f"{_P1}.f1_18[0]",  # Line 8c — points not reported on Form 1098
    "mortgage_interest_subtotal_8e": f"{_P1}.f1_20[0]",  # Line 8e — add 8a through 8c
    "investment_interest_9":         f"{_P1}.f1_21[0]",  # Line 9 — investment interest (Form 4952)
    "total_interest_10":             f"{_P1}.f1_22[0]",  # Line 10 — add 8e and 9 (far-right column)

    # --- Gifts to Charity (lines 11-14) ---
    "gifts_cash_11":                 f"{_P1}.f1_23[0]",  # Line 11 — gifts by cash or check
    "gifts_noncash_12":              f"{_P1}.f1_24[0]",  # Line 12 — gifts other than by cash/check (Form 8283 if >$500)
    "gifts_carryover_13":            f"{_P1}.f1_25[0]",  # Line 13 — carryover from prior year
    "total_gifts_14":                f"{_P1}.f1_26[0]",  # Line 14 — add 11 through 13 (far-right column)

    # --- Casualty and Theft Losses (line 15) ---
    "casualty_theft_15":             f"{_P1}.f1_27[0]",  # Line 15 — federally declared disaster loss (Form 4684)

    # --- Other Itemized Deductions (line 16) ---
    "other_itemized_type_16":        f"{_P1}.f1_28[0]",  # Line 16 — "List type" description (wide box)
    "other_itemized_16":             f"{_P1}.f1_29[0]",  # Line 16 — other itemized deductions amount (far-right column)

    # --- Total Itemized Deductions (line 17) ---
    "total_itemized_17":             f"{_P1}.f1_30[0]",  # Line 17 — add lines 4-16; carries to Form 1040 line 12
}

# ---------------------------------------------------------------------------
# Checkbox fields: logical key -> (AcroForm field name, on-state).
# Each is an INDEPENDENT single checkbox (its own /Btn, on-state "/1") — not a
# mutually-exclusive radio group. Set only when the caller supplies a truthy
# value; otherwise left /Off (conservative). Verified on-states from the dump.
# ---------------------------------------------------------------------------
F1040SA_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 5a — elect to include general sales taxes instead of income taxes.
    "sales_tax_election_5a": (f"{_P1}.c1_1[0]", "1"),
    # Line 8 — check if you didn't use all of your mortgage loan(s) to buy,
    # build, or improve your home (deduction may be limited).
    "mortgage_use_box_8":    (f"{_P1}.Line8_ReadOrder[0].c1_2[0]", "1"),
    # Line 18 — elect to itemize even though deductions are less than the
    # standard deduction.
    "elect_itemize_18":      (f"{_P1}.Line18_ReadOrder[0].c1_3[0]", "1"),
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Schedule A fields.
VALID_F1040SA_FIELDS: frozenset[str] = frozenset(
    list(F1040SA_FIELDS.values())
    + [v[0] for v in F1040SA_CHECKBOX_FIELDS.values()]
)
