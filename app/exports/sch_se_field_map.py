"""Schedule SE (Form 1040) — Self-Employment Tax — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1040sse.pdf``, **Tax Year 2025** revision, "Created 5/7/25")
with ``pypdf`` and cross-referencing every terminal field's widget /Rect against
the printed line labels (a positional audit — the IRS XFA tooltips are empty on
this form). Schedule SE is a modern XFA-hybrid AcroForm (``topmostSubform[0]``
naming, like the W-4P / Form 1040), so it MUST be filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that
sets /V, BAKES each value in as a flattened visible overlay, and drops the XFA
layer), NOT the older page-based ``_fill_pdf_fields``.

Pattern mirrors ``w4p_field_map.py`` / ``f1040_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.

This map covers the name + Part I (the self-employment-tax computation lines)
and Part II (the optional-method lines) that a preparer fills. It is the honest
"fill what the taxpayer / their documents give us onto the official form"
surface — it does not itself compute the SE tax.

Intentionally NOT mapped (left blank for the human)
---------------------------------------------------
- The "Social security number of person with self-employment income" box
  (``f1_2``) — a taxpayer identity item, left blank for the taxpayer to enter
  (mirrors the Form 1040 renderer's SSN policy). There is no signature line on
  Schedule SE (it attaches to the signed Form 1040).
- Line 7 (``f1_13``) and Line 14 (``f2_1``) are PREPRINTED statutory constants
  on the face of the form (2025: Line 7 = $176,100; Line 14 = $7,240) whose
  AcroForm widgets are ~1pt-wide vestigial slivers — they are not user-fillable
  and are deliberately excluded.

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas). The
  IRS AcroForm boxes are plain-text, not numeric.
- The IRS ``f{page}_{NN}`` numbering increments in document/tab order, so field
  numbers do NOT map 1:1 to line numbers — every name below was pinned
  positionally to its printed line.
- Line A is a single check box (minister / member of a religious order /
  Christian Science practitioner who filed Form 4361 but had $400+ of other
  self-employment earnings). Its on-state is /1.
- Conservative blank-fill: a line with no input value is simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f1040sse.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1040sse_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names + the
preprinted Line 7 / Line 14 constants before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
SCH_SE_FIELDS: dict[str, str] = {
    # --- Identity (page 1 header) ---
    # Name of person with self-employment income (as shown on Form 1040). The
    # accompanying SSN box (f1_2) is intentionally left blank for the taxpayer.
    "name":                     f"{_P1}.f1_1[0]",

    # --- Part I: Self-Employment Tax (page 1) ---
    # Line 1a — Net farm profit or (loss) from Schedule F / farm K-1.
    "net_farm_profit_1a":       f"{_P1}.f1_3[0]",
    # Line 1b — Conservation Reserve Program payments (subtraction; printed in
    # parentheses on the form).
    "crp_payments_1b":          f"{_P1}.f1_4[0]",
    # Line 2 — Net profit or (loss) from Schedule C / non-farm K-1.
    "net_nonfarm_profit_2":     f"{_P1}.f1_5[0]",
    # Line 3 — Combine lines 1a, 1b, and 2.
    "combined_se_income_3":     f"{_P1}.f1_6[0]",
    # Line 4a — Line 3 × 92.35% (or line 3 if not over zero).
    "net_earnings_4a":          f"{_P1}.f1_7[0]",
    # Line 4b — Optional-methods total (lines 15 + 17), if elected.
    "optional_methods_4b":      f"{_P1}.f1_8[0]",
    # Line 4c — Combine lines 4a and 4b.
    "combined_earnings_4c":     f"{_P1}.f1_9[0]",
    # Line 5a — Church employee income from Form W-2 (indented box).
    "church_employee_income_5a": f"{_P1}.Line5a_ReadOrder[0].f1_10[0]",
    # Line 5b — Line 5a × 92.35% (if less than $100, -0-).
    "church_se_income_5b":      f"{_P1}.f1_11[0]",
    # Line 6 — Add lines 4c and 5b.
    "total_se_income_6":        f"{_P1}.f1_12[0]",
    # Line 8a — Total social security wages and tips (W-2 boxes 3 + 7) and RRTA
    # tier-1 compensation (indented box).
    "ss_wages_tips_8a":         f"{_P1}.Line8a_ReadOrder[0].f1_14[0]",
    # Line 8b — Unreported tips subject to SS tax (Form 4137, line 10).
    "unreported_tips_8b":       f"{_P1}.f1_15[0]",
    # Line 8c — Wages subject to SS tax (Form 8919, line 10).
    "wages_8919_8c":            f"{_P1}.f1_16[0]",
    # Line 8d — Add lines 8a, 8b, and 8c.
    "total_ss_wages_8d":        f"{_P1}.f1_17[0]",
    # Line 9 — Subtract line 8d from line 7 (remaining SS wage base).
    "ss_base_remaining_9":      f"{_P1}.f1_18[0]",
    # Line 10 — Smaller of line 6 or line 9 × 12.4% (social security portion).
    "ss_tax_10":                f"{_P1}.f1_19[0]",
    # Line 11 — Line 6 × 2.9% (Medicare portion).
    "medicare_tax_11":          f"{_P1}.f1_20[0]",
    # Line 12 — Self-employment tax. Add lines 10 and 11.
    "self_employment_tax_12":   f"{_P1}.f1_21[0]",
    # Line 13 — Deduction for one-half of SE tax. Line 12 × 50% (indented box).
    "half_se_tax_deduction_13": f"{_P1}.f1_22[0]",

    # --- Part II: Optional Methods To Figure Net Earnings (page 2) ---
    # Line 15 — Farm optional method amount (also include on line 4b).
    "farm_optional_15":         f"{_P2}.f2_2[0]",
    # Line 16 — Subtract line 15 from line 14.
    "subtract_16":              f"{_P2}.f2_3[0]",
    # Line 17 — Nonfarm optional method amount (also include on line 4b).
    "nonfarm_optional_17":      f"{_P2}.f2_4[0]",
}

# ---------------------------------------------------------------------------
# Check boxes: logical key -> (AcroForm field name, on-state). Set only when the
# caller supplies a truthy value; otherwise left /Off (conservative).
# ---------------------------------------------------------------------------
SCH_SE_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line A — minister / member of a religious order / Christian Science
    # practitioner who filed Form 4361 but had $400+ of other self-employment
    # earnings ("check here and continue with Part I").
    "minister_exempt_4361_a": (f"{_P1}.c1_1[0]", "1"),
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Schedule SE fields.
VALID_SCH_SE_FIELDS: frozenset[str] = frozenset(
    list(SCH_SE_FIELDS.values())
    + [v[0] for v in SCH_SE_CHECKBOX_FIELDS.values()]
)
