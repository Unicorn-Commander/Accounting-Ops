"""IRS Form 7004 (Automatic Extension of Time To File Certain Business Income Tax Returns) — AcroForm field map.

Field names were verified by dumping the official fillable PDF
(``data/irs_forms/f7004.pdf``, **Rev. December 2025** face as currently
distributed on irs.gov) with ``pypdf`` (``PdfReader().get_fields()``) and
pinning every terminal widget's /Rect and /TU tooltip against the printed line
labels — a positional audit, because the IRS ``f{page}_{NN}`` numbering
increments in document/tab order, NOT line order. Form 7004 is a modern
XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the W-4P / 1040), so it
MUST be filled via ``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-
tree walker), not the older page-based ``_fill_pdf_fields``.

Layout (single page; 28 fields, all on Page1)
---------------------------------------------
  Header: Name (f1_1) | Identifying number (f1_2)
          Number, street, room/suite: street (f1_3) | room or suite no. (f1_4)
          City or town (f1_5) | State or province (f1_6) | Country (f1_7) |
          ZIP or foreign postal code (f1_8)
  Part I:  Line 1 — form code, TWO single-digit comb boxes: digit 1 (f1_9),
           digit 2 (f1_10).
  Part II: Line 2 foreign-corporation checkbox (c1_1, on-state /1);
           Line 3 consolidated-group checkbox (c1_2, on-state /1);
           Line 4 Regs 1.6081-5 qualifier checkbox (c1_3, on-state /1);
           Line 5a — "calendar year 20[YY]" (f1_11), "or tax year beginning
           [month day] (f1_12), 20[YY] (f1_13), and ending [month day] (f1_14),
           20[YY] (f1_15)";
           Line 5b — short-tax-year reason boxes c1_4[0..4] with DISTINCT
           on-states /1../5 (initial / final / change in accounting period /
           consolidated return to be filed / other);
           Line 6 tentative total tax (f1_16); Line 7 total payments and
           credits (f1_17); Line 8 balance due (f1_18).

Conventions mirror ``f4868_field_map.py``: a ``dict[str, str]`` of logical key
-> the fully-qualified AcroForm field name. Dollar amounts are written as plain
strings ("12500" — no $ or commas); the IRS boxes are plain-text. Conservative
blank-fill: a line with no input value is simply not written.

The taxpayer's **identifying number** (EIN/SSN) is mapped here for the
positional record but is NEVER auto-filled — consistent with the 1040 / 1120 /
1120-S / 1065 / 4562 / 4868 renderers, identity numbers are the filer's own
entry. See ``F7004_TAXPAYER_COMPLETED_KEYS``.

CPA/EA verification note
------------------------
These names match the f7004.pdf currently distributed on irs.gov. The face
field names are stable across recent revisions, but if a later (or prior-year
``f7004_{year}.pdf``) PDF is dropped into ``data/irs_forms/``, re-run the dump
and re-pin any changed names before use.
"""

from __future__ import annotations

_P1 = "topmostSubform[0].Page1[0]"

