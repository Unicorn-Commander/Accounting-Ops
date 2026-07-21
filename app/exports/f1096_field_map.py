"""IRS Form 1096 (Annual Summary and Transmittal of U.S. Information Returns) — AcroForm field map.

Field names verified 2026-07-21 by dumping the official 2024 fillable PDF
(``data/irs_forms/f1096_2024.pdf``) with ``pypdf`` (``PdfReader().get_fields()``)
and pinning every terminal widget's /Rect against the printed captions.
Form 1096 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so
it MUST be filled via ``form_prefill._render_acroform_pdf``.

Layout (2024 revision — the form itself is PAGE INDEX 1 of the 3-page file;
page 0 is the red-ink "Attention" notice, page 2 is instructions)
------------------------------------------------------------------
  - Filer block (``NameAddress_ReadOrder[0]``):
      f1_1  FILER'S name
      f1_2  Street address (including room or suite number)
      f1_3  City or town, state or province, country, and ZIP or foreign
            postal code  (ONE combined line — there are no separate
            city / state / ZIP boxes on this revision)
      f1_4  Name of person to contact          f1_5  Telephone number
      f1_6  Email address                      f1_7  Fax number
  - Box row (y≈528):
      f1_8  Box 1 Employer identification number
      f1_9  Box 2 Social security number
      f1_10 Box 3 Total number of forms
      f1_11 Box 4 Federal income tax withheld  (under ``Box4_ReadOrder[0]``)
      f1_12 Box 5 Total amount reported with this Form 1096
  - Box 6: "Enter an X in only one box below" — 34 per-form-type checkboxes,
    each its own field with a UNIQUE on-state ``/1``…``/34`` (left-to-right,
    top-to-bottom). Two grid cells (1099-K, 5498-QA) have no named wrapper
    subform — their widgets hang directly off ``Page1[0]`` as ``c1_1[0]`` /
    ``c1_1[1]``.
  - Signature row: f1_13 = Title (caption "Title", between "Signature" and
    "Date"). Signature and date are handwritten — no widgets exist for them.

DROPPED from the earlier (blind-written) map — no such widgets exist on the
2024 revision:
  - separate ``address_line2`` / ``city`` / ``state`` / ``zip_code`` boxes
    (the form has one combined city/state/ZIP line: ``city_state_zip``);
  - a "final return" checkbox (Form 1096 has none);
  - box-6 entries for 8849 / 8955-SSA / "other" (not on the 1096 grid).

Conventions mirror ``f4868_field_map.py``: ``F1096_FIELDS`` is a
``dict[str, str]`` of logical key -> fully-qualified AcroForm field name;
``F1096_CHECKBOX_FIELDS`` maps logical key -> (field name, on-state WITHOUT
the leading slash — ``_render_acroform_pdf`` adds it). Dollar amounts are
written as plain strings ("12500.00" style, no $ or thousands separators).
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
_NA = f"{_P1}.NameAddress_ReadOrder[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F1096_FIELDS: dict[str, str] = {
    # --- Filer identification block ---
    "filer_name":         f"{_NA}.f1_1[0]",   # FILER'S name
    "address_line1":      f"{_NA}.f1_2[0]",   # Street address (incl. room/suite)
    "city_state_zip":     f"{_NA}.f1_3[0]",   # City/state/ZIP — ONE combined line
    "contact_person":     f"{_NA}.f1_4[0]",   # Name of person to contact
    "phone":              f"{_NA}.f1_5[0]",   # Telephone number
    "email":              f"{_NA}.f1_6[0]",   # Email address
    "fax":                f"{_NA}.f1_7[0]",   # Fax number

    # --- Boxes 1-5 ---
    "ein":                f"{_P1}.f1_8[0]",                    # Box 1 EIN
    "ssn":                f"{_P1}.f1_9[0]",                    # Box 2 SSN
    "box_3_total_forms":  f"{_P1}.f1_10[0]",                   # Box 3
    "box_4_withheld":     f"{_P1}.Box4_ReadOrder[0].f1_11[0]", # Box 4
    "box_5_total_amount": f"{_P1}.f1_12[0]",                   # Box 5

    # --- Signature row ---
    "signer_title":       f"{_P1}.f1_13[0]",  # "Title" on the perjury line
}

# ---------------------------------------------------------------------------
# Box 6 grid: logical key -> (AcroForm field name, on-state).
# Grid order left-to-right / top-to-bottom; on-states are /1…/34 (stored here
# without the slash — the renderer prefixes it). Only ONE may be set per
# transmittal (Form 1096 instruction: "File a separate Form 1096 for each
# type of return").
# ---------------------------------------------------------------------------
F1096_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Row 1
    "box_6_w2g":        (f"{_P1}.FW-2G[0].c1_1[0]", "1"),
    "box_6_1097_btc":   (f"{_P1}.F1097-BTC[0].c1_1[0]", "2"),
    "box_6_1098":       (f"{_P1}.F1098[0].c1_1[0]", "3"),
    "box_6_1098_c":     (f"{_P1}.F1098-C[0].c1_1[0]", "4"),
    "box_6_1098_e":     (f"{_P1}.F1098-E[0].c1_1[0]", "5"),
    "box_6_1098_f":     (f"{_P1}.F1098-F[0].c1_1[0]", "6"),
    "box_6_1098_q":     (f"{_P1}.F1098-Q[0].c1_1[0]", "7"),
    "box_6_1098_t":     (f"{_P1}.F1098-T[0].c1_1[0]", "8"),
    "box_6_1099_a":     (f"{_P1}.F1099-A[0].c1_1[0]", "9"),
    "box_6_1099_b":     (f"{_P1}.F1099-B[0].c1_1[0]", "10"),
    "box_6_1099_c":     (f"{_P1}.F1099-C[0].c1_1[0]", "11"),
    "box_6_1099_cap":   (f"{_P1}.F1099-CAP[0].c1_1[0]", "12"),
    "box_6_1099_div":   (f"{_P1}.F1099-DIV[0].c1_1[0]", "13"),
    "box_6_1099_g":     (f"{_P1}.F1099-G[0].c1_1[0]", "14"),
    "box_6_1099_int":   (f"{_P1}.F1099-INT[0].c1_1[0]", "15"),
    # 1099-K's grid cell has no named wrapper subform — widget hangs directly
    # off Page1[0]. Verified: rect x=554.5 in row 1 == the printed 1099-K cell.
    "box_6_1099_k":     (f"{_P1}.c1_1[0]", "16"),
    # Row 2
    "box_6_1099_ls":    (f"{_P1}.F1099-LS[0].c1_1[0]", "17"),
    "box_6_1099_ltc":   (f"{_P1}.F1099-LTC[0].c1_1[0]", "18"),
    "box_6_1099_misc":  (f"{_P1}.F1099-MISC[0].c1_1[0]", "19"),
    "box_6_1099_nec":   (f"{_P1}.F1099-NEC[0].c1_1[0]", "20"),
    "box_6_1099_oid":   (f"{_P1}.F1099-OID[0].c1_1[0]", "21"),
    "box_6_1099_patr":  (f"{_P1}.F1099-PATR[0].c1_1[0]", "22"),
    "box_6_1099_q":     (f"{_P1}.F1099-Q[0].c1_1[0]", "23"),
    "box_6_1099_qa":    (f"{_P1}.F1099-QA[0].c1_1[0]", "24"),
    "box_6_1099_r":     (f"{_P1}.F1099-R[0].c1_1[0]", "25"),
    "box_6_1099_s":     (f"{_P1}.F1099-S[0].c1_1[0]", "26"),
    "box_6_1099_sa":    (f"{_P1}.F1099-SA[0].c1_1[0]", "27"),
    "box_6_1099_sb":    (f"{_P1}.F1099-SB[0].c1_1[0]", "28"),
    "box_6_3921":       (f"{_P1}.F3921[0].c1_1[0]", "29"),
    "box_6_3922":       (f"{_P1}.F3922[0].c1_1[0]", "30"),
    "box_6_5498":       (f"{_P1}.F5498[0].c1_1[0]", "31"),
    "box_6_5498_esa":   (f"{_P1}.F5498-ESA[0].c1_1[0]", "32"),
    # 5498-QA — the second unnamed grid cell, directly under Page1[0].
    "box_6_5498_qa":    (f"{_P1}.c1_1[1]", "33"),
    # Row 3
    "box_6_5498_sa":    (f"{_P1}.F5498-SA[0].c1_1[0]", "34"),
}

# Every AcroForm field name this map can write
VALID_F1096_FIELDS: frozenset[str] = frozenset(
    list(F1096_FIELDS.values())
    + [v[0] for v in F1096_CHECKBOX_FIELDS.values()]
)
