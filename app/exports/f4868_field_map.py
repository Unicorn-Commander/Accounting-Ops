"""IRS Form 4868 (Automatic Extension of Time To File) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f4868.pdf``, **Tax Year 2025** revision) with ``pypdf``
(``PdfReader().get_fields()``) and pinning every terminal widget's /Rect against
the printed line labels — a positional audit, because the IRS ``f{page}_{NN}``
numbering increments in document/tab order, NOT line order. Form 4868 is a modern
XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the W-4P / 1040), so it
MUST be filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-
tree walker), not the older page-based ``_fill_pdf_fields``.

Layout
------
The fillable form is the detachable voucher at the BOTTOM of page 1; the top of
page 1 and pages 2-4 are instructions. The form has two parts:

  Part I  — Identification: name(s), address, city/state/ZIP, your & spouse SSN
            (lines 1-3).
  Part II — Individual Income Tax: line 4 estimate of total tax liability, line 5
            total payments, line 6 balance due, line 7 amount you're paying
            (dollar fields); line 8 "out of the country" and line 9 "files Form
            1040-NR" (checkboxes).

The voucher header also carries the fiscal-year-filer date blanks ("…or other tax
year beginning ___, 20YY, and ending ___, 20__"), left blank by calendar-year
filers. Page 3 has a single record-keeping box for an electronic-payment
confirmation number (kept for the taxpayer's records, not part of the filing).

Conventions mirror ``f1040_field_map.py`` / ``w4p_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name.
Dollar amounts are written as plain strings ("12500" — no $ or commas); the IRS
boxes are plain-text. Conservative blank-fill: a line with no input value is
simply not written.

CPA/EA verification note
------------------------
These names match the TY2025 f4868.pdf as distributed on irs.gov. The face field
names are stable across recent revisions, but if a later (or prior-year
``f4868_{year}.pdf``) PDF is dropped into ``data/irs_forms/``, re-run the dump and
re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P3 = "topmostSubform[0].Page3[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F4868_FIELDS: dict[str, str] = {
    # --- Fiscal-year filers only (calendar-year filers leave these blank) ---
    # Voucher header: "For calendar year 20YY, or other tax year beginning ___,
    # 20YY, and ending ___, 20__."
    "fiscal_year_begin":   f"{_P1}.VoucherHeader[0].f1_1[0]",  # beginning date
    "fiscal_year_end":     f"{_P1}.VoucherHeader[0].f1_2[0]",  # ending date
    "fiscal_year_end_yy":  f"{_P1}.VoucherHeader[0].f1_3[0]",  # ending year (2-digit)

    # --- Part I: Identification (left column) ---
    "name":        f"{_P1}.PartI_ReadOrder[0].f1_4[0]",   # Line 1 — your name(s)
    "address":     f"{_P1}.PartI_ReadOrder[0].f1_5[0]",   # Line 1 — street address
    "city_town":   f"{_P1}.PartI_ReadOrder[0].f1_6[0]",   # City, town, or post office
    "state":       f"{_P1}.PartI_ReadOrder[0].f1_7[0]",   # State
    "zip_code":    f"{_P1}.PartI_ReadOrder[0].f1_8[0]",   # ZIP code
    # Lines 2 & 3 SSNs — mapped here for the positional record, but the renderer
    # NEVER fills them (identity/wet-ink items the taxpayer enters). See
    # TAXPAYER_COMPLETED_KEYS below.
    "your_ssn":    f"{_P1}.PartI_ReadOrder[0].f1_9[0]",   # Line 2 — your SSN
    "spouse_ssn":  f"{_P1}.PartI_ReadOrder[0].f1_10[0]",  # Line 3 — spouse's SSN

    # --- Part II: Individual Income Tax (right column) ---
    "tax_liability_4": f"{_P1}.f1_11[0]",  # Line 4 — estimate of total tax liability
    "payments_5":      f"{_P1}.f1_12[0]",  # Line 5 — total payments
    "balance_due_6":   f"{_P1}.f1_13[0]",  # Line 6 — balance due (line 4 − line 5)
    "amount_paying_7": f"{_P1}.f1_14[0]",  # Line 7 — amount you're paying

    # --- Page 3 record-keeping (electronic-payment confirmation number) ---
    "confirmation_number": f"{_P3}.Col4[0].f3_1[0]",
}

# ---------------------------------------------------------------------------
# Checkboxes (lines 8 & 9): logical key -> (AcroForm field name, on-state).
# Each is an INDEPENDENT /Btn with on-states ["/1", "/Off"] — set with on-state
# "1" when the taxpayer's answer is yes; otherwise leave /Off (conservative).
# ---------------------------------------------------------------------------
F4868_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    "out_of_country_8": (f"{_P1}.c1_1[0]", "1"),  # Line 8 — "out of the country"
    "form_1040nr_9":    (f"{_P1}.c1_2[0]", "1"),  # Line 9 — files Form 1040-NR
}

# Identity items the renderer never auto-fills, even if supplied — the taxpayer
# enters these themselves. (Form 4868 has no signature line.)
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({"your_ssn", "spouse_ssn"})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 4868 fields.
VALID_F4868_FIELDS: frozenset[str] = frozenset(
    list(F4868_FIELDS.values())
    + [v[0] for v in F4868_CHECKBOX_FIELDS.values()]
)
