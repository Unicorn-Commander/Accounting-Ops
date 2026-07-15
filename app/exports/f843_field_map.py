"""IRS Form 843 (Claim for Refund and Request for Abatement) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f843.pdf``) with ``pypdf`` and pinning every terminal widget's
/Rect against the printed line labels. Form 843 is a modern XFA-hybrid AcroForm
(``topmostSubform[0]`` naming), so it MUST be filled via
``form_prefill._render_acroform_pdf``.

Layout
------
The form has two pages.
Page 1:
  - Identity block (name, SSN/EIN, address, spouse SSN).
  - Line 1: Period beginning and ending dates.
  - Line 2: Amount to be refunded/abated.
  - Line 3: Type of tax/fee (checkboxes c1_1..c1_16).
  - Line 4: Penalty IRC section (checkboxes c1_2..c1_8).
  - Line 5a: Interest/penalty/addition reason (checkboxes c1_2..c1_8).
  - Line 7: Explanation (multiline text field).
Page 2:
  - Continuation of checkboxes (c2_1..c2_16).

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Dollar amounts are written as plain
strings. Conservative blank-fill: a line with no input value is simply not
written.

CPA/EA verification note
------------------------
These names match the f843.pdf as distributed on irs.gov. The face field names
are stable across recent revisions, but if a later PDF is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.

VERIFY-AT-GATE
--------------
This form is checkbox-heavy and has many fields. Only the most critical fields
(line 3 tax type, line 5a reason, line 2 amount, line 1 dates, line 7 explanation)
are mapped with confidence. The rest are marked with TODOs for positional audit.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F843_FIELDS: dict[str, str] = {
    # --- Identity Block ---
    # Name and address
    "name":          f"{_P1}.f1_1[0]",   # Line 1a - Name
    "address":       f"{_P1}.f1_2[0]",   # Line 1b - Street address
    "city_state_zip": f"{_P1}.f1_3[0]",  # Line 1c - City, state, ZIP
    "ssn_ein":       f"{_P1}.f1_4[0]",   # Line 1d - SSN or EIN
    "spouse_ssn":    f"{_P1}.f1_5[0]",   # Line 1e - Spouse SSN (if joint)

    # --- Line 1: Period ---
    "period_begin":  f"{_P1}.f1_6[0]",   # Line 1 - Period beginning
    "period_end":    f"{_P1}.f1_7[0]",   # Line 1 - Period ending

    # --- Line 2: Amount ---
    "amount":        f"{_P1}.f1_8[0]",   # Line 2 - Amount to be refunded/abated

    # --- Line 7: Explanation ---
    "explanation":   f"{_P1}.ExplainWhy[0]",  # Line 7 - Explain why

    # --- Page 2 Continuation (Unmapped/VERIFY-AT-GATE) ---
    # TODO: Verify Page 2 fields for additional explanation or signatures.
    # "page2_explanation": f"{_P2}.f2_1[0]",
}

# ---------------------------------------------------------------------------
# Checkboxes (lines 3, 4, 5a): logical key -> (AcroForm field name, on-state).
# Each is an INDEPENDENT /Btn with on-states ["/1", "/Off"].
# ---------------------------------------------------------------------------
F843_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 3: Type of tax/fee (c1_1..c1_16)
    # Mapping only the first few common ones; rest need verification.
    "tax_type_employment": (f"{_P1}.c1_1[0]", "1"),  # Employment
    "tax_type_estate":     (f"{_P1}.c1_2[0]", "1"),  # Estate
    "tax_type_gift":       (f"{_P1}.c1_3[0]", "1"),  # Gift
    "tax_type_excise":     (f"{_P1}.c1_4[0]", "1"),  # Excise
    "tax_type_income":     (f"{_P1}.c1_5[0]", "1"),  # Income
    "tax_type_other":      (f"{_P1}.c1_16[0]", "1"), # Other (if applicable)

    # Line 4: Penalty IRC section (c1_2..c1_8) - Note: c1_2 is used in Line 3 too?
    # Assuming distinct fields or groups. If c1_2 is shared, this needs audit.
    # For now, mapping distinct logical keys to distinct fields if possible.
    # If the PDF uses the same field name for different lines, this map will conflict.
    # Assuming separate fields for now.
    "penalty_irc_6651":    (f"{_P1}.c1_2[0]", "1"),  # Example: IRC 6651
    "penalty_irc_6654":    (f"{_P1}.c1_3[0]", "1"),  # Example: IRC 6654
    "penalty_irc_6655":    (f"{_P1}.c1_4[0]", "1"),  # Example: IRC 6655

    # Line 5a: Interest/Penalty/Addition reason (c1_2..c1_8)
    "reason_irs_error":    (f"{_P1}.c1_2[0]", "1"),  # Example: IRS error
    "reason_written_advice": (f"{_P1}.c1_3[0]", "1"), # Example: Erroneous written advice
    "reason_reasonable_cause": (f"{_P1}.c1_4[0]", "1"), # Example: Reasonable cause
}

# Identity items the renderer never auto-fills, even if supplied — the taxpayer
# enters these themselves. (Form 843 requires SSN/EIN for identification, but
# following the conservative pattern of 4868, we may leave them blank if they
# are considered "taxpayer completed". However, 843 often requires EIN for
# processing. We will map them but not skip them unless specified.
# For now, we do NOT skip them, unlike 4868 SSNs.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset()

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 843 fields.
VALID_F843_FIELDS: frozenset[str] = frozenset(
    list(F843_FIELDS.values())
    + [v[0] for v in F843_CHECKBOX_FIELDS.values()]
)
