"""Form 1120 (U.S. Corporation Income Tax Return) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute a C-corporation return and does NOT fabricate any figure. Each
input value carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
corporation's books / their documents give us onto the official form" surface,
not a tax-calculation engine.

Conservative blank-fill
-----------------------
Any line whose input is absent (or blank) is left blank. We never guess, never
insert a zero, and never derive one line from another. A logical key with no
AcroForm mapping on the current form revision is simply skipped.

Left blank for the human
------------------------
The corporation's **EIN** is intentionally LEFT BLANK by this renderer even if
supplied — it is an identity item the human enters on the final form (consistent
with the 1120-S / 1065 / 1040 renderers, which omit the EIN / SSN). The officer
signature / title, the "May the IRS discuss this return with the preparer"
authorization, and the paid-preparer block are likewise not filled here.

Scope
-----
This renderer fills, via the EXISTING ``form_1120_field_map``:

  * **page 1 — the return face**: the identity header (corp name, dates,
    business code, state of incorporation, total assets, number of
    shareholders) and the five page-1 status checkboxes (initial / final / name
    change / address change / amended return);
  * the **income lines** (1a–11) and **deduction / tax-and-payment lines**
    (12–38), keyed by their bare form line number ("1a", "2", … "31", … "38"); and
  * **Schedule L** (the balance sheet, end-of-year column) — either by a
    Schedule-L field key directly (``"1_eoy"``, ``"15_eoy"`` …) or by a
    ``"schedule_l"``-prefixed line input routed through
    ``SCHEDULE_L_LINE_TO_FIELD``.

The supporting schedules not covered by the field map (Schedule C dividends,
Schedule J tax computation, Schedule K other information, M-1 / M-2) remain blank
for the preparer.

XFA-hybrid form
---------------
Form 1120 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` field naming,
like the W-4P / 1040 / 1065 / 1120-S), so it is filled via
``form_prefill._render_acroform_pdf`` (the XFA-safe /Fields-tree walker that drops
the XFA layer and BAKES each value in as a flattened, visible overlay so it
renders in every viewer/printer), NOT the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.form_1120_fill import render_1120

    pdf_bytes, content_type = render_1120(
        tax_year=2024,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "corp_name":        {"value": "Acme Manufacturing Inc", "source_ref": "intake#1", "origin": "user_entered"},
            "date_incorporated":{"value": "2014-03-01",            "source_ref": "intake#1", "origin": "user_entered"},
            "business_code":    {"value": "339900",                "source_ref": "intake#1", "origin": "user_entered"},
            "total_assets":     {"value": "8200000",               "source_ref": "books-2024", "origin": "document"},
            "1a":               {"value": "12500000", "source_ref": "books-2024", "origin": "document"},
            "2":                {"value": "7400000",  "source_ref": "books-2024", "origin": "document"},
            "11":               {"value": "5200000",  "source_ref": "books-2024", "origin": "computed"},
            "12":               {"value": "650000",   "source_ref": "books-2024", "origin": "document"},
            "27":               {"value": "4100000",  "source_ref": "books-2024", "origin": "computed"},
            "28":               {"value": "1100000",  "source_ref": "books-2024", "origin": "computed"},
            "30":               {"value": "1100000",  "source_ref": "books-2024", "origin": "computed"},
            "31":               {"value": "231000",   "source_ref": "books-2024", "origin": "computed"},
            "final_return":     {"value": "true",     "source_ref": "intake#1",   "origin": "user_entered"},
        },
    )
"""

from __future__ import annotations

from pathlib import Path

from app.exports.form_prefill import (
    IRS_FORMS_DIR,
    WatermarkMode,
    _render_acroform_pdf,
)
from app.exports.form_1120_field_map import (
    FORM_1120_FIELDS,
    FORM_1120_HEADER_FIELDS,
    SCHEDULE_L_FIELDS,
    SCHEDULE_L_LINE_TO_FIELD,
)

# Strings that mean "set this independent boolean checkbox".
_TRUTHY = frozenset({"true", "1", "yes", "y", "on", "x", "checked"})

# The five page-1 status checkboxes live INSIDE FORM_1120_HEADER_FIELDS (mapping
# to the form's ``c1_*`` /Btn fields). They are boolean — set the box only when
# the logical key is present with a truthy value; everything else in the header
# map is a plain-text field. Their AcroForm on-state is "1" (the walker adds the
# leading slash: "1" -> /1).
_HEADER_CHECKBOX_KEYS = frozenset(
    {"initial_return", "final_return", "name_change", "address_change", "amended_return"}
)
_CHECKBOX_ON_STATE = "1"

