"""IRS Form 1125-E (Compensation of Officers) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1125e_2024.pdf``, **Tax Year 2024** revision) with ``pypdf``
(``PdfReader().get_fields()``) and pinning every terminal widget's /Rect against
the printed line labels — a positional audit, because the IRS ``f{page}_{NN}``
numbering increments in document/tab order, NOT line order. Form 1125-E is a
modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so it MUST be filled
via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker),
not the older page-based ``_fill_pdf_fields``.

Layout
------
The form has a header (corporation name, EIN) and a table of officers (up to 20).
Each officer row has 6 columns: name, SSN, % time, % common stock, % preferred
stock, compensation. The form ends with three total lines.

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Dollar amounts are written as plain
strings ("12500" — no $ or commas). Conservative blank-fill: a line with no
input value is simply not written.

CPA/EA verification note
------------------------
These names match the TY2024 f1125e_2024.pdf as distributed on irs.gov. The
field names are stable across recent revisions, but if a later (or prior-year
``f1125e_{year}.pdf``) PDF is dropped into ``data/irs_forms/``, re-run the dump
and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F1125E_FIELDS: dict[str, str] = {
    # --- Header ---
    "corporation_name": f"{_P1}.f1_1[0]",  # Corporation name
    "ein":             f"{_P1}.f1_2[0]",  # Employer identification number

    # --- Officer Table (20 rows x 6 columns) ---
    # Row 1
    "officer_1_name":      f"{_P1}.f1_3[0]",   # (a) Name
    "officer_1_ssn":       f"{_P1}.f1_4[0]",   # (b) SSN
    "officer_1_pct_time":  f"{_P1}.f1_5[0]",   # (c) % of time devoted to business
    "officer_1_common":    f"{_P1}.f1_6[0]",   # (d) % common stock owned
    "officer_1_preferred": f"{_P1}.f1_7[0]",   # (e) % preferred stock owned
    "officer_1_comp":      f"{_P1}.f1_8[0]",   # (f) Amount of compensation
    # Row 2
    "officer_2_name":      f"{_P1}.f1_9[0]",
    "officer_2_ssn":       f"{_P1}.f1_10[0]",
    "officer_2_pct_time":  f"{_P1}.f1_11[0]",
    "officer_2_common":    f"{_P1}.f1_12[0]",
    "officer_2_preferred": f"{_P1}.f1_13[0]",
    "officer_2_comp":      f"{_P1}.f1_14[0]",
    # Row 3
    "officer_3_name":      f"{_P1}.f1_15[0]",
    "officer_3_ssn":       f"{_P1}.f1_16[0]",
    "officer_3_pct_time":  f"{_P1}.f1_17[0]",
    "officer_3_common":    f"{_P1}.f1_18[0]",
    "officer_3_preferred": f"{_P1}.f1_19[0]",
    "officer_3_comp":      f"{_P1}.f1_20[0]",
    # Row 4
    "officer_4_name":      f"{_P1}.f1_21[0]",
    "officer_4_ssn":       f"{_P1}.f1_22[0]",
    "officer_4_pct_time":  f"{_P1}.f1_23[0]",
    "officer_4_common":    f"{_P1}.f1_24[0]",
    "officer_4_preferred": f"{_P1}.f1_25[0]",
    "officer_4_comp":      f"{_P1}.f1_26[0]",
    # Row 5
    "officer_5_name":      f"{_P1}.f1_27[0]",
    "officer_5_ssn":       f"{_P1}.f1_28[0]",
    "officer_5_pct_time":  f"{_P1}.f1_29[0]",
    "officer_5_common":    f"{_P1}.f1_30[0]",
    "officer_5_preferred": f"{_P1}.f1_31[0]",
    "officer_5_comp":      f"{_P1}.f1_32[0]",
    # Row 6
    "officer_6_name":      f"{_P1}.f1_33[0]",
    "officer_6_ssn":       f"{_P1}.f1_34[0]",
    "officer_6_pct_time":  f"{_P1}.f1_35[0]",
    "officer_6_common":    f"{_P1}.f1_36[0]",
    "officer_6_preferred": f"{_P1}.f1_37[0]",
    "officer_6_comp":      f"{_P1}.f1_38[0]",
    # Row 7
    "officer_7_name":      f"{_P1}.f1_39[0]",
    "officer_7_ssn":       f"{_P1}.f1_40[0]",
    "officer_7_pct_time":  f"{_P1}.f1_41[0]",
    "officer_7_common":    f"{_P1}.f1_42[0]",
    "officer_7_preferred": f"{_P1}.f1_43[0]",
    "officer_7_comp":      f"{_P1}.f1_44[0]",
    # Row 8
    "officer_8_name":      f"{_P1}.f1_45[0]",
    "officer_8_ssn":       f"{_P1}.f1_46[0]",
    "officer_8_pct_time":  f"{_P1}.f1_47[0]",
    "officer_8_common":    f"{_P1}.f1_48[0]",
    "officer_8_preferred": f"{_P1}.f1_49[0]",
    "officer_8_comp":      f"{_P1}.f1_50[0]",
    # Row 9
    "officer_9_name":      f"{_P1}.f1_51[0]",
    "officer_9_ssn":       f"{_P1}.f1_52[0]",
    "officer_9_pct_time":  f"{_P1}.f1_53[0]",
    "officer_9_common":    f"{_P1}.f1_54[0]",
    "officer_9_preferred": f"{_P1}.f1_55[0]",
    "officer_9_comp":      f"{_P1}.f1_56[0]",
    # Row 10
    "officer_10_name":     f"{_P1}.f1_57[0]",
    "officer_10_ssn":      f"{_P1}.f1_58[0]",
    "officer_10_pct_time": f"{_P1}.f1_59[0]",
    "officer_10_common":   f"{_P1}.f1_60[0]",
    "officer_10_preferred":f"{_P1}.f1_61[0]",
    "officer_10_comp":     f"{_P1}.f1_62[0]",
    # Row 11
    "officer_11_name":     f"{_P1}.f1_63[0]",
    "officer_11_ssn":      f"{_P1}.f1_64[0]",
    "officer_11_pct_time": f"{_P1}.f1_65[0]",
    "officer_11_common":   f"{_P1}.f1_66[0]",
    "officer_11_preferred":f"{_P1}.f1_67[0]",
    "officer_11_comp":     f"{_P1}.f1_68[0]",
    # Row 12
    "officer_12_name":     f"{_P1}.f1_69[0]",
    "officer_12_ssn":      f"{_P1}.f1_70[0]",
    "officer_12_pct_time": f"{_P1}.f1_71[0]",
    "officer_12_common":   f"{_P1}.f1_72[0]",
    "officer_12_preferred":f"{_P1}.f1_73[0]",
    "officer_12_comp":     f"{_P1}.f1_74[0]",
    # Row 13
    "officer_13_name":     f"{_P1}.f1_75[0]",
    "officer_13_ssn":      f"{_P1}.f1_76[0]",
    "officer_13_pct_time": f"{_P1}.f1_77[0]",
    "officer_13_common":   f"{_P1}.f1_78[0]",
    "officer_13_preferred":f"{_P1}.f1_79[0]",
    "officer_13_comp":     f"{_P1}.f1_80[0]",
    # Row 14
    "officer_14_name":     f"{_P1}.f1_81[0]",
    "officer_14_ssn":      f"{_P1}.f1_82[0]",
    "officer_14_pct_time": f"{_P1}.f1_83[0]",
    "officer_14_common":   f"{_P1}.f1_84[0]",
    "officer_14_preferred":f"{_P1}.f1_85[0]",
    "officer_14_comp":     f"{_P1}.f1_86[0]",
    # Row 15
    "officer_15_name":     f"{_P1}.f1_87[0]",
    "officer_15_ssn":      f"{_P1}.f1_88[0]",
    "officer_15_pct_time": f"{_P1}.f1_89[0]",
    "officer_15_common":   f"{_P1}.f1_90[0]",
    "officer_15_preferred":f"{_P1}.f1_91[0]",
    "officer_15_comp":     f"{_P1}.f1_92[0]",
    # Row 16
    "officer_16_name":     f"{_P1}.f1_93[0]",
    "officer_16_ssn":      f"{_P1}.f1_94[0]",
    "officer_16_pct_time": f"{_P1}.f1_95[0]",
    "officer_16_common":   f"{_P1}.f1_96[0]",
    "officer_16_preferred":f"{_P1}.f1_97[0]",
    "officer_16_comp":     f"{_P1}.f1_98[0]",
    # Row 17
    "officer_17_name":     f"{_P1}.f1_99[0]",
    "officer_17_ssn":      f"{_P1}.f1_100[0]",
    "officer_17_pct_time": f"{_P1}.f1_101[0]",
    "officer_17_common":   f"{_P1}.f1_102[0]",
    "officer_17_preferred":f"{_P1}.f1_103[0]",
    "officer_17_comp":     f"{_P1}.f1_104[0]",
    # Row 18
    "officer_18_name":     f"{_P1}.f1_105[0]",
    "officer_18_ssn":      f"{_P1}.f1_106[0]",
    "officer_18_pct_time": f"{_P1}.f1_107[0]",
    "officer_18_common":   f"{_P1}.f1_108[0]",
    "officer_18_preferred":f"{_P1}.f1_109[0]",
    "officer_18_comp":     f"{_P1}.f1_110[0]",
    # Row 19
    "officer_19_name":     f"{_P1}.f1_111[0]",
    "officer_19_ssn":      f"{_P1}.f1_112[0]",
    "officer_19_pct_time": f"{_P1}.f1_113[0]",
    "officer_19_common":   f"{_P1}.f1_114[0]",
    "officer_19_preferred":f"{_P1}.f1_115[0]",
    "officer_19_comp":     f"{_P1}.f1_116[0]",
    # Row 20
    "officer_20_name":     f"{_P1}.f1_117[0]",
    "officer_20_ssn":      f"{_P1}.f1_118[0]",
    "officer_20_pct_time": f"{_P1}.f1_119[0]",
    "officer_20_common":   f"{_P1}.f1_120[0]",
    "officer_20_preferred":f"{_P1}.f1_121[0]",
    "officer_20_comp":     f"{_P1}.f1_122[0]",

    # --- Total Lines ---
    "line_2_total": f"{_P1}.f1_123[0]",  # Line 2 — Total compensation of officers
    "line_3_claimed": f"{_P1}.f1_124[0]",  # Line 3 — Compensation claimed elsewhere
    "line_4_net": f"{_P1}.f1_125[0]",  # Line 4 — Net compensation (line 2 - line 3)
}

# Identity items the renderer never auto-fills, even if supplied — the taxpayer
# enters these themselves.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    "officer_1_ssn", "officer_2_ssn", "officer_3_ssn", "officer_4_ssn",
    "officer_5_ssn", "officer_6_ssn", "officer_7_ssn", "officer_8_ssn",
    "officer_9_ssn", "officer_10_ssn", "officer_11_ssn", "officer_12_ssn",
    "officer_13_ssn", "officer_14_ssn", "officer_15_ssn", "officer_16_ssn",
    "officer_17_ssn", "officer_18_ssn", "officer_19_ssn", "officer_20_ssn",
})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 1125-E fields.
VALID_F1125E_FIELDS: frozenset[str] = frozenset(F1125E_FIELDS.values())
