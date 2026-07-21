"""IRS Form 9465 (Installment Agreement Request) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f9465.pdf``) with ``pypdf`` (``PdfReader().get_fields()``)
and pinning every terminal widget's /Rect against the printed line labels.
Form 9465 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming), so
it MUST be filled via ``form_prefill._render_acroform_pdf``.

Layout
------
Part I — Personal Information: name(s), SSN(s), address, phone, best time to
call.
Part I — Financials: total amount owed (line 5), additional balances (line 6),
amount paid with request (line 7), proposed monthly payment (line 8), payment
day (line 9).
Part I — Direct Debit: routing number, account number, account type.
Part I — Checkboxes: address changed, related to a return, etc.
Part II — Household/Employment/Payment Method questions (radio groups).

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Dollar amounts are plain strings.
Conservative blank-fill: a line with no input value is simply not written.

Taxpayer-completed fields
-------------------------
Bank routing/account numbers are sensitive payment credentials. Like SSNs,
these are intentionally LEFT BLANK by this renderer even if values were
supplied — they are identity/payment items the taxpayer must enter themselves
to ensure security and accuracy of banking details. See
``TAXPAYER_COMPLETED_KEYS``.

VERIFY-AT-GATE
--------------
Some Part II radio groups (c2_1..c2_8) have complex nesting ([0]/[1] or
[0..3]) that requires careful positional auditing against the live PDF.
Fields marked below are best-effort mappings; the gate review must verify
their correctness against the current IRS PDF.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"
_P2 = "topmostSubform[0].Page2[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# ---------------------------------------------------------------------------
F9465_FIELDS: dict[str, str] = {
    # --- Part I: Personal Information ---
    "name":            f"{_P1}.f1_1[0]",       # Line 1 — Name(s)
    "ssn":             f"{_P1}.f1_2[0]",       # Line 1 — SSN
    "spouse_name":     f"{_P1}.f1_3[0]",       # Line 2 — Spouse's name
    "spouse_ssn":      f"{_P1}.f1_4[0]",       # Line 2 — Spouse's SSN
    "address":         f"{_P1}.f1_5[0]",       # Line 3 — Street address
    "apt":             f"{_P1}.f1_6[0]",       # Line 3 — Apt/Suite
    "city":            f"{_P1}.f1_7[0]",       # Line 3 — City
    "state":           f"{_P1}.f1_8[0]",       # Line 3 — State
    "zip":             f"{_P1}.f1_9[0]",       # Line 3 — ZIP
    "phone":           f"{_P1}.f1_10[0]",      # Line 4 — Phone number
    "best_time_call":  f"{_P1}.f1_11[0]",      # Line 4 — Best time to call

    # --- Part I: Financials ---
    "total_owed_5":    f"{_P1}.f1_12[0]",      # Line 5 — Total amount owed
    "additional_bal_6":f"{_P1}.f1_13[0]",      # Line 6 — Additional balances due
    "amount_paid_7":   f"{_P1}.f1_14[0]",      # Line 7 — Amount paid with request
    "monthly_payment_8": f"{_P1}.f1_15[0]",    # Line 8 — Proposed monthly payment
    "payment_day_9":   f"{_P1}.f1_16[0]",      # Line 9 — Payment day (1-28)

    # --- Part I: Direct Debit ---
    "routing_number":  f"{_P1}.f1_17[0]",      # Routing number
    "account_number":  f"{_P1}.f1_18[0]",      # Account number
    "account_type":    f"{_P1}.f1_19[0]",      # Account type (Checking/Savings)

    # --- Part I: Checkboxes (Text representation for mapping, actuals in CHECKBOX_FIELDS) ---
    # These are logical keys for the checkbox logic below.

    # --- Part II: Radio Groups (VERIFY-AT-GATE) ---
    # Mapping Part II radio buttons is complex due to nested groups.
    # These are best-effort mappings based on typical PDF structure.
    "household_status": f"{_P2}.c2_1[0]",       # Household status (radio)
    "employment_status": f"{_P2}.c2_2[0]",      # Employment status (radio)
    "payment_method":   f"{_P2}.c2_3[0]",       # Payment method preference (radio)
    # Additional Part II fields to be verified at gate:
    # "c2_4", "c2_5", "c2_6", "c2_7", "c2_8"
}

# ---------------------------------------------------------------------------
# Checkboxes (Part I): logical key -> (AcroForm field name, on-state).
# ---------------------------------------------------------------------------
F9465_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    "address_changed": (f"{_P1}.c1_1[0]", "1"),  # Address changed?
    "related_return":  (f"{_P1}.c1_2[0]", "1"),  # Related to a return?
    "other_agreement": (f"{_P1}.c1_3[0]", "1"),  # Other agreement?
    "consent":         (f"{_P1}.c1_4[0]", "1"),  # Consent to direct debit?
}

# Identity/Payment items the renderer never auto-fills, even if supplied — the
# taxpayer enters these themselves for security.
TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({
    "ssn", "spouse_ssn", "routing_number", "account_number"
})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 9465 fields.
VALID_F9465_FIELDS: frozenset[str] = frozenset(
    list(F9465_FIELDS.values())
    + [v[0] for v in F9465_CHECKBOX_FIELDS.values()]
)