# Identity item left blank for the human even if supplied (mirrors 1120-S / 1065
# / 1040). It IS present in FORM_1120_HEADER_FIELDS, so we must explicitly skip it.
_LEAVE_BLANK_FOR_HUMAN = frozenset({"ein"})

# Prefix on a logical key that routes a bare Schedule L line number (e.g.
# "schedule_l_10a") through SCHEDULE_L_LINE_TO_FIELD to its end-of-year field.
_SCHEDULE_L_PREFIX = "schedule_l_"


def _f1120_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 1120 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f1120_{tax_year}.pdf`` first (when a tax year is
    given), then falls back to the unversioned ``data/irs_forms/f1120.pdf``.
    Mirrors ``f1120s_fill._f1120s_form_path`` / ``f1065_fill._f1065_form_path``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f1120_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f1120.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def render_1120(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 1120 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line/box is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f1120_{tax_year}.pdf`` then
        ``f1120.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 / 1065 / 1120-S watermark
        behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from the Form 1120 field map:

          - an identity header key in ``FORM_1120_HEADER_FIELDS`` — e.g.
            ``"corp_name"``, ``"date_incorporated"``, ``"business_code"``,
            ``"state_inc"``, ``"total_assets"``, ``"tax_year_begin"``,
            ``"tax_year_end"``, ``"num_shareholders"``;
          - one of the five page-1 status checkboxes (``"initial_return"``,
            ``"final_return"``, ``"name_change"``, ``"address_change"``,
            ``"amended_return"``) — a truthy value (``true`` / ``1`` / ``yes`` /
            ``x`` …) checks that box; anything else leaves it /Off;
          - an income / deduction / tax line keyed by its bare form line number
            in ``FORM_1120_FIELDS`` — ``"1a"``, ``"1b"``, ``"1c"``, ``"2"``,
            ``"3"`` … ``"11"`` (income); ``"12"`` … ``"27"`` (deductions);
            ``"28"`` (taxable income before NOL), ``"29a"``/``"29b"``/``"29c"``,
            ``"30"`` (taxable income), ``"31"`` (total tax), ``"33"`` … ``"38"``;
          - a Schedule L (balance-sheet, end-of-year) value — either a Schedule-L
            field key directly (``"1_eoy"``, ``"15_eoy"``, ``"28_eoy"`` …) or a
            ``"schedule_l_<line>"`` key (``"schedule_l_10a"``) routed through
            ``SCHEDULE_L_LINE_TO_FIELD``.

        Provenance (``source_ref``) is validated upstream; this renderer trusts
        the caller. A blank/empty value is skipped.

        Notes:
          - The corporation's EIN is NOT filled here even if supplied — it is an
            identity item for the human (left blank, like the 1120-S / 1065 EIN).
          - An unknown / unmapped key is silently skipped (conservative — never
            invent a field, never write to a line we don't recognize).

    Raises
    ------
    FileNotFoundError
        If no Form 1120 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f1120_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 1120 PDF not found at {source_path}. "
            "Run scripts/download_irs_forms.sh to fetch it."
        )
    source_bytes = source_path.read_bytes()

    field_values: dict[str, str] = {}

    for key, payload in inputs.items():
        # Each input payload is a dict with at least a "value"; provenance was
        # validated upstream. A missing/blank value means "leave it blank".
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if value is None or str(value) == "":
            continue
        value = str(value)

        # Identity item left blank for the human even if supplied.
        if key in _LEAVE_BLANK_FOR_HUMAN:
            continue

        # Page-1 status checkbox (initial / final / name / address / amended): a
        # truthy value checks it; otherwise leave it /Off.
        if key in _HEADER_CHECKBOX_KEYS:
            if value.strip().lower() in _TRUTHY:
                acro_name = FORM_1120_HEADER_FIELDS.get(key)
                if acro_name:
                    field_values[acro_name] = _CHECKBOX_ON_STATE
            continue

        # Schedule L line routed by bare line number (e.g. "schedule_l_10a").
        if key.startswith(_SCHEDULE_L_PREFIX):
            line = key[len(_SCHEDULE_L_PREFIX):].strip().lower()
            field_key = SCHEDULE_L_LINE_TO_FIELD.get(line)
            acro_name = SCHEDULE_L_FIELDS.get(field_key) if field_key else None
            if acro_name:
                field_values[acro_name] = value
            continue

        # Schedule L field key supplied directly (e.g. "15_eoy").
        if key in SCHEDULE_L_FIELDS:
            field_values[SCHEDULE_L_FIELDS[key]] = value
            continue

        # Identity header text field (corp name, dates, codes, totals).
        acro_name = FORM_1120_HEADER_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value
            continue

        # Ordinary income / deduction / tax line keyed by bare line number.
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = FORM_1120_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"


__all__ = ["render_1120", "_f1120_form_path", "WatermarkMode"]
