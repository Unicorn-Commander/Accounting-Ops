"""Schedule K-1 (Form 1065) — Partner's Share of Income, Deductions, Credits,
etc. — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1065sk1.pdf``, **Tax Year 2025** revision, "Created 2/26/25")
with ``pypdf`` (``PdfReader().get_fields()``) and cross-referencing every one of
the 111 terminal widget /Rect positions against the printed line/box labels (a
positional audit — the IRS XFA tooltips are empty on this form). The K-1 is a
modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the W-4P / 1040 /
1065), so it MUST be filled via ``form_prefill._render_acroform_pdf`` (the
XFA-safe /Fields-tree walker that BAKES values in as a flattened visible
overlay), not the older page-based ``_fill_pdf_fields``.

Pattern mirrors ``f1065_field_map.py`` / ``sch_se_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name for
text boxes, plus a ``dict[str, tuple[str, str]]`` of logical key -> (field name,
on-state) for the checkboxes / radio options.

Scope
-----
The Schedule K-1 is issued BY the partnership TO a partner and is attached to the
Form 1065. This map covers the whole single-page face:

* Header — tax-year begin/end date boxes; the "Final K-1" and "Amended K-1"
  checkboxes.
* Part I (Information About the Partnership) — name/address (B), IRS center (C),
  the publicly-traded-partnership checkbox (D).
* Part II (Information About the Partner) — partner name/address (F), the
  general/limited (G) and domestic/foreign (H1) radio options, the
  disregarded-entity (H2) checkbox + DE name, entity type (I1), the
  retirement-plan checkbox (I2), the profit/loss/capital percentage table (J),
  the "decrease due to sale/exchange" checkboxes, the liabilities table (K1) and
  the K2/K3 checkboxes, the capital-account analysis (L), the built-in-gain
  Yes/No (M), and the section-704(c) gain/loss boxes (N).
* Part III (boxes 1-23) — every income / deduction / credit line, including the
  code+amount sub-rows for the multi-line boxes (11, 13, 14, 15, 17-20) and the
  box 16 / 22 / 23 checkboxes.

Identity numbers intentionally NOT mapped (left blank for the preparer/human, per
the conservative blank-fill policy shared across this suite):
  * Partnership EIN — Part I A (``...LeftCol[0].f1_6[0]``).
  * Partner SSN or TIN — Part II E (``...LeftCol[0].f1_9[0]``).
  * Disregarded-entity partner TIN — Part II H2 (``...LeftCol[0].f1_11[0]``).
The Schedule K-1 has NO signature line, so there is no wet-ink signature field.

Key conventions
---------------
- Dollar amounts and percentages are written as plain strings ("125000", "50.0")
  — no $, %, or commas. The IRS AcroForm boxes are plain-text, not numeric. A
  loss uses the form's parenthetical/negative convention; the filler writes the
  caller's raw string verbatim.
- The IRS ``f1_NN`` numbering increments in document/tab order (it skips
  f1_67-f1_78 entirely), so field numbers do NOT map 1:1 to box numbers — every
  name below was pinned positionally to its printed box.
- Several Part III boxes hold multiple (code, amount) rows. Row 1 is
  ``boxNN_code`` / ``boxNN_amount``; later rows are suffixed ``_2``, ``_3``,
  ``_4``. Boxes that are a single amount (1-10, 12, 21) expose one key.
- The mutually-exclusive radio groups (G, H1, M, the J "decrease due to" pair)
  are exposed as INDEPENDENT checkbox keys — the caller sets exactly the one it
  means and leaves the rest /Off (conservative). The box 23 widget's on-state is
  ``/4`` (not ``/1``) on this revision — preserved exactly as dumped.
- Conservative blank-fill: a line/box with no input value is simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f1065sk1.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1065sk1_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_HDR = f"{_P1}.Pg1Header[0]"
_CAL = f"{_HDR}.ForCalendarYear[0]"
_LEFT = f"{_P1}.LeftCol[0]"
_JT = f"{_LEFT}.LineJTable[0]"
_K1T = f"{_LEFT}.LineK1Table[0].LineK1Table[0]"
# NB: the L-table parent subform is misspelled "LIneLTable" (capital I, lower n)
# in the actual IRS PDF — preserved verbatim so the name resolves.
_LT = f"{_LEFT}.LIneLTable[0].LineLTable[0]"
_NT = f"{_LEFT}.LineN[0]"
_RC1 = f"{_P1}.RightCol[0].RightCol1[0]"
_RC2 = f"{_P1}.RightCol[0].RightCol2[0]"
_RC = f"{_P1}.RightCol[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
K1_1065_FIELDS: dict[str, str] = {
    # --- Header: tax-year begin/end (calendar year 2025 is preprinted) ---
    "tax_year_begin_month":        f"{_CAL}.f1_1[0]",
    "tax_year_begin_day":          f"{_CAL}.f1_2[0]",
    "tax_year_end_month":          f"{_CAL}.f1_3[0]",
    "tax_year_end_day":            f"{_CAL}.f1_4[0]",
    "tax_year_end_year":           f"{_CAL}.f1_5[0]",

    # --- Part I: Information About the Partnership ---
    # A (partnership EIN, f1_6) intentionally omitted — see module docstring.
    "partnership_name_address":    f"{_LEFT}.f1_7[0]",   # B (multiline)
    "irs_center":                  f"{_LEFT}.f1_8[0]",   # C

    # --- Part II: Information About the Partner ---
    # E (partner SSN/TIN, f1_9) intentionally omitted — see module docstring.
    "partner_name_address":        f"{_LEFT}.f1_10[0]",  # F (multiline)
    # H2 DE TIN (f1_11) intentionally omitted; the DE name IS fillable:
    "de_partner_name":             f"{_LEFT}.f1_12[0]",  # H2 name
    "partner_entity_type":         f"{_LEFT}.f1_13[0]",  # I1
    # J — Partner's share of profit, loss, and capital (begin / end percentages)
    "profit_share_beginning":      f"{_JT}.Profit[0].f1_14[0]",
    "profit_share_ending":         f"{_JT}.Profit[0].f1_15[0]",
    "loss_share_beginning":        f"{_JT}.Loss[0].f1_16[0]",
    "loss_share_ending":           f"{_JT}.Loss[0].f1_17[0]",
    "capital_share_beginning":     f"{_JT}.Capital[0].f1_18[0]",
    "capital_share_ending":        f"{_JT}.Capital[0].f1_19[0]",
    # K1 — Partner's share of liabilities (begin / end)
    "nonrecourse_beginning":             f"{_K1T}.Nonrecourse[0].f1_20[0]",
    "nonrecourse_ending":                f"{_K1T}.Nonrecourse[0].f1_21[0]",
    "qualified_nonrecourse_beginning":   f"{_K1T}.QualifiedNonrecourse[0].f1_22[0]",
    "qualified_nonrecourse_ending":      f"{_K1T}.QualifiedNonrecourse[0].f1_23[0]",
    "recourse_beginning":                f"{_K1T}.Recourse[0].f1_24[0]",
    "recourse_ending":                   f"{_K1T}.Recourse[0].f1_25[0]",
    # L — Partner's Capital Account Analysis
    "capital_beginning":                 f"{_LT}.Row1[0].f1_26[0]",
    "capital_contributed":               f"{_LT}.Row2[0].f1_27[0]",
    "capital_net_income":                f"{_LT}.Row3[0].f1_28[0]",
    "capital_other_increase_decrease":   f"{_LT}.Row4[0].f1_29[0]",
    "capital_withdrawals":               f"{_LT}.Row5[0].f1_30[0]",
    "capital_ending":                    f"{_LT}.Row6[0].f1_31[0]",
    # N — Partner's Share of Net Unrecognized Section 704(c) Gain or (Loss)
    "section_704c_beginning":            f"{_NT}.f1_32[0]",
    "section_704c_ending":               f"{_NT}.f1_33[0]",

    # --- Part III, left sub-column (boxes 1-13) ---
    "box1_ordinary_business_income":     f"{_RC1}.f1_34[0]",   # 1
    "box2_net_rental_real_estate":       f"{_RC1}.f1_35[0]",   # 2
    "box3_other_net_rental":             f"{_RC1}.f1_36[0]",   # 3
    "box4a_guaranteed_pmt_services":     f"{_RC1}.f1_37[0]",   # 4a
    "box4b_guaranteed_pmt_capital":      f"{_RC1}.f1_38[0]",   # 4b
    "box4c_total_guaranteed_pmt":        f"{_RC1}.f1_39[0]",   # 4c
    "box5_interest_income":              f"{_RC1}.f1_40[0]",   # 5
    "box6a_ordinary_dividends":          f"{_RC1}.f1_41[0]",   # 6a
    "box6b_qualified_dividends":         f"{_RC1}.f1_42[0]",   # 6b
    "box6c_dividend_equivalents":        f"{_RC1}.f1_43[0]",   # 6c
    "box7_royalties":                    f"{_RC1}.f1_44[0]",   # 7
    "box8_net_short_term_capital_gain":  f"{_RC1}.f1_45[0]",   # 8
    "box9a_net_long_term_capital_gain":  f"{_RC1}.f1_46[0]",   # 9a
    "box9b_collectibles_28_gain":        f"{_RC1}.f1_47[0]",   # 9b
    "box9c_unrecaptured_1250_gain":      f"{_RC1}.f1_48[0]",   # 9c
    "box10_net_section_1231_gain":       f"{_RC1}.f1_49[0]",   # 10
    # 11 Other income (loss) — two (code, amount) rows
    "box11_code":                        f"{_RC1}.f1_50[0]",
    "box11_amount":                      f"{_RC1}.f1_51[0]",
    "box11_code_2":                      f"{_RC1}.f1_52[0]",
    "box11_amount_2":                    f"{_RC1}.f1_53[0]",
    # 12 Section 179 deduction — single amount
    "box12_section_179":                 f"{_RC1}.f1_54[0]",
    # 13 Other deductions — three (code, amount) rows
    "box13_code":                        f"{_RC1}.Line13[0]",
    "box13_amount":                      f"{_RC1}.f1_55[0]",
    "box13_code_2":                      f"{_RC1}.f1_56[0]",
    "box13_amount_2":                    f"{_RC1}.f1_57[0]",
    "box13_code_3":                      f"{_RC1}.f1_58[0]",
    "box13_amount_3":                    f"{_RC1}.f1_59[0]",

    # --- Part III, right sub-column (boxes 14-21) ---
    # 14 Self-employment earnings (loss) — two (code, amount) rows
    "box14_code":                        f"{_RC2}.Line14[0]",
    "box14_amount":                      f"{_RC2}.f1_60[0]",
    "box14_code_2":                      f"{_RC2}.f1_61[0]",
    "box14_amount_2":                    f"{_RC2}.f1_62[0]",
    # 15 Credits — two (code, amount) rows
    "box15_code":                        f"{_RC2}.Line15[0]",
    "box15_amount":                      f"{_RC2}.f1_63[0]",
    "box15_code_2":                      f"{_RC2}.f1_64[0]",
    "box15_amount_2":                    f"{_RC2}.f1_65[0]",
    # 16 (Schedule K-3 attached) is a checkbox — see K1_1065_CHECKBOX_FIELDS.
    # 17 Alternative minimum tax (AMT) items — three (code, amount) rows
    "box17_code":                        f"{_RC2}.Line17[0]",
    "box17_amount":                      f"{_RC2}.f1_79[0]",
    "box17_code_2":                      f"{_RC2}.f1_80[0]",
    "box17_amount_2":                    f"{_RC2}.f1_81[0]",
    "box17_code_3":                      f"{_RC2}.f1_82[0]",
    "box17_amount_3":                    f"{_RC2}.f1_83[0]",
    # 18 Tax-exempt income and nondeductible expenses — three (code, amount) rows
    "box18_code":                        f"{_RC2}.Line18[0]",
    "box18_amount":                      f"{_RC2}.f1_84[0]",
    "box18_code_2":                      f"{_RC2}.f1_85[0]",
    "box18_amount_2":                    f"{_RC2}.f1_86[0]",
    "box18_code_3":                      f"{_RC2}.f1_87[0]",
    "box18_amount_3":                    f"{_RC2}.f1_88[0]",
    # 19 Distributions — two (code, amount) rows
    "box19_code":                        f"{_RC2}.Line19[0]",
    "box19_amount":                      f"{_RC2}.f1_89[0]",
    "box19_code_2":                      f"{_RC2}.f1_90[0]",
    "box19_amount_2":                    f"{_RC2}.f1_91[0]",
    # 20 Other information — four (code, amount) rows
    "box20_code":                        f"{_RC2}.Line20[0]",
    "box20_amount":                      f"{_RC2}.f1_92[0]",
    "box20_code_2":                      f"{_RC2}.f1_93[0]",
    "box20_amount_2":                    f"{_RC2}.f1_94[0]",
    "box20_code_3":                      f"{_RC2}.f1_95[0]",
    "box20_amount_3":                    f"{_RC2}.f1_96[0]",
    "box20_code_4":                      f"{_RC2}.f1_97[0]",
    "box20_amount_4":                    f"{_RC2}.f1_98[0]",
    # 21 Foreign taxes paid or accrued — single amount
    "box21_foreign_taxes":               f"{_RC2}.f1_66[0]",
}

# ---------------------------------------------------------------------------
# Checkbox / radio-option fields: logical key -> (AcroForm field name, on-state).
# Each is set only when the caller passes a truthy value for that key; the box
# is otherwise left /Off (conservative). Mutually-exclusive groups (G, H1, M, the
# J "decrease due to" pair) are exposed as independent keys — set exactly the one
# you mean. On-states are the bare state string the on-state NameObject is built
# from (e.g. "1", "2", "4"); the renderer prepends the slash.
# ---------------------------------------------------------------------------
K1_1065_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Header
    "final_k1":                       (f"{_HDR}.c1_1[0]", "1"),
    "amended_k1":                     (f"{_HDR}.c1_2[0]", "1"),
    # Part I — D
    "ptp":                            (f"{_LEFT}.c1_3[0]", "1"),
    # Part II — G (general / limited partner)
    "general_partner":                (f"{_LEFT}.c1_4[0]", "1"),
    "limited_partner":                (f"{_LEFT}.c1_4[1]", "2"),
    # Part II — H1 (domestic / foreign partner)
    "domestic_partner":               (f"{_LEFT}.c1_5[0]", "1"),
    "foreign_partner":                (f"{_LEFT}.c1_5[1]", "2"),
    # Part II — H2 (disregarded-entity partner)
    "de_partner":                     (f"{_LEFT}.c1_6[0]", "1"),
    # Part II — I2 (retirement plan)
    "retirement_plan":                (f"{_LEFT}.c1_7[0]", "1"),
    # Part II — J "Check if decrease is due to: Sale / Exchange of interest"
    "decrease_due_to_sale":           (f"{_LEFT}.c1_8[0]", "1"),
    "decrease_due_to_exchange":       (f"{_LEFT}.c1_8[1]", "2"),
    # Part II — K2 / K3
    "k1_includes_lower_tier":         (f"{_LEFT}.LineK1Table[0].c1_9[0]", "1"),
    "liability_subject_to_guarantees": (f"{_LEFT}.LineK1Table[0].c1_10[0]", "1"),
    # Part II — M (built-in gain (loss) contributed? Yes / No)
    "built_in_gain_yes":              (f"{_LEFT}.c1_11[0]", "1"),
    "built_in_gain_no":               (f"{_LEFT}.c1_11[1]", "2"),
    # Part III — 16 (Schedule K-3 is attached if checked)
    "box16_k3_attached":              (f"{_RC2}.c1_12[0]", "1"),
    # Part III — 22 / 23 (more than one activity for at-risk / passive purposes).
    # Box 23's on-state is /4 on this revision (dumped verbatim).
    "box22_multiple_at_risk":         (f"{_RC}.c1_13[0]", "1"),
    "box23_multiple_passive":         (f"{_RC}.c1_14[0]", "4"),
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Schedule K-1 fields.
VALID_K1_1065_FIELDS: frozenset[str] = frozenset(
    list(K1_1065_FIELDS.values())
    + [v[0] for v in K1_1065_CHECKBOX_FIELDS.values()]
)
