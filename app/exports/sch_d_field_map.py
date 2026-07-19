"""IRS Schedule D (Form 1040) — Capital Gains and Losses — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1040sd.pdf``, **Tax Year 2025** revision, OMB 1545-0074) with
``pypdf`` (``PdfReader().get_fields()``) and cross-referencing every terminal
field's widget /Rect against the printed line labels (a positional audit — the
IRS XFA tooltips are empty on this form). Schedule D is a modern XFA-hybrid
AcroForm (``topmostSubform[0]`` naming, like the W-4P / 1040), so it MUST be
filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe walker that bakes
visible, flattened values), not the older page-based ``_fill_pdf_fields``.

Pattern mirrors ``w4p_field_map.py`` / ``f1040_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.

This map covers identity + the four transaction-summary rows in each part
(columns d/e/g/h) + the carry-in lines + the Part III summary lines + the four
Yes/No questions. It is the honest "fill what the taxpayer's Form 8949 / 1099-B /
1099-DA / Schedule(s) K-1 supply onto the official form" surface — it does NOT
compute the return (the netting math, the 28% Rate Gain / Unrecaptured §1250 /
Capital Loss Carryover worksheets are the preparer's input).

Column layout (verified positionally — x=288 / 360 / 432 / 504)
--------------------------------------------------------------
Each transaction row (1a, 1b, 2, 3 in Part I; 8a, 8b, 9, 10 in Part II) has four
boxes, left to right:
  (d) Proceeds (sales price)            -> ``*_proceeds``
  (e) Cost (or other basis)            -> ``*_cost``
  (g) Adjustments to gain/loss (8949)  -> ``*_adjustments``
  (h) Gain or (loss)                   -> ``*_gain``

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas). The
  IRS AcroForm boxes are plain-text. The renderer writes the caller's raw value.
- Loss lines 6, 14, and 21 are PRE-PRINTED with parentheses on the form, so the
  caller passes the positive magnitude of the loss (e.g. "3000", not "-3000").
- The IRS ``f1_NN`` numbering increments in document/tab order, so field numbers
  do NOT map 1:1 to printed line numbers — every name below was pinned
  positionally to its printed line/column.
- SSN ("Your social security number", field ``f1_2[0]``) and the signature are
  intentionally NOT in this map — they are left blank for the taxpayer to enter.
- Conservative blank-fill: a line with no input value is simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f1040sd.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1040sd_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"
_PI = f"{_P1}.Table_PartI[0]"
_PII = f"{_P1}.Table_PartII[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
SCH_D_FIELDS: dict[str, str] = {
    # --- Identity (page-1 header) ---
    # "Name(s) shown on return". SSN (f1_2[0]) is intentionally left blank.
    "taxpayer_name": f"{_P1}.f1_1[0]",

    # === Part I — Short-Term Capital Gains and Losses (held one year or less) ===
    # Line 1a — totals for 1099-B/1099-DA transactions, basis reported, no adj.
    "line1a_proceeds":     f"{_PI}.Row1a[0].f1_3[0]",
    "line1a_cost":         f"{_PI}.Row1a[0].f1_4[0]",
    "line1a_adjustments":  f"{_PI}.Row1a[0].f1_5[0]",
    "line1a_gain":         f"{_PI}.Row1a[0].f1_6[0]",
    # Line 1b — totals for Form(s) 8949 with Box A (or Box G) checked.
    "line1b_proceeds":     f"{_PI}.Row1b[0].f1_7[0]",
    "line1b_cost":         f"{_PI}.Row1b[0].f1_8[0]",
    "line1b_adjustments":  f"{_PI}.Row1b[0].f1_9[0]",
    "line1b_gain":         f"{_PI}.Row1b[0].f1_10[0]",
    # Line 2 — totals for Form(s) 8949 with Box B (or Box H) checked.
    "line2_proceeds":      f"{_PI}.Row2[0].f1_11[0]",
    "line2_cost":          f"{_PI}.Row2[0].f1_12[0]",
    "line2_adjustments":   f"{_PI}.Row2[0].f1_13[0]",
    "line2_gain":          f"{_PI}.Row2[0].f1_14[0]",
    # Line 3 — totals for Form(s) 8949 with Box C (or Box I) checked.
    "line3_proceeds":      f"{_PI}.Row3[0].f1_15[0]",
    "line3_cost":          f"{_PI}.Row3[0].f1_16[0]",
    "line3_adjustments":   f"{_PI}.Row3[0].f1_17[0]",
    "line3_gain":          f"{_PI}.Row3[0].f1_18[0]",
    # Carry-in lines (column h only).
    "line4_gain":              f"{_P1}.f1_19[0]",  # ST gain from 6252; ST g/(l) from 4684/6781/8824
    "line5_gain":              f"{_P1}.f1_20[0]",  # Net ST g/(l) from K-1 (p'ships/S corps/estates/trusts)
    "line6_st_loss_carryover": f"{_P1}.f1_21[0]",  # Short-term capital loss carryover (pre-printed parens)
    "line7_net_short_term":    f"{_P1}.f1_22[0]",  # Net ST capital gain or (loss) — combine 1a-6 col (h)

    # === Part II — Long-Term Capital Gains and Losses (held more than one year) ===
    # Line 8a — totals for 1099-B/1099-DA transactions, basis reported, no adj.
    "line8a_proceeds":     f"{_PII}.Row8a[0].f1_23[0]",
    "line8a_cost":         f"{_PII}.Row8a[0].f1_24[0]",
    "line8a_adjustments":  f"{_PII}.Row8a[0].f1_25[0]",
    "line8a_gain":         f"{_PII}.Row8a[0].f1_26[0]",
    # Line 8b — totals for Form(s) 8949 with Box D checked.
    "line8b_proceeds":     f"{_PII}.Row8b[0].f1_27[0]",
    "line8b_cost":         f"{_PII}.Row8b[0].f1_28[0]",
    "line8b_adjustments":  f"{_PII}.Row8b[0].f1_29[0]",
    "line8b_gain":         f"{_PII}.Row8b[0].f1_30[0]",
    # Line 9 — totals for Form(s) 8949 with Box E checked.
    "line9_proceeds":      f"{_PII}.Row9[0].f1_31[0]",
    "line9_cost":          f"{_PII}.Row9[0].f1_32[0]",
    "line9_adjustments":   f"{_PII}.Row9[0].f1_33[0]",
    "line9_gain":          f"{_PII}.Row9[0].f1_34[0]",
    # Line 10 — totals for Form(s) 8949 with Box F checked.
    "line10_proceeds":     f"{_PII}.Row10[0].f1_35[0]",
    "line10_cost":         f"{_PII}.Row10[0].f1_36[0]",
    "line10_adjustments":  f"{_PII}.Row10[0].f1_37[0]",
    "line10_gain":         f"{_PII}.Row10[0].f1_38[0]",
    # Carry-in lines (column h only).
    "line11_gain":               f"{_P1}.f1_39[0]",  # Gain from 4797 Pt I; LT gain from 2439/6252; 4684/6781/8824
    "line12_gain":               f"{_P1}.f1_40[0]",  # Net LT g/(l) from K-1
    "line13_cap_gain_distrib":   f"{_P1}.f1_41[0]",  # Capital gain distributions
    "line14_lt_loss_carryover":  f"{_P1}.f1_42[0]",  # Long-term capital loss carryover (pre-printed parens)
    "line15_net_long_term":      f"{_P1}.f1_43[0]",  # Net LT capital gain or (loss) — combine 8a-14 col (h)

    # === Page 2 — Part III Summary ===
    "line16_total":            f"{_P2}.f2_1[0]",  # Combine lines 7 and 15
    "line18_28pct_rate_gain":  f"{_P2}.f2_2[0]",  # 28% Rate Gain Worksheet, line 7
    "line19_unrecap_1250_gain": f"{_P2}.f2_3[0]", # Unrecaptured Section 1250 Gain Worksheet, line 18
    "line21_allowed_loss":     f"{_P2}.f2_4[0]",  # Allowed loss, capped at (3,000) / (1,500) MFS (pre-printed parens)
}

# ---------------------------------------------------------------------------
# Yes/No questions: logical question -> {choice: (field name, on-state)}.
# Each Yes and No is an INDEPENDENT /Btn checkbox (on-states /1 and /2). Set at
# most one choice per question; the other remains /Off (conservative).
# ---------------------------------------------------------------------------
SCH_D_CHOICE_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    # Top of page 1: "Did you dispose of any investment(s) in a qualified
    # opportunity fund during the tax year?"
    "qof_disposed": {
        "yes": (f"{_P1}.c1_1[0]", "1"),
        "no":  (f"{_P1}.c1_1[1]", "2"),
    },
    # Line 17: "Are lines 15 and 16 both gains?"
    "line17_both_gains": {
        "yes": (f"{_P2}.c2_1[0]", "1"),
        "no":  (f"{_P2}.c2_1[1]", "2"),
    },
    # Line 20: "Are lines 18 and 19 both zero or blank and you are not filing
    # Form 4952?"
    "line20_both_zero_not_filing_4952": {
        "yes": (f"{_P2}.c2_2[0]", "1"),
        "no":  (f"{_P2}.c2_2[1]", "2"),
    },
    # Line 22: "Do you have qualified dividends on Form 1040, line 3a?"
    "line22_qualified_dividends": {
        "yes": (f"{_P2}.c2_3[0]", "1"),
        "no":  (f"{_P2}.c2_3[1]", "2"),
    },
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Schedule D fields.
VALID_SCH_D_FIELDS: frozenset[str] = frozenset(
    list(SCH_D_FIELDS.values())
    + [fld for q in SCH_D_CHOICE_FIELDS.values() for (fld, _on) in q.values()]
)
