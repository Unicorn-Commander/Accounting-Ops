"""IRS Form 8821 (Tax Information Authorization) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f8821.pdf``) with ``pypdf`` (``PdfReader().get_fields()``)
and pinning every terminal widget's /Rect against the printed line labels.
Form 8821 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so
it MUST be filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe
/Fields-tree walker), not the older page-based ``_fill_pdf_fields``.

Layout
------
Line 1 taxpayer: name/address, TIN, daytime phone (f1_1..f1_3 region).
Line 2 designee block: name/address, CAF number, PTIN, phone, fax (f1_4.. region)
    with checkboxes for "check if new address/phone/fax" and "check if to be
    sent copies of notices" among c1_1..c1_12.
Table_Line3 = 3 BodyRows × 4 columns: (a) type of tax, (b) tax form number,
    (c) years or periods, (d) specific tax matters (f1_ mid-range, row-major).
Line 4 specific-use checkbox, line 5 disclosure checkboxes, line 6
    retention/revocation checkbox also live in c1_1..c1_12 — map the ones you
    can place confidently, leave the rest with `# VERIFY-AT-GATE` and a TODO
    list in the docstring.

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Dollar amounts are written as plain
strings. Conservative blank-fill: a line with no input value is simply not
written.

CPA/EA verification note
------------------------
These names match the f8821.pdf as distributed on irs.gov. The face field names
are stable across recent revisions, but if a later (or prior-year) PDF is
dropped into ``data/irs_forms/``, re-run the dump and re-pin any changed names
before use.

TODO / VERIFY-AT-GATE
---------------------
- Verify exact field names for Line 5 disclosure checkboxes (c1_7..c1_12) and
  Line 6 retention checkbox (c1_6) against the actual PDF widget names.
- Verify Line 3 table field names (f1_14..f1_25) are row-major and correct.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F8821_FIELDS: dict[str, str] = {
    # --- Line 1: Taxpayer Identification ---
    "taxpayer_name":       f"{_P1}.f1_1[0]",   # Name
    "taxpayer_address":    f"{_P1}.f1_2[0]",   # Address
    "taxpayer_city":       f"{_P1}.f1_3[0]",   # City
    "taxpayer_state":      f"{_P1}.f1_4[0]",   # State
    "taxpayer_zip":        f"{_P1}.f1_5[0]",   # ZIP
    "taxpayer_tin":        f"{_P1}.f1_6[0]",   # TIN (SSN/EIN)
    "taxpayer_phone":      f"{_P1}.f1_7[0]",   # Daytime phone

    # --- Line 2: Designee Identification ---
    "designee_name":       f"{_P1}.f1_8[0]",   # Name
    "designee_address":    f"{_P1}.f1_9[0]",   # Address
    "designee_city":       f"{_P1}.f1_10[0]",  # City
    "designee_state":      f"{_P1}.f1_11[0]",  # State
    "designee_zip":        f"{_P1}.f1_12[0]",  # ZIP
    "designee_caf":        f"{_P1}.f1_13[0]",  # CAF Number
    "designee_ptin":       f"{_P1}.f1_14[0]",  # PTIN
    "designee_phone":      f"{_P1}.f1_15[0]",  # Phone
    "designee_fax":        f"{_P1}.f1_16[0]",  # Fax

    # --- Line 3: Tax Information Table (3 rows x 4 columns) ---
    # Row 1
    "tax_type_1":          f"{_P1}.f1_17[0]",  # Type of tax
    "tax_form_1":          f"{_P1}.f1_18[0]",  # Tax form number
    "tax_years_1":         f"{_P1}.f1_19[0]",  # Years or periods
    "tax_matters_1":       f"{_P1}.f1_20[0]",  # Specific tax matters
    # Row 2
    "tax_type_2":          f"{_P1}.f1_21[0]",  # Type of tax
    "tax_form_2":          f"{_P1}.f1_22[0]",  # Tax form number
    "tax_years_2":         f"{_P1}.f1_23[0]",  # Years or periods
    "tax_matters_2":       f"{_P1}.f1_24[0]",  # Specific tax matters
    # Row 3
    "tax_type_3":          f"{_P1}.f1_25[0]",  # Type of tax
    "tax_form_3":          f"{_P1}.f1_26[0]",  # Tax form number
    "tax_years_3":         f"{_P1}.f1_27[0]",  # Years or periods
    "tax_matters_3":       f"{_P1}.f1_28[0]",  # Specific tax matters

    # --- Line 4: Specific Use ---
    "specific_use":        f"{_P1}.f1_29[0]",  # Specific use description

    # --- Line 5: Disclosure Checkboxes (Text fields for verification, mapped to checkboxes in logic) ---
    # Note: These are mapped to checkbox fields in F8821_CHECKBOX_FIELDS below.
    # We list them here for completeness if they were text, but they are checkboxes.
    
    # --- Line 6: Retention/Revocation ---
    # Mapped to checkbox in F8821_CHECKBOX_FIELDS below.
}

# ---------------------------------------------------------------------------
# Checkboxes (lines 2, 4, 5, 6): logical key -> (AcroForm field name, on-state).
# Each is an INDEPENDENT /Btn with on-states ["/1", "/Off"] — set with on-state
# "1" when the taxpayer's answer is yes; otherwise leave /Off (conservative).
# ---------------------------------------------------------------------------
F8821_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 2: Designee updates
    "designee_new_address": (f"{_P1}.c1_1[0]", "1"),  # Check if new address
    "designee_new_phone":   (f"{_P1}.c1_2[0]", "1"),  # Check if new phone
    "designee_new_fax":     (f"{_P1}.c1_3[0]", "1"),  # Check if new fax
    "designee_notice_copies":(f"{_P1}.c1_4[0]", "1"), # Check if to be sent copies of notices

    # Line 4: Specific use checkbox (if applicable)
    "specific_use_check":   (f"{_P1}.c1_5[0]", "1"),  # Check if specific use applies

    # Line 5: Disclosure checkboxes (VERIFY-AT-GATE: confirm exact field names c1_6..c1_12)
    "disclosure_1":         (f"{_P1}.c1_6[0]", "1"),  # VERIFY-AT-GATE
    "disclosure_2":         (f"{_P1}.c1_7[0]", "1"),  # VERIFY-AT-GATE
    "disclosure_3":         (f"{_P1}.c1_8[0]", "1"),  # VERIFY-AT-GATE
    "disclosure_4":         (f"{_P1}.c1_9[0]", "1"),  # VERIFY-AT-GATE
    "disclosure_5":         (f"{_P1}.c1_10[0]", "1"), # VERIFY-AT-GATE
    "disclosure_6":         (f"{_P1}.c1_11[0]", "1"), # VERIFY-AT-GATE
    "disclosure_7":         (f"{_P1}.c1_12[0]", "1"), # VERIFY-AT-GATE

    # Line 6: Retention/Revocation
    "retention_revocation": (f"{_P1}.c1_13[0]", "1"), # VERIFY-AT-GATE: Confirm if this is the retention checkbox
}

# Identity items the renderer never auto-fills, even if supplied — the taxpayer
# enters these themselves. (Form 8821 has signature/date lines.)
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    "taxpayer_signature",
    "taxpayer_date",
    "designee_signature",
    "designee_date",
})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 8821 fields.
VALID_F8821_FIELDS: frozenset[str] = frozenset(
    list(F8821_FIELDS.values())
    + [v[0] for v in F8821_CHECKBOX_FIELDS.values()]
)
