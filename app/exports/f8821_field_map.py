"""IRS Form 8821 (Tax Information Authorization, Rev. January 2021) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f8821.pdf``) with ``pypdf`` (``PdfReader().get_fields()``)
and pinning every terminal widget's /Rect against the printed line labels
(marker-render + raster inspection). Form 8821 is a modern XFA-hybrid AcroForm
(``topmostSubform[0]`` naming), so it MUST be filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker), not
the older page-based ``_fill_pdf_fields``.

Actual layout (Rev. 1-2021, single page)
----------------------------------------
Line 1 taxpayer: ONE combined multi-line "Name and address" box (``f1_6``) —
    there are NO separate city/state/ZIP fields on this form — plus TIN
    (``f1_7``), daytime phone (``f1_8``), and plan number (``f1_9``).
Line 2 designee(s): TWO designee blocks. Each has a combined name-and-address
    box, CAF No., PTIN, telephone, fax, and a checkbox row reading
    "Check if to be sent copies of notices and communications [] Check if new:
    Address [] Telephone No. [] Fax No. []". A separate line-2 header checkbox
    (``c1_1``) marks "a list of additional designees is attached".
Line 3 tax information: header checkbox ``c1_10`` ("By checking here, I
    authorize access to my IRS records via an Intermediate Service Provider")
    plus ``Table_Line3`` = 3 BodyRows x 4 columns: (a) type of tax, (b) tax
    form number, (c) years or periods, (d) specific tax matters
    (``f1_20``..``f1_31``, row-major, nested under ``BodyRowN[0]``).
Line 4 specific use not recorded on CAF: checkbox ``c1_11`` ONLY — there is no
    free-text "specific use" field on this revision.
Line 5 retention of prior authorizations: checkbox ``c1_12`` ("check this box
    and attach a copy of the tax information authorization(s) you want to
    retain" — i.e. checking it means prior authorizations are NOT revoked).
Line 6 taxpayer signature: the signature and date lines are NOT AcroForm
    fields (hand-signed); only "Print Name" (``f1_32``) and "Title (if
    applicable)" (``f1_33``) are fillable.

Never mapped (deliberately)
---------------------------
``Pg1Header[0].f1_1[0]``..``f1_5[0]`` are the "For IRS Use Only" box
(Received by / Name / Telephone / Function / Date). They are IRS-completed
areas and MUST never be written by this renderer.

Rendering note
--------------
``_render_acroform_pdf`` bakes each value as a single line centred in the
widget rect — it does not wrap. For the combined name-and-address boxes
(``f1_6``/``f1_10``/``f1_15``) callers should pass a compact comma-separated
block ("Name, Street, City ST ZIP"), not embedded newlines.

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> fully-qualified AcroForm field name. Conservative blank-fill: a line with no
input value is simply not written.

CPA/EA verification note
------------------------
These names match f8821.pdf (Rev. January 2021) as distributed on irs.gov. If
a later (or prior-year) PDF is dropped into ``data/irs_forms/``, re-run the
dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_T3 = f"{_P1}.Table_Line3[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F8821_FIELDS: dict[str, str] = {
    # --- Line 1: Taxpayer information ---
    # ONE combined multi-line box on the official form (no separate
    # city/state/ZIP fields exist). Pass "Name, Street, City ST ZIP".
    "taxpayer_name_address":  f"{_P1}.f1_6[0]",   # Taxpayer name and address
    "taxpayer_tin":           f"{_P1}.f1_7[0]",   # Taxpayer identification number(s)
    "taxpayer_phone":         f"{_P1}.f1_8[0]",   # Daytime telephone number
    "taxpayer_plan_number":   f"{_P1}.f1_9[0]",   # Plan number (if applicable)

    # --- Line 2: Designee 1 ---
    "designee_name_address":  f"{_P1}.f1_10[0]",  # Designee name and address
    "designee_caf":           f"{_P1}.f1_11[0]",  # CAF No.
    "designee_ptin":          f"{_P1}.f1_12[0]",  # PTIN
    "designee_phone":         f"{_P1}.f1_13[0]",  # Telephone number
    "designee_fax":           f"{_P1}.f1_14[0]",  # Fax number

    # --- Line 2: Designee 2 ---
    "designee2_name_address": f"{_P1}.f1_15[0]",  # Designee name and address
    "designee2_caf":          f"{_P1}.f1_16[0]",  # CAF No.
    "designee2_ptin":         f"{_P1}.f1_17[0]",  # PTIN
    "designee2_phone":        f"{_P1}.f1_18[0]",  # Telephone number
    "designee2_fax":          f"{_P1}.f1_19[0]",  # Fax number

    # --- Line 3: Tax Information table (3 rows x 4 columns, row-major) ---
    # Row 1
    "tax_type_1":    f"{_T3}.BodyRow1[0].f1_20[0]",  # (a) Type of tax information
    "tax_form_1":    f"{_T3}.BodyRow1[0].f1_21[0]",  # (b) Tax form number
    "tax_years_1":   f"{_T3}.BodyRow1[0].f1_22[0]",  # (c) Year(s) or period(s)
    "tax_matters_1": f"{_T3}.BodyRow1[0].f1_23[0]",  # (d) Specific tax matters
    # Row 2
    "tax_type_2":    f"{_T3}.BodyRow2[0].f1_24[0]",  # (a) Type of tax information
    "tax_form_2":    f"{_T3}.BodyRow2[0].f1_25[0]",  # (b) Tax form number
    "tax_years_2":   f"{_T3}.BodyRow2[0].f1_26[0]",  # (c) Year(s) or period(s)
    "tax_matters_2": f"{_T3}.BodyRow2[0].f1_27[0]",  # (d) Specific tax matters
    # Row 3
    "tax_type_3":    f"{_T3}.BodyRow3[0].f1_28[0]",  # (a) Type of tax information
    "tax_form_3":    f"{_T3}.BodyRow3[0].f1_29[0]",  # (b) Tax form number
    "tax_years_3":   f"{_T3}.BodyRow3[0].f1_30[0]",  # (c) Year(s) or period(s)
    "tax_matters_3": f"{_T3}.BodyRow3[0].f1_31[0]",  # (d) Specific tax matters

    # --- Line 6: Signature block (print name / title only; the signature and
    # date lines are hand-completed and have NO AcroForm fields) ---
    "taxpayer_print_name": f"{_P1}.f1_32[0]",  # Print Name
    "taxpayer_title":      f"{_P1}.f1_33[0]",  # Title (if applicable)
}

# ---------------------------------------------------------------------------
# Checkboxes: logical key -> (AcroForm field name, on-state). Each is an
# INDEPENDENT /Btn with states ["/1", "/Off"] — set with on-state "1" when the
# taxpayer's answer is yes; otherwise leave /Off (conservative).
# ---------------------------------------------------------------------------
F8821_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 2 header: "Check here if a list of additional designees is attached"
    "additional_designees_attached": (f"{_P1}.c1_1[0]", "1"),

    # Line 2, designee 1 row: "Check if to be sent copies of notices and
    # communications" then "Check if new: Address / Telephone No. / Fax No."
    "designee_notice_copies": (f"{_P1}.c1_2[0]", "1"),
    "designee_new_address":   (f"{_P1}.c1_3[0]", "1"),
    "designee_new_phone":     (f"{_P1}.c1_4[0]", "1"),
    "designee_new_fax":       (f"{_P1}.c1_5[0]", "1"),

    # Line 2, designee 2 row (mirrors designee 1)
    "designee2_notice_copies": (f"{_P1}.c1_6[0]", "1"),
    "designee2_new_address":   (f"{_P1}.c1_7[0]", "1"),
    "designee2_new_phone":     (f"{_P1}.c1_8[0]", "1"),
    "designee2_new_fax":       (f"{_P1}.c1_9[0]", "1"),

    # Line 3 header: "By checking here, I authorize access to my IRS records
    # via an Intermediate Service Provider."
    "intermediate_service_provider": (f"{_P1}.c1_10[0]", "1"),

    # Line 4: "Specific use not recorded on CAF ... check this box. If you
    # check this box, skip line 5." (checkbox only — no text field exists)
    "specific_use_check": (f"{_P1}.c1_11[0]", "1"),

    # Line 5: "... check this box and attach a copy of the tax information
    # authorization(s) that you want to retain." Checking it means prior
    # authorizations are RETAINED (not auto-revoked).
    "retain_prior_authorizations": (f"{_P1}.c1_12[0]", "1"),
}

# Identity items the renderer never auto-fills, even if supplied — the taxpayer
# signs and dates line 6 themselves. (On this revision the signature/date lines
# are not AcroForm fields at all; this frozenset is defense-in-depth.)
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    "taxpayer_signature",
    "taxpayer_date",
})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 8821 fields.
VALID_F8821_FIELDS: frozenset[str] = frozenset(
    list(F8821_FIELDS.values())
    + [v[0] for v in F8821_CHECKBOX_FIELDS.values()]
)
