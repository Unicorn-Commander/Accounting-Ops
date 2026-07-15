"""IRS Form W-9 (Request for TIN) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/fw9.pdf``) with ``pypdf`` and pinning terminal widgets.
Form W-9 uses a zero-padded naming convention (``f1_01``, ``f1_02``, etc.)
which differs from the ``f1_1``, ``f1_2`` style of other forms.

Layout
------
  Line 1: Name (individual or single-member LLC)
  Line 2: Business name/disregarded entity
  Line 3a: Federal tax classification (radio buttons c1_1[0]..c1_1[6])
  Line 3b: Foreign account/foreign payer (checkbox c1_2[0])
  Line 4: Exempt payee code (f1_05) & FATCA exemption code (f1_06)
  Line 5: Address (f1_07)
  Line 6: City, State, ZIP (f1_08)
  Line 7: Account numbers (f1_09)
  Line 8: Requester name & address (f1_10)
  Part I: TIN (f1_10..f1_15 — segmented digit boxes)

Conventions mirror ``f4868_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.
Dollar amounts are written as plain strings ("12500" — no $ or commas); the IRS
boxes are plain-text. Conservative blank-fill: a line with no input value is
simply not written.

CPA/EA verification note
------------------------
These names match the current fw9.pdf as distributed on irs.gov. The face field
names are stable across recent revisions, but if a later (or prior-year
``fw9_{year}.pdf``) PDF is dropped into ``data/irs_forms/``, re-run the dump and
re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
FW9_FIELDS: dict[str, str] = {
    # --- Line 1: Name ---
    "name": f"{_P1}.f1_01[0]",  # Line 1 — Name (individual or single-member LLC)

    # --- Line 2: Business Name ---
    "business_name": f"{_P1}.f1_02[0]",  # Line 2 — Business name / disregarded entity

    # --- Line 3a: Tax Classification (handled via radio buttons) ---
    # The radio group is c1_1[0]..c1_1[6]. The logical key maps to the specific
    # radio button index to check. See FW9_RADIO_FIELDS.

    # --- Line 3b: Foreign Status ---
    "foreign_status": f"{_P1}.c1_2[0]",  # Line 3b — Foreign account/foreign payer

    # --- Line 4: Exempt Codes ---
    "exempt_payee_code": f"{_P1}.f1_05[0]",  # Line 4 — Exempt payee code
    "fatca_exemption_code": f"{_P1}.f1_06[0]",  # Line 4 — FATCA exemption code

    # --- Line 5: Address ---
    "address": f"{_P1}.f1_07[0]",  # Line 5 — Street address

    # --- Line 6: City, State, ZIP ---
    "city": f"{_P1}.f1_08[0]",  # Line 6 — City
    "state": f"{_P1}.f1_08[1]",  # Line 6 — State
    "zip_code": f"{_P1}.f1_08[2]",  # Line 6 — ZIP code

    # --- Line 7: Account Numbers ---
    "account_numbers": f"{_P1}.f1_09[0]",  # Line 7 — Account numbers

    # --- Line 8: Requester Name & Address ---
    "requester_name": f"{_P1}.f1_10[0]",  # Line 8 — Requester name
    "requester_address": f"{_P1}.f1_10[1]",  # Line 8 — Requester address
    "requester_city": f"{_P1}.f1_10[2]",  # Line 8 — Requester city
    "requester_state": f"{_P1}.f1_10[3]",  # Line 8 — Requester state
    "requester_zip": f"{_P1}.f1_10[4]",  # Line 8 — Requester ZIP

    # --- Part I: TIN ---
    # The TIN is entered in segmented boxes. The layout depends on SSN (3-2-4)
    # vs EIN (2-7), but the field names are consistent in the PDF structure.
    # We map logical keys to the specific digit boxes.
    "tin_part1": f"{_P1}.f1_11[0]",  # Part I — TIN first segment
    "tin_part2": f"{_P1}.f1_12[0]",  # Part I — TIN second segment
    "tin_part3": f"{_P1}.f1_13[0]",  # Part I — TIN third segment
    "tin_part4": f"{_P1}.f1_14[0]",  # Part I — TIN fourth segment
    "tin_part5": f"{_P1}.f1_15[0]",  # Part I — TIN fifth segment
}

# ---------------------------------------------------------------------------
# Radio buttons (Line 3a): logical key -> (AcroForm field name, on-state).
# The radio group is c1_1[0]..c1_1[6].
# ---------------------------------------------------------------------------
FW9_RADIO_FIELDS: dict[str, tuple[str, str]] = {
    "individual_sole_prop": (f"{_P1}.c1_1[0]", "1"),  # Line 3a — Individual/sole proprietor
    "c_corporation": (f"{_P1}.c1_1[1]", "1"),  # Line 3a — C Corporation
    "s_corporation": (f"{_P1}.c1_1[2]", "1"),  # Line 3a — S Corporation
    "partnership": (f"{_P1}.c1_1[3]", "1"),  # Line 3a — Partnership
    "trust_estate": (f"{_P1}.c1_1[4]", "1"),  # Line 3a — Trust/Estate
    "llc": (f"{_P1}.c1_1[5]", "1"),  # Line 3a — LLC
    "other": (f"{_P1}.c1_1[6]", "1"),  # Line 3a — Other
}

# LLC tax classification letter (Line 3a detail for LLC)
FW9_LLC_CLASSIFICATION: str = f"{_P1}.f1_03[0]"  # Line 3a — LLC classification (C/S/P)

# "Other" detail (Line 3a detail for Other)
FW9_OTHER_DETAIL: str = f"{_P1}.f1_04[0]"  # Line 3a — Other detail

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form W-9 fields.
VALID_FW9_FIELDS: frozenset[str] = frozenset(
    list(FW9_FIELDS.values())
    + [v[0] for v in FW9_RADIO_FIELDS.values()]
    + [FW9_LLC_CLASSIFICATION, FW9_OTHER_DETAIL]
)
