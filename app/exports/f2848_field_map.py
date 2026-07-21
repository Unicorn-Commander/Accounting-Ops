"""IRS Form 2848 (Power of Attorney and Declaration of Representative) — AcroForm field map.

Field names were verified by dumping the shipped official fillable PDF
(``data/irs_forms/f2848.pdf``, Rev. January 2021 layout) with ``pypdf``
(``PdfReader().get_fields()``) and pinning every terminal widget's /Rect
against the printed line captions (see the smoke test's marker-render
procedure). Form 2848 is a modern XFA-hybrid AcroForm (``topmostSubform[0]``
naming) with HUMAN-READABLE partial names (``TaxpayerName[0]``,
``CAFNumber1[0]``, …), so it MUST be filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that
bakes values in as a flattened, visible overlay), not the older page-based
``_fill_pdf_fields``.

Layout (page 1)
---------------
Line 1 taxpayer: name, address (ONE two-row box that holds street AND
    city/state/ZIP), the "Taxpayer identification number(s)" area (three
    sub-boxes named SSN / ITIN / EIN in the AcroForm), daytime telephone,
    plan number.
Line 2 representatives: FOUR blocks (name+address, CAF No., PTIN, telephone,
    fax) each with new-address/new-telephone/new-fax checkboxes. Only
    representatives 1 and 2 have a "check if to be sent copies of notices and
    communications" box — the IRS sends copies to at most two representatives,
    and the shipped PDF has no ``SentCopies3/4`` widget.
Line 3 acts authorized: ``Table_Line3`` = 3 BodyRows × 3 columns
    (Description of Matter | Tax Form Number | Year(s) or Period(s)).
Line 4 "Specific use not recorded on CAF" checkbox.
Line 5a additional acts: four captioned checkboxes (Access my IRS records via
    an Intermediate Service Provider / Authorize disclosure to third parties /
    Substitute or add representative(s) / Sign a return) + three ruled
    free-text lines, then the "Other acts authorized:" checkbox + two ruled
    free-text lines.

Layout (page 2)
---------------
Line 5b specific deletions: two ruled free-text lines.
Line 6 retention/revocation: ONE checkbox — checking it means the taxpayer
    does NOT revoke prior powers of attorney (retention). Its widget's partial
    name is literally ``RetentionRevocation[1]`` in the shipped PDF.
Line 7 taxpayer signature block: signature/date have NO widgets (unfillable by
    design); the Title / Print name / Print name of taxpayer boxes DO have
    widgets but are deliberately NOT mapped (see below).
Part II Declaration of Representative: ``Table_PartII`` = 4 BodyRows ×
    (Designation | Licensing jurisdiction | Bar/license/etc. number |
    Signature | Date). Designation/jurisdiction/number are mapped;
    Signature/Date columns are deliberately NOT mapped.

Deliberately unmapped widgets (never written by this module)
-----------------------------------------------------------
- ``Page1.CheckForm[0]`` — the PDF's own interactive "Check Form for Common
  Errors & Reminders" helper button, not a data field.
- ``Page2.Title[0]``, ``Page2.PrintName[0]``, ``Page2.PrintNameTaxpayer[0]`` —
  the line 7 taxpayer signature block; identity items the signer completes by
  hand (see ``TAXPAYER_COMPLETED_KEYS``).
- ``Page2.Table_PartII…Signature1-4[0]`` / ``Date1-4[0]`` — representative
  signature columns of the Part II declaration.

Conventions mirror ``f8821_field_map.py``: ``F2848_FIELDS`` is logical key ->
fully-qualified AcroForm /Tx field name; ``F2848_CHECKBOX_FIELDS`` is logical
key -> (field name, on-state). All checkboxes on this revision use on-state
``"1"`` (i.e. ``/1``). Conservative blank-fill: a line with no input value is
simply not written.

Shared-box logical keys
-----------------------
- ``taxpayer_address`` and ``taxpayer_city_state_zip`` both target the single
  two-row ``TaxpayerAddress[0]`` box; when both are supplied the renderer
  joins them as ``"<street>, <city_state_zip>"``.
- ``taxpayer_ssn_ein`` is a combined convenience key: an EIN-formatted value
  (``NN-NNNNNNN``) is routed to the EIN sub-box, anything else to the SSN
  sub-box (the leftmost/primary box under the shared printed caption
  "Taxpayer identification number(s)"). Callers that know which kind of TIN
  they hold should prefer the explicit ``taxpayer_ssn`` / ``taxpayer_itin`` /
  ``taxpayer_ein`` keys, which win over the combined key if both are given.

CPA/EA verification note
------------------------
These names match the f2848.pdf as distributed on irs.gov (Rev. Jan 2021
layout). If a later (or prior-year) PDF is dropped into ``data/irs_forms/``
(``f2848_<year>.pdf`` is preferred by the path resolver), re-run the dump and
re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"
_T3 = f"{_P1}.Table_Line3[0]"
_PII = f"{_P2}.Table_PartII[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F2848_FIELDS: dict[str, str] = {
    # --- Line 1: Taxpayer information ---
    "taxpayer_name":           f"{_P1}.TaxpayerName[0]",
    # ONE physical two-row box holds street + city/state/ZIP (see module doc).
    "taxpayer_address":        f"{_P1}.TaxpayerAddress[0]",
    "taxpayer_city_state_zip": f"{_P1}.TaxpayerAddress[0]",   # same box; renderer merges
    "taxpayer_ssn":            f"{_P1}.TaxpayerIDSSN[0]",
    "taxpayer_ssn_ein":        f"{_P1}.TaxpayerIDSSN[0]",     # default box; EIN-format re-routed
    "taxpayer_itin":           f"{_P1}.TaxpayerIDITIN[0]",
    "taxpayer_ein":            f"{_P1}.TaxpayerIDEIN[0]",
    "taxpayer_telephone":      f"{_P1}.TaxpayerTelephone[0]",
    "taxpayer_plan_number":    f"{_P1}.TaxpayerPlanNumber[0]",

    # --- Line 2: Representatives (four blocks) ---
    "rep1_name":      f"{_P1}.RepresentativesName1[0]",
    "rep1_address":   f"{_P1}.RepresentativesAddress1[0]",
    "rep1_caf":       f"{_P1}.CAFNumber1[0]",
    "rep1_ptin":      f"{_P1}.PTIN1[0]",
    "rep1_telephone": f"{_P1}.TelephoneNo1[0]",
    "rep1_fax":       f"{_P1}.FaxNo1[0]",

    "rep2_name":      f"{_P1}.RepresentativesName2[0]",
    "rep2_address":   f"{_P1}.RepresentativesAddress2[0]",
    "rep2_caf":       f"{_P1}.CAFNumber2[0]",
    "rep2_ptin":      f"{_P1}.PTIN2[0]",
    "rep2_telephone": f"{_P1}.TelephoneNo2[0]",
    "rep2_fax":       f"{_P1}.FaxNo2[0]",

    "rep3_name":      f"{_P1}.RepresentativesName3[0]",
    "rep3_address":   f"{_P1}.RepresentativesAddress3[0]",
    "rep3_caf":       f"{_P1}.CAFNumber3[0]",
    "rep3_ptin":      f"{_P1}.PTIN3[0]",
    "rep3_telephone": f"{_P1}.TelephoneNo3[0]",
    "rep3_fax":       f"{_P1}.FaxNo3[0]",

    "rep4_name":      f"{_P1}.RepresentativesName4[0]",
    "rep4_address":   f"{_P1}.RepresentativesAddress4[0]",
    "rep4_caf":       f"{_P1}.CAFNumber4[0]",
    "rep4_ptin":      f"{_P1}.PTIN4[0]",
    "rep4_telephone": f"{_P1}.TelephoneNo4[0]",
    "rep4_fax":       f"{_P1}.FaxNo4[0]",

    # --- Line 3: Acts authorized (3 rows × 3 columns) ---
    "acts_description_1": f"{_T3}.BodyRow1[0].Description1[0]",
    "acts_form_number_1": f"{_T3}.BodyRow1[0].TaxForm1[0]",
    "acts_years_1":       f"{_T3}.BodyRow1[0].Years1[0]",
    "acts_description_2": f"{_T3}.BodyRow2[0].Description2[0]",
    "acts_form_number_2": f"{_T3}.BodyRow2[0].TaxForm2[0]",
    "acts_years_2":       f"{_T3}.BodyRow2[0].Years2[0]",
    "acts_description_3": f"{_T3}.BodyRow3[0].Description3[0]",
    "acts_form_number_3": f"{_T3}.BodyRow3[0].TaxForm3[0]",
    "acts_years_3":       f"{_T3}.BodyRow3[0].Years3[0]",

    # --- Line 5a: additional-acts free-text lines (after the four checkboxes)
    "additional_acts_1": f"{_P1}.AdditionalActs1[0]",   # short line after "Sign a return;"
    "additional_acts_2": f"{_P1}.AdditionalActs2[0]",
    "additional_acts_3": f"{_P1}.AdditionalActs3[0]",
    # "Other acts authorized:" description lines (checkbox is other_acts below)
    "other_acts_description_1": f"{_P1}.OtherActs1[0]",
    "other_acts_description_2": f"{_P1}.OtherActs2[0]",

    # --- Line 5b (page 2): specific deletions free-text lines ---
    "specific_deletions_1": f"{_P2}.SpecificDeletions1[0]",
    "specific_deletions_2": f"{_P2}.SpecificDeletions2[0]",

    # --- Part II: Declaration of Representative (4 rows; signature/date
    #     columns deliberately unmapped) ---
    "rep1_designation":  f"{_PII}.BodyRow1[0].Designation1[0]",
    "rep1_jurisdiction": f"{_PII}.BodyRow1[0].Jurisdiction1[0]",
    "rep1_bar_number":   f"{_PII}.BodyRow1[0].Bar1[0]",
    "rep2_designation":  f"{_PII}.BodyRow2[0].Designation2[0]",
    "rep2_jurisdiction": f"{_PII}.BodyRow2[0].Jurisdiction2[0]",
    "rep2_bar_number":   f"{_PII}.BodyRow2[0].Bar2[0]",
    "rep3_designation":  f"{_PII}.BodyRow3[0].Designation3[0]",
    "rep3_jurisdiction": f"{_PII}.BodyRow3[0].Jurisdiction3[0]",
    "rep3_bar_number":   f"{_PII}.BodyRow3[0].Bar3[0]",
    "rep4_designation":  f"{_PII}.BodyRow4[0].Designation4[0]",
    "rep4_jurisdiction": f"{_PII}.BodyRow4[0].Jurisdiction4[0]",
    "rep4_bar_number":   f"{_PII}.BodyRow4[0].Bar4[0]",
}

# ---------------------------------------------------------------------------
# Checkboxes: logical key -> (AcroForm field name, on-state). Every checkbox
# on this revision is an independent /Btn with states ["/1", "/Off"].
# ---------------------------------------------------------------------------
F2848_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 2: per-representative boxes. Only reps 1-2 have a "send copies of
    # notices and communications" box on the printed form.
    "rep1_sent_copies":   (f"{_P1}.SentCopies1[0]", "1"),
    "rep2_sent_copies":   (f"{_P1}.SentCopies2[0]", "1"),
    "rep1_new_address":   (f"{_P1}.NewAddress1[0]", "1"),
    "rep1_new_telephone": (f"{_P1}.NewTelephoneNo1[0]", "1"),
    "rep1_new_fax":       (f"{_P1}.NewFaxNo1[0]", "1"),
    "rep2_new_address":   (f"{_P1}.NewAddress2[0]", "1"),
    "rep2_new_telephone": (f"{_P1}.NewTelephoneNo2[0]", "1"),
    "rep2_new_fax":       (f"{_P1}.NewFaxNo2[0]", "1"),
    "rep3_new_address":   (f"{_P1}.NewAddress3[0]", "1"),
    "rep3_new_telephone": (f"{_P1}.NewTelephoneNo3[0]", "1"),
    "rep3_new_fax":       (f"{_P1}.NewFaxNo3[0]", "1"),
    "rep4_new_address":   (f"{_P1}.NewAddress4[0]", "1"),
    "rep4_new_telephone": (f"{_P1}.NewTelephoneNo4[0]", "1"),
    "rep4_new_fax":       (f"{_P1}.NewFaxNo4[0]", "1"),

    # Line 4: specific use not recorded on CAF.
    "specific_use": (f"{_P1}.SpecificUse[0]", "1"),

    # Line 5a: additional acts authorized.
    "access_records":       (f"{_P1}.AccessRecords[0]", "1"),
    "authorize_disclosure": (f"{_P1}.AuthorizeDisclosure[0]", "1"),
    # NOTE: the shipped PDF's own field name misspells "Substitute".
    "substitute_or_add":    (f"{_P1}.SubtituteOrAdd[0]", "1"),
    "sign_return":          (f"{_P1}.SignReturn[0]", "1"),
    "other_acts":           (f"{_P1}.OtherActs[0]", "1"),

    # Line 6 (page 2): check = RETAIN (do NOT revoke) prior powers of attorney.
    # The widget's partial name in the shipped PDF is literally
    # "RetentionRevocation[1]".
    "retention_revocation": (f"{_P2}.RetentionRevocation[1]", "1"),
}

# Identity / attestation items the renderer never auto-fills, even if supplied
# — the taxpayer (line 7) and the representatives (Part II) complete these by
# hand. The corresponding widgets (where they exist at all) are deliberately
# absent from the maps above, so these logical keys can never reach the PDF.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    # Line 7 taxpayer signature block (page 2).
    "signature_taxpayer",
    "signature_date",
    "signature_title",
    "signature_print_name",
    "signature_print_name_taxpayer",
    # Part II representative signature columns.
    "signature_representative",
    "rep1_signature", "rep1_signature_date",
    "rep2_signature", "rep2_signature_date",
    "rep3_signature", "rep3_signature_date",
    "rep4_signature", "rep4_signature_date",
})

# Every AcroForm field name this map can write — used by tests + the
# render-time provenance guard to validate that we only ever touch known
# Form 2848 fields.
VALID_F2848_FIELDS: frozenset[str] = frozenset(
    list(F2848_FIELDS.values())
    + [v[0] for v in F2848_CHECKBOX_FIELDS.values()]
)
