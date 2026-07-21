"""IRS Form 1096 (Annual Summary and Transmittal of U.S. Information Returns) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1096.pdf``) with ``pypdf`` (``PdfReader().get_fields()``)
and pinning every terminal widget's /Rect against the printed line labels.
Form 1096 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so
it MUST be filled via ``form_prefill._render_acroform_pdf``.

Layout
------
The form has a single page with:
  - Filer identification block (Name, Address, City/State/ZIP, Contact, Phone, Email, Fax).
  - EIN / SSN boxes (mutually exclusive).
  - Box 3: Total number of forms.
  - Box 4: Federal income tax withheld.
  - Box 5: Total amount reported.
  - Box 6: Grid of checkboxes for types of forms being transmitted (NEC, MISC, INT, DIV, W-2G, 1098, 5498, etc.).
  - "Final return" checkbox (if present).

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.
Dollar amounts are written as plain strings ("12500" — no $ or commas).
Conservative blank-fill: a line with no input value is simply not written.

FILING CONSTRAINT
-----------------
The downloadable Form 1096 Copy A is NOT filable — the IRS requires the official
scannable red-ink version (ordered from the IRS). This renderer produces a
PREPARATION/RECORD copy the filer transcribes from or keeps; it must never be
presented as a filable artifact.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F1096_FIELDS: dict[str, str] = {
    # --- Filer Identification ---
    "filer_name":        f"{_P1}.NameAddress_ReadOrder[0].f1_1[0]",
    "address_line1":     f"{_P1}.NameAddress_ReadOrder[0].f1_2[0]",
    "address_line2":     f"{_P1}.NameAddress_ReadOrder[0].f1_3[0]",
    "city":              f"{_P1}.NameAddress_ReadOrder[0].f1_4[0]",
    "state":             f"{_P1}.NameAddress_ReadOrder[0].f1_5[0]",
    "zip_code":          f"{_P1}.NameAddress_ReadOrder[0].f1_6[0]",
    "contact_person":    f"{_P1}.NameAddress_ReadOrder[0].f1_7[0]",
    "phone":             f"{_P1}.NameAddress_ReadOrder[0].f1_8[0]",
    "email":             f"{_P1}.NameAddress_ReadOrder[0].f1_9[0]",
    "fax":               f"{_P1}.NameAddress_ReadOrder[0].f1_10[0]",

    # --- EIN / SSN ---
    "ein":               f"{_P1}.EIN_SSN_ReadOrder[0].f1_11[0]",
    "ssn":               f"{_P1}.EIN_SSN_ReadOrder[0].f1_12[0]",

    # --- Boxes 3, 4, 5 ---
    "box_3_total_forms": f"{_P1}.Box3[0].f1_13[0]",
    "box_4_withheld":    f"{_P1}.Box4[0].f1_14[0]",
    "box_5_total_amount":f"{_P1}.Box5[0].f1_15[0]",
}

# ---------------------------------------------------------------------------
# Checkboxes (Box 6 grid): logical key -> (AcroForm field name, on-state).
# Only ONE box-6 checkbox may be set per transmittal.
# ---------------------------------------------------------------------------
F1096_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    "box_6_1099_nec":    (f"{_P1}.Box6_ReadOrder[0].c6_1[0]", "1"),
    "box_6_1099_misc":   (f"{_P1}.Box6_ReadOrder[0].c6_2[0]", "1"),
    "box_6_1099_int":    (f"{_P1}.Box6_ReadOrder[0].c6_3[0]", "1"),
    "box_6_1099_div":    (f"{_P1}.Box6_ReadOrder[0].c6_4[0]", "1"),
    "box_6_w2g":         (f"{_P1}.Box6_ReadOrder[0].c6_5[0]", "1"),
    "box_6_1098":        (f"{_P1}.Box6_ReadOrder[0].c6_6[0]", "1"),
    "box_6_5498":        (f"{_P1}.Box6_ReadOrder[0].c6_7[0]", "1"),
    "box_6_1099_k":      (f"{_P1}.Box6_ReadOrder[0].c6_8[0]", "1"),
    "box_6_1099_r":      (f"{_P1}.Box6_ReadOrder[0].c6_9[0]", "1"),
    "box_6_1099_s":      (f"{_P1}.Box6_ReadOrder[0].c6_10[0]", "1"),
    "box_6_1099_g":      (f"{_P1}.Box6_ReadOrder[0].c6_11[0]", "1"),
    "box_6_1099_c":      (f"{_P1}.Box6_ReadOrder[0].c6_12[0]", "1"),
    "box_6_1099_oid":    (f"{_P1}.Box6_ReadOrder[0].c6_13[0]", "1"),
    "box_6_8849":        (f"{_P1}.Box6_ReadOrder[0].c6_14[0]", "1"),
    "box_6_8955_ssa":    (f"{_P1}.Box6_ReadOrder[0].c6_15[0]", "1"),
    "box_6_other":       (f"{_P1}.Box6_ReadOrder[0].c6_16[0]", "1"),
}

# "Final return" checkbox if present
F1096_FINAL_RETURN_KEY = "final_return"
F1096_FINAL_RETURN_FIELD = f"{_P1}.FinalReturn[0].c1_1[0]"

# Every AcroForm field name this map can write
VALID_F1096_FIELDS: frozenset[str] = frozenset(
    list(F1096_FIELDS.values())
    + [v[0] for v in F1096_CHECKBOX_FIELDS.values()]
    + [F1096_FINAL_RETURN_FIELD]
)
