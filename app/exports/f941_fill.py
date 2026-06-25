"""Form 941 (Employer's QUARTERLY Federal Tax Return) form-fill renderer.

Part of the autonomous professional-substitute suite. This renderer FILLS THE
OFFICIAL FORM FROM CALLER-PROVIDED, PROVENANCE-BEARING INPUTS — it does NOT
auto-compute the return and does NOT fabricate any figure. Each input value
carries a ``source_ref`` (validated upstream, e.g. by
``app.api.work_products._assert_provenance``). This is the honest "fill what the
employer / their payroll records give us onto the official form" surface, not a
payroll-tax engine: the social-security / Medicare tax columns (lines 5a-5d
Column 2), the totals (lines 5e, 6, 10, 12), and the balance due / overpayment
are transcribed from the figures the caller supplies, never computed here.

Conservative blank-fill
-----------------------
Any field whose input is absent is left blank. We never guess, never insert a
zero, and never derive one line from another. A logical key with no mapping on
the current form revision is simply skipped.

Money lines (dollars + cents)
-----------------------------
Each Form 941 dollar amount is two adjacent boxes (whole dollars and 2-digit
cents). For a money key the renderer splits a supplied amount like ``"12345.67"``
into the dollars box (``12345``) and the cents box (``67``) — pure formatting of
the provided figure onto the form's two-box layout, never a computation. An
amount with no fractional part leaves the cents box blank.

Employer-completed fields
-------------------------
The Employer Identification Number (EIN) and every wet-ink signature / signature
date are intentionally LEFT BLANK by this renderer even if data were available —
they are identity / wet-ink items the filer must enter and sign themselves. (The
EIN appears in the entity area and atop pages 2 & 3 and on the 941-V voucher; the
field map deliberately does not map any of them.)

XFA-hybrid form
---------------
Form 941 is a modern XFA-hybrid AcroForm (``topmostSubform[0]`` naming, like the
W-4P / 1040), so it is filled via ``form_prefill._render_acroform_pdf`` (the
XFA-safe /Fields-tree walker that bakes each value in as a flattened, visible
overlay), NOT the older page-based ``_fill_pdf_fields``.

Usage
-----
    from app.exports.form_prefill import WatermarkMode
    from app.exports.f941_fill import render_941

    pdf_bytes, content_type = render_941(
        tax_year=2026,
        watermark_mode=WatermarkMode.DRAFT,
        inputs={
            "name":      {"value": "Acme Widgets LLC", "source_ref": "intake#1", "origin": "user_entered"},
            "quarter":   {"value": "2", "source_ref": "intake#1", "origin": "user_entered"},
            "line1_num_employees":           {"value": "12", "source_ref": "payroll-q2", "origin": "document"},
            "line2_wages":                   {"value": "185000.00", "source_ref": "payroll-q2", "origin": "document"},
            "line3_fed_income_tax_withheld": {"value": "21450.00", "source_ref": "payroll-q2", "origin": "document"},
            "deposit_schedule":              {"value": "monthly", "source_ref": "intake#1", "origin": "user_entered"},
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
from app.exports.f941_field_map import (
    FORM_941_AGGREGATE_FILER_FIELDS,
    FORM_941_CENTS_FIELDS,
    FORM_941_DEPOSIT_SCHEDULE_FIELDS,
    FORM_941_DESIGNEE_FIELDS,
    FORM_941_FIELDS,
    FORM_941_LINE15B_FIELDS,
    FORM_941_LINE15D_ACCOUNT_TYPE,
    FORM_941_NAME_FIELDS,
    FORM_941_QUARTER_FIELDS,
    FORM_941_SINGLE_CHECKBOXES,
)

# Truthy strings that flip a single (boolean) checkbox on.
_TRUTHY = {"true", "1", "yes", "y", "x", "on", "checked"}

# Logical keys that select one of a set of mutually-exclusive checkboxes.
_CHOICE_KEYS: dict[str, dict[str, tuple[str, str]]] = {
    "line15b_overpayment_choice": FORM_941_LINE15B_FIELDS,
    "line15d_account_type":       FORM_941_LINE15D_ACCOUNT_TYPE,
    "deposit_schedule":           FORM_941_DEPOSIT_SCHEDULE_FIELDS,
    "designee_authorized":        FORM_941_DESIGNEE_FIELDS,
    "aggregate_filer_type":       FORM_941_AGGREGATE_FILER_FIELDS,
}


def _f941_form_path(tax_year: int | None) -> Path:
    """Resolve the Form 941 source PDF, preferring a year-specific file.

    Tries ``data/irs_forms/f941_{tax_year}.pdf`` first (when a tax year is given),
    then falls back to the unversioned ``data/irs_forms/f941.pdf``.
    """
    candidates: list[Path] = []
    if tax_year is not None:
        candidates.append(IRS_FORMS_DIR / f"f941_{tax_year}.pdf")
    candidates.append(IRS_FORMS_DIR / "f941.pdf")
    for p in candidates:
        if p.exists():
            return p
    return candidates[-1]


def _split_amount(raw: str) -> tuple[str | None, str | None]:
    """Split a supplied dollar amount into (whole_dollars, cents) display strings.

    Strips ``$``, commas, and spaces, then separates on the decimal point. The
    cents portion is padded/truncated to exactly two digits. This is faithful
    FORMATTING of the caller's figure onto the form's two-box layout — it never
    rounds the dollars or invents a value. Returns ``(None, None)`` for an empty
    or sign-only string; ``(dollars, None)`` when there is no fractional part.

    Examples:
        "12345.67" -> ("12345", "67")
        "12,345"   -> ("12345", None)
        "-50.2"    -> ("-50", "20")
        ".75"      -> ("0", "75")
    """
    s = raw.replace("$", "").replace(",", "").replace(" ", "").strip()
    if s in ("", "-", ".", "-.", "+"):
        return (None, None)
    neg = s.startswith("-")
    if neg or s.startswith("+"):
        s = s[1:]
    if "." in s:
        dollars, cents = s.split(".", 1)
        dollars = dollars or "0"
        cents = (cents + "00")[:2] if cents else None
    else:
        dollars, cents = s, None
    if neg:
        dollars = "-" + dollars
    return (dollars, cents)


def render_941(
    tax_year: int | None,
    watermark_mode: WatermarkMode,
    inputs: dict,
) -> tuple[bytes, str]:
    """Fill an official Form 941 from provenance-bearing ``inputs``.

    Returns ``(pdf_bytes, "application/pdf")``. Fills only the fields present in
    ``inputs``; every other line is left blank (conservative blank-fill — no
    figure is ever guessed, derived, or zero-filled).

    Parameters
    ----------
    tax_year:
        Used to resolve a year-specific source PDF (``f941_{tax_year}.pdf`` then
        ``f941.pdf``). May be ``None``.
    watermark_mode:
        DRAFT (un-attested), SELF_CERTIFIED (a user with no available tax
        professional has self-certified the work product), or NONE (a licensed
        pro has finalized it). Mirrors the W-4P / 1040 watermark behavior.
    inputs:
        ``{field_key: {"value": str, "source_ref": str, "origin": str}}`` where
        ``field_key`` is a LOGICAL key from ``FORM_941_FIELDS`` (e.g.
        ``"line2_wages"``, ``"line3_fed_income_tax_withheld"``,
        ``"line16_total_liability"``) OR one of the special keys:

          - ``"name"`` — value is the employer's legal name; written to all three
            name boxes (page-1 entity area, page-2 header, 941-V voucher).
          - ``"quarter"`` — value is ``"1"``/``"2"``/``"3"``/``"4"``; checks the
            page-1 "Report for this Quarter" box AND the 941-V tax-period circle.
          - ``"deposit_schedule"`` — ``line12_under_2500`` | ``monthly`` |
            ``semiweekly`` (Part 2, line 16).
          - ``"line15b_overpayment_choice"`` — ``apply_to_next_return`` |
            ``send_refund``.
          - ``"line15d_account_type"`` — ``checking`` | ``savings``.
          - ``"designee_authorized"`` — ``yes`` | ``no`` (Part 4).
          - ``"aggregate_filer_type"`` — ``section_3504_agent`` | ``cpeo`` |
            ``other_third_party``.
          - boolean checkbox keys (truthy value sets them): ``line4_no_wages_subject``,
            ``line17_business_closed``, ``line18_seasonal``, ``preparer_self_employed``.

        Money values may include a decimal (``"185000.00"``) — the dollars and
        cents are placed in their respective boxes. Provenance (``source_ref``) is
        validated upstream; this renderer trusts the caller. A blank/empty value
        is skipped.

        Notes:
          - The EIN and the wet-ink signatures / signature dates are NOT filled
            here even if supplied — they are identity / wet-ink items for the
            employer.

    Raises
    ------
    FileNotFoundError
        If no Form 941 source PDF is present in ``data/irs_forms/``.
    """
    source_path = _f941_form_path(tax_year)
    if not source_path.exists():
        raise FileNotFoundError(
            f"IRS Form 941 PDF not found at {source_path}. "
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
        value = str(value).strip()
        if value == "":
            continue

        # --- Employer name: fan out to all three name boxes. ---
        if key == "name":
            for fname in FORM_941_NAME_FIELDS:
                field_values[fname] = value
            continue

        # --- Quarter: page-1 header box + 941-V voucher circle. ---
        if key == "quarter":
            for fname, on_state in FORM_941_QUARTER_FIELDS.get(value.strip(), []):
                field_values[fname] = on_state
            continue

        # --- Mutually-exclusive choice checkboxes. ---
        choice_map = _CHOICE_KEYS.get(key)
        if choice_map is not None:
            mapped = choice_map.get(value.strip().lower())
            if mapped:
                fname, on_state = mapped
                field_values[fname] = on_state
            continue

        # --- Boolean checkboxes (set when truthy). ---
        single = FORM_941_SINGLE_CHECKBOXES.get(key)
        if single is not None:
            if value.strip().lower() in _TRUTHY:
                fname, on_state = single
                field_values[fname] = on_state
            continue

        # --- Money lines: split into dollars + cents boxes. ---
        if key in FORM_941_CENTS_FIELDS:
            dollars, cents = _split_amount(value)
            if dollars is not None:
                field_values[FORM_941_FIELDS[key]] = dollars
            if cents is not None:
                field_values[FORM_941_CENTS_FIELDS[key]] = cents
            continue

        # --- Ordinary text line: map the logical key to its AcroForm field. ---
        # Unknown / unmapped keys are skipped (conservative — never invent a
        # field, never write to a line we don't recognize).
        acro_name = FORM_941_FIELDS.get(key)
        if acro_name:
            field_values[acro_name] = value

    return _render_acroform_pdf(source_bytes, field_values, watermark_mode), "application/pdf"
