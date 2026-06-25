"""IRS Schedule B (Form 1040) — Interest and Ordinary Dividends — field map.

Schedule B reports taxable interest (Part I), ordinary dividends (Part II), and
answers the foreign-accounts / foreign-trust questions (Part III). It is filed
as an ATTACHMENT to Form 1040 when a taxpayer has over $1,500 of taxable
interest or ordinary dividends (or a foreign account/trust). Its Part I line 4
total flows to Form 1040 line 2b; its Part II line 6 total flows to line 3b.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f1040sb.pdf``, **Tax Year 2025** revision, "Created 4/23/25")
with ``pypdf`` and cross-referencing every terminal field's widget /Rect against
the printed line labels (a positional audit — the IRS XFA tooltips are empty on
this form). Schedule B is a modern single-page XFA-hybrid AcroForm
(``topmostSubform[0]`` naming, like the W-4P / Form 1040), so it MUST be filled
via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that
bakes values in as a flattened, visible overlay), NOT the older page-based
``_fill_pdf_fields``.

Pattern mirrors ``f1040_field_map.py`` / ``w4p_field_map.py``: a
``dict[str, str]`` of logical key -> the fully-qualified AcroForm field name,
plus a Yes/No checkbox map for the Part III questions.

Key conventions
---------------
- Dollar amounts are written as plain strings ("12500" — no $ or commas). The
  IRS AcroForm boxes are plain-text, not numeric.
- The ``f1_NN`` numbering increments in document/tab order, so field numbers do
  NOT map 1:1 to line numbers — every name below was pinned positionally.
- Part I line 1 is a 14-row payer list (name box + amount box per row); Part II
  line 5 is a 15-row payer list. The FIRST name box of each list carries an
  extra read-order subform wrapper (``Line1_ReadOrder[0]`` / ``ReadOrderControl[0]``)
  because the printed label sits inline with row 1; every other field is directly
  under ``Page1[0]``. Verified against the dump.
- The taxpayer's SSN (field ``f1_02``) is intentionally OMITTED from this map —
  it is left blank for the taxpayer to enter, like the SSN on Form 1040.
- Part III is three independent Yes/No questions; each Yes and each No is a
  separate one-state /Btn (Yes on-state /1, No on-state /2). Set at most one per
  question; leave the others /Off (conservative blank-fill).

CPA/EA verification note
------------------------
These names match the TY2025 f1040sb.pdf as distributed on irs.gov. If a later
revision (or a prior-year ``f1040sb_{year}.pdf``) is dropped into
``data/irs_forms/``, re-run the dump and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# Pre-printed payer rows in each list section (verified against the /Rect dump).
_INTEREST_ROWS = 14   # Part I, Line 1 — interest payers (f1_03/f1_04 .. f1_29/f1_30)
_DIVIDEND_ROWS = 15   # Part II, Line 5 — dividend payers (f1_34/f1_35 .. f1_62/f1_63)


def _fld(n: int, wrapper: str | None = None) -> str:
    """Fully-qualified name for the ``f1_NN[0]`` field, optionally under a
    read-order subform wrapper (used by the first row of each payer list)."""
    base = f"f1_{n:02d}[0]"
    return f"{_P1}.{wrapper}.{base}" if wrapper else f"{_P1}.{base}"


def _build_schb_fields() -> dict[str, str]:
    """Build the logical-key -> AcroForm-field-name map (built once at import)."""
    fields: dict[str, str] = {
        # Identity — "Name(s) shown on return". (SSN field f1_02 is OMITTED on
        # purpose — left blank for the taxpayer.)
        "name_shown_on_return": _fld(1),
    }

    # --- Part I — Interest: Line 1 payer list (name + amount per row) ---
    for k in range(_INTEREST_ROWS):
        wrap = "Line1_ReadOrder[0]" if k == 0 else None  # only row 1 is wrapped
        fields[f"interest_payer_{k + 1}_name"] = _fld(3 + 2 * k, wrap)
        fields[f"interest_payer_{k + 1}_amount"] = _fld(4 + 2 * k)
    # Part I totals.
    fields["interest_total_2"] = _fld(31)        # Line 2 — add the amounts on line 1
    fields["excludable_interest_3"] = _fld(32)   # Line 3 — excludable interest (Form 8815)
    fields["interest_taxable_4"] = _fld(33)      # Line 4 — line 2 - line 3 -> 1040 line 2b

    # --- Part II — Ordinary Dividends: Line 5 payer list ---
    for k in range(_DIVIDEND_ROWS):
        wrap = "ReadOrderControl[0]" if k == 0 else None  # only row 1 is wrapped
        fields[f"dividend_payer_{k + 1}_name"] = _fld(34 + 2 * k, wrap)
        fields[f"dividend_payer_{k + 1}_amount"] = _fld(35 + 2 * k)
    # Part II total.
    fields["dividends_total_6"] = _fld(64)       # Line 6 — add the amounts on line 5 -> 1040 line 3b

    # --- Part III — Foreign Accounts and Trusts: Line 7b foreign-country name(s) ---
    fields["foreign_country_7b"] = _fld(65)
    fields["foreign_country_7b_continued"] = _fld(66)

    return fields


# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
SCHB_FIELDS: dict[str, str] = _build_schb_fields()

# Part III Yes/No questions: logical key -> {answer -> (field name, on-state)}.
# Each Yes/No is an independent one-state /Btn (Yes /1, No /2). The Part III
# Line 7a foreign-account Yes/No pair lives under the ``TagcorrectingSubform[0]``
# wrapper; the FinCEN-required and Line 8 foreign-trust pairs are directly under
# Page1[0]. Verified positionally against the dump.
_TAG = "TagcorrectingSubform[0]"
SCHB_YESNO_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    # Line 7a — financial interest in / signature authority over a foreign account?
    "foreign_account_7a": {
        "yes": (f"{_P1}.{_TAG}.c1_1[0]", "1"),
        "no":  (f"{_P1}.{_TAG}.c1_1[1]", "2"),
    },
    # Line 7a (second question) — required to file FinCEN Form 114 (FBAR)?
    "fincen_required_7a": {
        "yes": (f"{_P1}.c1_2[0]", "1"),
        "no":  (f"{_P1}.c1_2[1]", "2"),
    },
    # Line 8 — received a distribution from / grantor of / transferor to a foreign trust?
    "foreign_trust_8": {
        "yes": (f"{_P1}.c1_3[0]", "1"),
        "no":  (f"{_P1}.c1_3[1]", "2"),
    },
}

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Schedule B fields.
VALID_SCHB_FIELDS: frozenset[str] = frozenset(
    list(SCHB_FIELDS.values())
    + [name for question in SCHB_YESNO_FIELDS.values() for (name, _state) in question.values()]
)