# ---------------------------------------------------------------------------
# Plain-text fields: logical key -> fully-qualified AcroForm field name (/Tx).
# Widget rects (pts, y from page bottom) from the pypdf dump are noted so a
# reviewer can re-pin without re-deriving.
# ---------------------------------------------------------------------------
F7004_FIELDS: dict[str, str] = {
    # --- Header identification ---
    "name":       f"{_P1}.f1_1[0]",   # Name                       [87, 684..446, 698]
    "ein":        f"{_P1}.f1_2[0]",   # Identifying number         [446, 684..576, 698] (never auto-filled)
    "address":    f"{_P1}.f1_3[0]",   # Number and street          [87, 660..489, 674]
    "room_suite": f"{_P1}.f1_4[0]",   # Room or suite no.          [489, 660..576, 674]
    "city_town":  f"{_P1}.f1_5[0]",   # City or town               [87, 636..273, 650]
    "state":      f"{_P1}.f1_6[0]",   # State or province          [274, 636..381, 650]
    "country":    f"{_P1}.f1_7[0]",   # Country                    [382, 636..489, 650]
    "zip_code":   f"{_P1}.f1_8[0]",   # ZIP / foreign postal code  [489, 636..576, 650]

    # --- Part I, Line 1: form code — TWO single-digit comb boxes ---
    # The renderer also accepts the convenience key "form_code" with a 2-digit
    # value (e.g. "12" for Form 1120) and splits it across these two boxes.
    "form_code_digit1": f"{_P1}.f1_9[0]",   # Line 1 digit 1 of 2  [547, 601..562, 610]
    "form_code_digit2": f"{_P1}.f1_10[0]",  # Line 1 digit 2 of 2  [562, 601..576, 610]

    # --- Part II, Line 5a: tax-year dates ---
    # "The application is for calendar year 20[YY], or tax year beginning
    #  [month day], 20[YY], and ending [month day], 20[YY]."
    "calendar_year_yy":     f"{_P1}.f1_11[0]",  # calendar year, 2-digit YY   [223, 264..238, 276]
    "fiscal_year_begin":    f"{_P1}.f1_12[0]",  # beginning month and day     [331, 264..382, 276]
    "fiscal_year_begin_yy": f"{_P1}.f1_13[0]",  # beginning year, 2-digit YY  [403, 264..418, 276]
    "fiscal_year_end":      f"{_P1}.f1_14[0]",  # ending month and day        [475, 264..533, 276]
    "fiscal_year_end_yy":   f"{_P1}.f1_15[0]",  # ending year, 2-digit YY     [554, 264..569, 276]

    # --- Part II, Lines 6-8: dollar amounts (right column) ---
    "tentative_tax_6": f"{_P1}.f1_16[0]",  # Line 6 — tentative total tax        [504, 216..576, 228]
    "payments_7":      f"{_P1}.f1_17[0]",  # Line 7 — total payments and credits [504, 192..576, 204]
    "balance_due_8":   f"{_P1}.f1_18[0]",  # Line 8 — balance due                [504, 168..576, 180]
}

# ---------------------------------------------------------------------------
# Checkboxes: logical key -> (AcroForm field name, on-state).
#
# Lines 2-4 are independent /Btn boxes with on-states ["/1", "/Off"].
# Line 5b's five reason boxes are SEPARATE terminal fields c1_4[0]..c1_4[4]
# whose on-states are DISTINCT (/1../5) — verified from each field's
# /_States_. Setting one does not clear another; exclusivity (the printed
# form expects one reason) is the caller's responsibility.
# ---------------------------------------------------------------------------
F7004_CHECKBOX_FIELDS: dict[str, tuple[str, str]] = {
    # Line 2: foreign corporation that does not have an office in the US
    "foreign_corp_2": (f"{_P1}.c1_1[0]", "1"),      # [549, 337..558, 345]
    # Line 3: common parent of a group filing a consolidated return
    "consolidated_3": (f"{_P1}.c1_2[0]", "1"),      # [549, 313..558, 321]
    # Line 4: corporation/partnership qualifying under Regs 1.6081-5
    "regs_qualifier_4": (f"{_P1}.c1_3[0]", "1"),    # [549, 277..558, 285]
    # Line 5b: short tax year reason (each box has its OWN on-state)
    "short_year_initial":      (f"{_P1}.c1_4[0]", "1"),  # Initial return          [374, 254..382, 262]
    "short_year_final":        (f"{_P1}.c1_4[1]", "2"),  # Final return            [460, 254..469, 262]
    "short_year_accounting":   (f"{_P1}.c1_4[2]", "3"),  # Change in acct. period  [64, 242..73, 250]
    "short_year_consolidated": (f"{_P1}.c1_4[3]", "4"),  # Consolidated return     [216, 242..224, 250]
    "short_year_other":        (f"{_P1}.c1_4[4]", "5"),  # Other (attach expl.)    [374, 242..382, 250]
}

# Identity items the renderer never auto-fills, even if supplied — the filer
# enters these themselves (mirrors f4868's TAXPAYER_COMPLETED_KEYS and the
# 1040 / 1120 / 1120-S / 1065 / 4562 posture on EIN/SSN).
F7004_TAXPAYER_COMPLETED_KEYS: frozenset[str] = frozenset({"ein"})

# Every AcroForm field name this map can write — used by tests + the render-time
# provenance guard to validate that we only ever touch known Form 7004 fields.
VALID_F7004_FIELDS: frozenset[str] = frozenset(
    list(F7004_FIELDS.values())
    + [v[0] for v in F7004_CHECKBOX_FIELDS.values()]
)
