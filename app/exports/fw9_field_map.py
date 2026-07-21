"""IRS Form W-9 (Request for TIN) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/fw9.pdf``, Rev. March 2024) with ``pypdf``
(``PdfReader.get_fields()`` for names/types/on-states + per-page widget
rects via ``page.annotations``). Form W-9 uses a zero-padded naming
convention (``f1_01``, ``f1_02``, …) AND nests some terminal fields under
intermediate "read order" subform nodes — the fully-qualified name MUST
include those nodes or the /Fields-tree walker in
``form_prefill._set_acroform_values`` silently never matches them:

  - Line 3a/3b widgets live under ``Boxes3a-b_ReadOrder[0]``
  - Line 5/6 widgets live under ``Address_ReadOrder[0]``

Layout (verified against widget rects, top of page 1 downward)
--------------------------------------------------------------
  Line 1:  Name (f1_01)
  Line 2:  Business name/disregarded entity (f1_02)
  Line 3a: Federal tax classification — 7 checkbox widgets c1_1[0]..c1_1[6]
           with DISTINCT on-states "1".."7" (NOT all "1"), plus the LLC
           classification letter box (f1_03) and the "Other" detail (f1_04)
  Line 3b: Foreign partners/owners/beneficiaries checkbox (c1_2[0], on "1")
  Line 4:  Exempt payee code (f1_05) & FATCA exemption code (f1_06)
  Line 5:  Address — number, street, apt/suite (f1_07)
  Line 6:  City, state, and ZIP code — ONE combined box (f1_08; the PDF has
           no separate city/state/ZIP fields)
  Right of lines 5-6: Requester's name and address, ONE multiline box (f1_09)
  Line 7:  Account number(s) (f1_10)
  Part I:  SSN digit groups 3-2-4 (f1_11/f1_12/f1_13) OR
           EIN digit groups 2-7 (f1_14/f1_15)

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical
key -> the fully-qualified AcroForm field name. Values are plain strings.
Conservative blank-fill: a line with no input value is simply not written.
``city_state_zip`` is one logical key (house convention — same as the W-4P,
843, 941 and Schedule C maps) because the form prints ONE box for it.

CPA/EA verification note
------------------------
These names match the current fw9.pdf (Rev. 3-2024) as distributed on
irs.gov. If a later (or prior-year ``fw9_{year}.pdf``) PDF is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
# Intermediate subform nodes — part of the fully-qualified name chain.
_BOX3 = f"{_P1}.Boxes3a-b_ReadOrder[0]"
_ADDR = f"{_P1}.Address_ReadOrder[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
FW9_FIELDS: dict[str, str] = {
    # --- Line 1: Name ---
    "name": f"{_P1}.f1_01[0]",  # Line 1 — Name of entity/individual

    # --- Line 2: Business Name ---
    "business_name": f"{_P1}.f1_02[0]",  # Line 2 — Business name / disregarded entity

    # --- Line 3a: Tax Classification is checkbox-driven; see
    # FW9_RADIO_FIELDS plus FW9_LLC_CLASSIFICATION / FW9_OTHER_DETAIL below.

    # --- Line 4: Exempt Codes ---
    "exempt_payee_code": f"{_P1}.f1_05[0]",  # Line 4 — Exempt payee code
    "fatca_exemption_code": f"{_P1}.f1_06[0]",  # Line 4 — FATCA exemption code

    # --- Line 5: Address (nested under Address_ReadOrder[0]) ---
    "address": f"{_ADDR}.f1_07[0]",  # Line 5 — Number, street, apt/suite

    # --- Line 6: City, state, and ZIP code — ONE combined box on the form ---
    "city_state_zip": f"{_ADDR}.f1_08[0]",  # Line 6 — City, state, and ZIP code

    # --- Requester box (right of lines 5-6): ONE multiline box ---
    "requester_name_address": f"{_P1}.f1_09[0]",  # Requester's name and address (optional)

    # --- Line 7: Account Numbers ---
    "account_numbers": f"{_P1}.f1_10[0]",  # Line 7 — List account number(s) (optional)

    # --- Part I: TIN (never auto-filled by the renderer — the taxpayer
    # enters their own identity items; see fw9_fill.TAXPAYER_COMPLETED_KEYS) ---
    "ssn_part1": f"{_P1}.f1_11[0]",  # Part I — SSN first 3 digits
    "ssn_part2": f"{_P1}.f1_12[0]",  # Part I — SSN middle 2 digits
    "ssn_part3": f"{_P1}.f1_13[0]",  # Part I — SSN last 4 digits
    "ein_part1": f"{_P1}.f1_14[0]",  # Part I — EIN first 2 digits
    "ein_part2": f"{_P1}.f1_15[0]",  # Part I — EIN last 7 digits
}

# ---------------------------------------------------------------------------
# Line 3a federal tax classification: logical key -> (fully-qualified field
# name, on-state). The seven widgets are c1_1[0]..c1_1[6] under
# Boxes3a-b_ReadOrder[0]; each widget has its OWN export/on-state value
# ("1".."7" — dumped from /_States_), not a shared "1".
# ---------------------------------------------------------------------------
FW9_RADIO_FIELDS: dict[str, tuple[str, str]] = {
    "individual_sole_prop": (f"{_BOX3}.c1_1[0]", "1"),  # Individual/sole proprietor
    "c_corporation": (f"{_BOX3}.c1_1[1]", "2"),  # C corporation
    "s_corporation": (f"{_BOX3}.c1_1[2]", "3"),  # S corporation
    "partnership": (f"{_BOX3}.c1_1[3]", "4"),  # Partnership
    "trust_estate": (f"{_BOX3}.c1_1[4]", "5"),  # Trust/estate
    "llc": (f"{_BOX3}.c1_1[5]", "6"),  # LLC (enter classification in f1_03)
    "other": (f"{_BOX3}.c1_1[6]", "7"),  # Other (see instructions)
}

# ---------------------------------------------------------------------------
# Standalone checkboxes: logical key -> (fully-qualified field name, on-state).
# ---------------------------------------------------------------------------
FW9_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 3b — partnership/trust/estate with foreign partners, owners, or
    # beneficiaries (checkbox, independent of the 3a classification).
    "foreign_status": (f"{_BOX3}.c1_2[0]", "1"),
}

# LLC tax classification letter (Line 3a detail for LLC: C, S, or P)
FW9_LLC_CLASSIFICATION: str = f"{_BOX3}.f1_03[0]"

# "Other" detail (Line 3a detail for Other)
FW9_OTHER_DETAIL: str = f"{_BOX3}.f1_04[0]"

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form W-9 fields.
VALID_FW9_FIELDS: frozenset[str] = frozenset(
    list(FW9_FIELDS.values())
    + [v[0] for v in FW9_RADIO_FIELDS.values()]
    + [v[0] for v in FW9_CHECKBOX_FIELDS.values()]
    + [FW9_LLC_CLASSIFICATION, FW9_OTHER_DETAIL]
)
