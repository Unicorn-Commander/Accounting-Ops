"""IRS Form 7004 (Automatic Extension of Time To File Certain Business Income Tax Returns) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f7004.pdf``, **Tax Year 2025** revision) with ``pypdf``
(``PdfReader().get_fields()``) and pinning every terminal widget's /Rect against
the printed line labels — a positional audit, because the IRS ``f{page}_{NN}``
numbering increments in document/tab order, NOT line order. Form 7004 is a modern
XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the W-4P / 1040), so it
MUST be filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-
tree walker), not the older page-based ``_fill_pdf_fields``.

Layout
------
Form 7004 is a single-page form for business entities (1120, 1065, 1041, 8832,
etc.) requesting an automatic extension of time to file.

  Header: Name, address, city/state/ZIP, EIN.
  Part I: Line 1 = 2-digit form code (e.g. "12" for 1120).
  Part II: Line 2 (foreign corp checkbox), Line 3 (consolidated group checkbox),
           Line 4 (Regs 1.6081-5 qualifier checkbox).
  Part III: Line 5a = fiscal year begin/end dates; Line 5b = short tax year reason
            (radio buttons c1_4[0]..c1_4[4]); Line 6 = tentative total tax;
            Line 7 = total payments and credits; Line 8 = balance due.

Conventions mirror ``f4868_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.
Dollar amounts are written as plain strings ("12500" — no $ or commas); the IRS
boxes are plain-text. Conservative blank-fill: a line with no input value is
simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f7004.pdf as distributed on irs.gov. The face field
names are stable across recent revisions, but if a later (or prior-year
``f7004_{year}.pdf``) PDF is dropped into ``data/irs_forms/``, re-run the dump and
re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F7004_FIELDS: dict[str, str] = {
    # --- Header Identification ---
    "name":            f"{_P1}.f1_1[0]",   # Name
    "address":         f"{_P1}.f1_2[0]",   # Street address
    "city_town":       f"{_P1}.f1_3[0]",   # City, town, or post office
    "state":           f"{_P1}.f1_4[0]",   # State
    "zip_code":        f"{_P1}.f1_5[0]",   # ZIP code
    "ein":             f"{_P1}.f1_6[0]",   # Employer identification number

    # --- Part I: Form Code ---
    "form_code":       f"{_P1}.f1_7[0]",   # Line 1 — 2-digit form code (e.g. 12)

    # --- Part II: Entity Type Checkboxes (handled separately) ---
    # (See F7004_CHECKBOX_FIELDS)

    # --- Part III: Extension Details ---
    "fiscal_year_begin": f"{_P1}.f1_8[0]",  # Line 5a — Beginning date
    "fiscal_year_end":   f"{_P1}.f1_9[0]",  # Line 5a — Ending date
    "tentative_tax_6":   f"{_P1}.f1_10[0]", # Line 6 — Tentative total tax
    "payments_7":        f"{_P1}.f1_11[0]", # Line 7 — Total payments and credits
    "balance_due_8":     f"{_P1}.f1_12[0]", # Line 8 — Balance due

    # --- Short Tax Year Reason (Radio buttons, handled separately) ---
    # (See F7004_CHECKBOX_FIELDS)
}

# ---------------------------------------------------------------------------
# Checkboxes & Radios: logical key -> (AcroForm field name, on-state).
# Each is an INDEPENDENT /Btn with on-states ["/1", "/Off"] — set with on-state
# "1" when the taxpayer's answer is yes; otherwise leave /Off (conservative).
# ---------------------------------------------------------------------------
F7004_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 2: Foreign corporation without a US office
    "foreign_corp_2": (f"{_P1}.c1_1[0]", "1"),
    # Line 3: Consolidated group parent
    "consolidated_3": (f"{_P1}.c1_2[0]", "1"),
    # Line 4: Regs 1.6081-5 qualifier
    "regs_qualifier_4": (f"{_P1}.c1_3[0]", "1"),
    # Line 5b: Short tax year reason (Radio buttons c1_4[0]..c1_4[4])
    "short_year_initial":    (f"{_P1}.c1_4[0]", "1"),  # Initial return
    "short_year_final":      (f"{_P1}.c1_4[1]", "1"),  # Final return
    "short_year_accounting": (f"{_P1}.c1_4[2]", "1"),  # Change in accounting period
    "short_year_consolidated":(f"{_P1}.c1_4[3]", "1"), # Consolidated return to be filed
    "short_year_other":      (f"{_P1}.c1_4[4]", "1"),  # Other
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 7004 fields.
VALID_F7004_FIELDS: frozenset[str] = frozenset(
    list(F7004_FIELDS.values())
    + [v[0] for v in F7004_CHECKBOX_FIELDS.values()]
)
