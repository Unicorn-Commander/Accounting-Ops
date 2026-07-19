"""IRS Form W-4P AcroForm field map — page-1 certificate fields only.

The certificate the taxpayer gives to the PAYER is page 1; pages 2-4 are
instructions + worksheets the taxpayer keeps. We fill only the page-1 face
fields. Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f-w4p.pdf``, TY2026; the Step 1-4 face names are shared with
the 2024/2025 forms — only the page-3/4 deductions worksheet differs by year,
and we do not fill that scratch worksheet onto the certificate).

Pattern mirrors ``form_1120_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Filing status is THREE independent
checkbox fields that share the base name ``c1_1`` (verified: each is a separate
/Btn with on-states /1, /2, /3 respectively) — set exactly one, leave the others
``/Off``.
"""
from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# Plain-text fields: logical key -> AcroForm field name (/Tx).
W4P_FIELDS: dict[str, str] = {
    # Step 1(a) — identity
    "first_name_mi":   f"{_P1}.Step1a[0].f1_01[0]",
    "last_name":       f"{_P1}.Step1a[0].f1_02[0]",
    "address":         f"{_P1}.Step1a[0].f1_03[0]",
    "city_state_zip":  f"{_P1}.Step1a[0].f1_04[0]",
    "ssn":             f"{_P1}.f1_05[0]",
    # Step 2 — income from other pensions/annuities/jobs (taxpayer declaration)
    "step2a":          f"{_P1}.f1_06[0]",
    "step2b":          f"{_P1}.f1_07[0]",
    "step2c":          f"{_P1}.f1_08[0]",
    # Step 3 — claim dependents / other credits
    "step3a_child":    f"{_P1}.Step3_ReadOrder[0].f1_09[0]",
    "step3b_other":    f"{_P1}.Step3_ReadOrder[0].f1_10[0]",
    "step3c_credits":  f"{_P1}.Step3_ReadOrder[0].f1_11[0]",
    "step3_total":     f"{_P1}.f1_12[0]",
    # Step 4 — other adjustments
    "step4a_income":     f"{_P1}.f1_13[0]",
    "step4b_deductions": f"{_P1}.f1_14[0]",
    "step4c_extra":      f"{_P1}.f1_15[0]",
}

# Filing-status checkboxes: logical status -> (AcroForm field name, on-state).
# Exactly one is set; the others remain /Off (conservative).
W4P_FILING_STATUS_FIELDS: dict[str, tuple[str, str]] = {
    "single_or_mfs":     (f"{_P1}.c1_1[0]", "1"),
    "mfj_or_qss":        (f"{_P1}.c1_1[1]", "2"),
    "head_of_household": (f"{_P1}.c1_1[2]", "3"),
}

# "Elect no withholding" checkbox (new on the 2026 redesign). on-state /1.
W4P_NO_WITHHOLDING_FIELD: tuple[str, str] = (f"{_P1}.c1_2[0]", "1")

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known W-4P fields.
VALID_W4P_FIELDS: frozenset[str] = frozenset(
    list(W4P_FIELDS.values())
    + [v[0] for v in W4P_FILING_STATUS_FIELDS.values()]
    + [W4P_NO_WITHHOLDING_FIELD[0]]
)
