"""Compute a Form 1040 from a client's income figures (the calculator path).

Bridges raw income inputs (each tagged with a ``source_ref`` — a W-2/1099
document, or an explicit user entry) → the vendored deterministic engine
(``calculate_form1040``) → a WorkProduct ``inputs`` map keyed by the f1040 field
map's logical keys. This is what turns the 1040 from a *filler* into a
*calculator*: the computed lines (AGI, taxable income, tax, total tax) are
produced by the engine, NOT typed by hand and NOT fabricated.

Every computed line carries ``source_ref = "computed:form1040@<year>"`` and
``origin = "computed"`` so it passes the work-product provenance guard; the
income lines carry the caller's source_ref (the underlying document). Money is
whole dollars on the form (the IRS rounds to dollars); the engine works in cents.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.tax.engines.configs_1040 import tax_config_for
from app.tax.engines.form1040 import Form1040Inputs, calculate_form1040

# Raw income key -> Form1040Inputs field (all dollar amounts).
_INCOME_FIELDS = (
    "wages", "taxable_interest", "ordinary_dividends", "qualified_dividends",
    "ira_taxable", "pensions_taxable", "social_security_taxable",
    "capital_gain_loss", "schedule_1_income", "adjustments",
    "itemized_deduction", "qbi_deduction", "nonrefundable_credits",
    "withholding", "estimated_payments",
)

VALID_FILING_STATUSES = ("single", "mfj", "mfs", "hoh", "qss")


def _dollars_to_cents(value: Any) -> int:
    s = str(value).strip().replace(",", "").replace("$", "")
    if s == "":
        return 0
    return int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _cents_to_dollar_str(cents: int) -> str:
    return str(int(Decimal(cents).quantize(Decimal("100"), rounding=ROUND_HALF_UP) / 100))


def _rec(value_cents: int, source_ref: str, origin: str) -> dict[str, str]:
    return {"value": _cents_to_dollar_str(value_cents), "source_ref": source_ref, "origin": origin}


def compute_1040(
    tax_year: int,
    filing_status: str,
    income: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compute a 1040 and return the WorkProduct payload.

    Args:
        tax_year: 2025 or 2026 (raises on an unsupported year).
        filing_status: single|mfj|mfs|hoh|qss.
        income: {raw_key: {"value": <dollars>, "source_ref": "..."}} for any of
            the _INCOME_FIELDS. Missing keys are treated as 0 (not fabricated —
            absence means the client had none, which the preparer confirms).

    Returns:
        {"inputs": <f1040 field-map keyed provenance inputs>,
         "provenance": {...engine result summary + assumptions + reconciliation},
         "result": <Form1040Result as dict>}.

    Raises ValueError on an unsupported year/status.
    """
    fs = (filing_status or "").strip().lower()
    if fs not in VALID_FILING_STATUSES:
        raise ValueError(f"filing_status must be one of {VALID_FILING_STATUSES}; got {filing_status!r}")

    config = tax_config_for(tax_year, fs)  # raises on unsupported year

    # Build engine inputs (cents) + remember each line's source_ref.
    raw_cents: dict[str, int] = {}
    source_of: dict[str, str] = {}
    for key in _INCOME_FIELDS:
        rec = income.get(key) or {}
        val = rec.get("value")
        if val in (None, ""):
            continue
        raw_cents[key] = _dollars_to_cents(val)
        source_of[key] = str(rec.get("source_ref") or "user_entered")

    engine_inputs = Form1040Inputs(
        filing_status=fs,
        wages_cents=raw_cents.get("wages", 0),
        taxable_interest_cents=raw_cents.get("taxable_interest", 0),
        ordinary_dividends_cents=raw_cents.get("ordinary_dividends", 0),
        qualified_dividends_cents=raw_cents.get("qualified_dividends", 0),
        ira_taxable_cents=raw_cents.get("ira_taxable", 0),
        pensions_taxable_cents=raw_cents.get("pensions_taxable", 0),
        social_security_taxable_cents=raw_cents.get("social_security_taxable", 0),
        capital_gain_loss_cents=raw_cents.get("capital_gain_loss", 0),
        schedule_1_income_cents=raw_cents.get("schedule_1_income", 0),
        adjustments_cents=raw_cents.get("adjustments", 0),
        itemized_deduction_cents=raw_cents.get("itemized_deduction", 0),
        qbi_deduction_cents=raw_cents.get("qbi_deduction", 0),
        nonrefundable_credits_cents=raw_cents.get("nonrefundable_credits", 0),
        withholding_cents=raw_cents.get("withholding", 0),
        estimated_payments_cents=raw_cents.get("estimated_payments", 0),
    )
    result = calculate_form1040(engine_inputs, config)

    computed_ref = f"computed:form1040@{tax_year}"
    inputs: dict[str, dict[str, str]] = {}

    # Income lines — carry the client's own source_ref (the underlying document).
    def income_line(field_key: str, raw_key: str) -> None:
        if raw_key in raw_cents and raw_cents[raw_key] != 0:
            inputs[field_key] = _rec(raw_cents[raw_key], source_of[raw_key], "document")

    income_line("wages_1a", "wages")
    income_line("total_wages_1z", "wages")
    income_line("taxable_interest_2b", "taxable_interest")
    income_line("ordinary_div_3b", "ordinary_dividends")
    income_line("ira_distrib_4b", "ira_taxable")
    income_line("pensions_5b", "pensions_taxable")
    income_line("social_security_6b", "social_security_taxable")
    income_line("capital_gain_7", "capital_gain_loss")
    income_line("additional_income_8", "schedule_1_income")
    income_line("adjustments_10", "adjustments")
    income_line("federal_withholding_25a", "withholding")

    # Computed lines — produced by the engine (provenance = the engine).
    inputs["total_income_9"] = _rec(result.total_income_cents, computed_ref, "computed")
    inputs["agi_11"] = _rec(result.agi_cents, computed_ref, "computed")
    inputs["std_or_itemized_12"] = _rec(result.deduction_cents, computed_ref, "computed")
    inputs["taxable_income_15"] = _rec(result.taxable_income_cents, computed_ref, "computed")
    inputs["tax_16"] = _rec(result.ordinary_tax_cents + result.preferential_tax_cents, computed_ref, "computed")
    inputs["total_tax_24"] = _rec(result.total_tax_cents, computed_ref, "computed")

    # Filing status checkbox (user_entered election).
    fs_src = str((income.get("filing_status") or {}).get("source_ref") or "user_entered")
    inputs["filing_status"] = {"value": fs, "source_ref": fs_src, "origin": "user_entered"}

    provenance = {
        "form": "1040",
        "tax_year": tax_year,
        "engine": "app.tax.engines.form1040 (vendored from TPO; Rev. Proc. 2024-40 / 2025-32 / OBBBA)",
        "filing_status": fs,
        "computed": {
            "total_income": _cents_to_dollar_str(result.total_income_cents),
            "agi": _cents_to_dollar_str(result.agi_cents),
            "deduction": _cents_to_dollar_str(result.deduction_cents),
            "taxable_income": _cents_to_dollar_str(result.taxable_income_cents),
            "total_tax": _cents_to_dollar_str(result.total_tax_cents),
            "balance_due": _cents_to_dollar_str(result.balance_due_cents),
            "marginal_rate": str(result.marginal_rate),
            "effective_rate": str(round(result.effective_rate, 4)),
        },
        "assumptions": result.assumptions,
        "reconciliation": [r.model_dump() for r in result.reconciliation],
        "ssn_note": "SSN and signature are left blank for the taxpayer to complete.",
    }
    return {"inputs": inputs, "provenance": provenance, "result": result.model_dump()}
