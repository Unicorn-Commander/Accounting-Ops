"""Compute a Form 4562 from a fixed-asset list (the depreciation calculator path).

Bridges asset records → the deterministic depreciation engine
(``app.tax.engines.depreciation``) → a WorkProduct ``inputs`` map keyed by the
Form 4562 field map's logical keys. This is what makes 4562 a *computed* form
rather than a *filler*: §179, the §168(k) special allowance and every MACRS line
are produced by the engine, NOT typed by hand and NOT fabricated.

Every computed line carries ``source_ref = "computed:form4562@<year>"`` and
``origin = "computed"`` so it passes the work-product provenance guard
(``work_product_service.normalize_provenance_fields`` /
``api.work_products._assert_provenance``). Asset identity lines (descriptions,
placed-in-service dates) carry the caller's own source_ref — the asset record
they came from.

Money is whole dollars on the form (the IRS rounds to dollars); the engine works
in integer cents throughout.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.exports.f4562_field_map import (
    DESCRIPTION_MAX_CHARS,
    revision_for_tax_year,
)
from app.tax.engines.configs_depreciation import config_for_year, passenger_auto_cap
from app.tax.engines.depreciation import (
    AssetInput,
    Convention,
    DepreciationInputs,
    DepreciationResult,
    compute_depreciation,
)


def _cents_to_dollar_str(cents: int) -> str:
    return str(int(Decimal(cents).quantize(Decimal("100"), rounding=ROUND_HALF_UP) / 100))


def _rec(value: str, source_ref: str, origin: str) -> dict[str, str]:
    return {"value": value, "source_ref": source_ref, "origin": origin}


def _money(cents: int, source_ref: str, origin: str = "computed") -> dict[str, str]:
    return _rec(_cents_to_dollar_str(cents), source_ref, origin)


def _clip(text: str) -> str:
    """Form 4562 description columns are narrow; clip rather than overflow."""
    text = (text or "").strip()
    return text if len(text) <= DESCRIPTION_MAX_CHARS else text[: DESCRIPTION_MAX_CHARS - 1] + "…"


# Convention / method strings as printed in Form 4562 Section B columns (e) and (f).
_CONVENTION_LABEL = {
    Convention.HALF_YEAR: "HY",
    Convention.MID_QUARTER: "MQ",
    Convention.MID_MONTH: "MM",
}


def build_4562_inputs(
    result: DepreciationResult,
    *,
    asset_source_refs: dict[str, str] | None = None,
    name: str | None = None,
    business_activity: str | None = None,
    identity_source_ref: str = "user_entered",
) -> dict[str, dict[str, str]]:
    """Turn a ``DepreciationResult`` into a provenance-bearing WorkProduct inputs map.

    ``asset_source_refs`` maps ``asset_id`` → the source_ref for that asset's
    identity data (e.g. the ``fixed_asset`` row id). Anything not supplied falls
    back to the computed ref, since the figure itself came from the engine.
    """
    tax_year = result.tax_year
    revision = revision_for_tax_year(tax_year)   # raises on an unmapped year
    ref = f"computed:form4562@{tax_year}"
    refs = asset_source_refs or {}
    form = result.form_4562
    inputs: dict[str, dict[str, str]] = {}

    # -- Header (identity; the caller's own provenance) ----------------------
    if name:
        inputs["name"] = _rec(name, identity_source_ref, "user_entered")
    if business_activity:
        inputs["business_activity"] = _rec(
            business_activity, identity_source_ref, "user_entered"
        )
    # The taxpayer's identifying number (EIN/SSN) is deliberately LEFT BLANK for
    # the human to enter, consistent with the 1040 / 1120 / 1065 renderers.

    # -- Part I — §179 -------------------------------------------------------
    inputs["1"] = _money(form.line_1_max_amount, ref)
    inputs["2"] = _money(form.line_2_total_179_property, ref)
    inputs["3"] = _money(form.line_3_threshold, ref)
    inputs["4"] = _money(form.line_4_reduction, ref)
    inputs["5"] = _money(form.line_5_dollar_limit, ref)
    for i, prop in enumerate(form.line_6_properties[:2], start=1):
        inputs[f"6_{i}_a"] = _rec(
            _clip(prop.description), refs.get(prop.description, ref), "computed"
        )
        inputs[f"6_{i}_b"] = _money(prop.cost_cents, ref)
        inputs[f"6_{i}_c"] = _money(prop.elected_cost_cents, ref)
    if form.line_7_listed_property:
        inputs["7"] = _money(form.line_7_listed_property, ref)
    inputs["8"] = _money(form.line_8_total_elected_cost, ref)
    inputs["9"] = _money(form.line_9_tentative_deduction, ref)
    if form.line_10_carryover_from_prior:
        inputs["10"] = _money(form.line_10_carryover_from_prior, ref)
    inputs["11"] = _money(form.line_11_business_income_limit, ref)
    inputs["12"] = _money(form.line_12_section_179_deduction, ref)
    if form.line_13_carryover_to_next:
        inputs["13"] = _money(form.line_13_carryover_to_next, ref)

    # -- Part II -------------------------------------------------------------
    if form.line_14_special_allowance:
        inputs["14"] = _money(form.line_14_special_allowance, ref)
    if form.line_16_other_depreciation:
        inputs["16"] = _money(form.line_16_other_depreciation, ref)

    # -- Part III Section A / B / C -----------------------------------------
    if form.line_17_prior_year_macrs:
        inputs["17"] = _money(form.line_17_prior_year_macrs, ref)

    for period, row in form.part_iii_section_b.items():
        if section_b_missing(revision, period):
            raise ValueError(
                f"Form 4562 (TY{tax_year}) has no Part III Section B row for a "
                f"{period}-year GDS class. Refusing to file it on another row."
            )
        base = f"sectionb_{period}"
        if row.month_year:
            inputs[f"{base}_b"] = _rec(row.month_year, ref, "computed")
        inputs[f"{base}_c"] = _money(row.basis_cents, ref)
        inputs[f"{base}_d"] = _rec(_period_text(row.recovery_period_years), ref, "computed")
        inputs[f"{base}_e"] = _rec(_CONVENTION_LABEL[row.convention], ref, "computed")
        inputs[f"{base}_f"] = _rec(row.method.value, ref, "computed")
        inputs[f"{base}_g"] = _money(row.deduction_cents, ref)

    for period, row in form.part_iii_section_c.items():
        base = f"sectionc_{period}"
        inputs[f"{base}_c"] = _money(row.basis_cents, ref)
        inputs[f"{base}_d"] = _rec(_period_text(row.recovery_period_years), ref, "computed")
        inputs[f"{base}_e"] = _rec(_CONVENTION_LABEL[row.convention], ref, "computed")
        inputs[f"{base}_g"] = _money(row.deduction_cents, ref)

    # -- Part IV Summary -----------------------------------------------------
    if form.line_21_listed_property:
        inputs["21"] = _money(form.line_21_listed_property, ref)
    inputs["22"] = _money(form.line_22_total, ref)

    # -- Part V Listed property ---------------------------------------------
    if form.line_25_listed_special_allowance:
        inputs["25"] = _money(form.line_25_listed_special_allowance, ref)
    for i, row in enumerate(form.line_26_over_50_pct[:3], start=1):
        src = refs.get(row.description, ref)
        inputs[f"listed26_{i}_a"] = _rec(_clip(row.description), src, "computed")
        inputs[f"listed26_{i}_b"] = _rec(row.placed_in_service.strftime("%m/%d/%y"), src, "computed")
        inputs[f"listed26_{i}_c"] = _rec(f"{row.business_use_pct.normalize()}%", ref, "computed")
        inputs[f"listed26_{i}_d"] = _money(row.cost_cents, ref)
        inputs[f"listed26_{i}_e"] = _money(row.basis_cents, ref)
        inputs[f"listed26_{i}_f"] = _rec(_period_text(row.recovery_period_years), ref, "computed")
        inputs[f"listed26_{i}_g"] = _rec(row.method_convention, ref, "computed")
        inputs[f"listed26_{i}_h"] = _money(row.deduction_cents, ref)
        if row.elected_179_cents:
            inputs[f"listed26_{i}_i"] = _money(row.elected_179_cents, ref)
    for i, row in enumerate(form.line_27_50_pct_or_less[:3], start=1):
        src = refs.get(row.description, ref)
        inputs[f"listed27_{i}_a"] = _rec(_clip(row.description), src, "computed")
        inputs[f"listed27_{i}_b"] = _rec(row.placed_in_service.strftime("%m/%d/%y"), src, "computed")
        inputs[f"listed27_{i}_c"] = _rec(f"{row.business_use_pct.normalize()}%", ref, "computed")
        inputs[f"listed27_{i}_d"] = _money(row.cost_cents, ref)
        inputs[f"listed27_{i}_e"] = _money(row.basis_cents, ref)
        inputs[f"listed27_{i}_f"] = _rec(_period_text(row.recovery_period_years), ref, "computed")
        inputs[f"listed27_{i}_g"] = _rec(row.method_convention, ref, "computed")
        inputs[f"listed27_{i}_h"] = _money(row.deduction_cents, ref)
    if form.line_28_total_listed:
        inputs["28"] = _money(form.line_28_total_listed, ref)
    if form.line_29_listed_179:
        inputs["29"] = _money(form.line_29_listed_179, ref)

    # -- Part VI Amortization ------------------------------------------------
    for i, row in enumerate(form.line_42_amortization[:2], start=1):
        src = refs.get(row.description, ref)
        inputs[f"amort42_{i}_a"] = _rec(_clip(row.description), src, "computed")
        inputs[f"amort42_{i}_b"] = _rec(row.begins.strftime("%m/%d/%y"), src, "computed")
        inputs[f"amort42_{i}_c"] = _money(row.amortizable_amount_cents, ref)
        inputs[f"amort42_{i}_d"] = _rec(row.code_section, src, "computed")
        inputs[f"amort42_{i}_e"] = _rec(f"{row.period_months} mo", ref, "computed")
        inputs[f"amort42_{i}_f"] = _money(row.amortization_cents, ref)
    if form.line_43_amortization_prior:
        inputs["43"] = _money(form.line_43_amortization_prior, ref)
    if form.line_44_amortization_total:
        inputs["44"] = _money(form.line_44_amortization_total, ref)

    return inputs


def section_b_missing(revision, period: str) -> bool:
    return revision.section_b.get(period) is None


def _period_text(period: Decimal) -> str:
    """Recovery period as printed in column (d), e.g. "5" or "27.5"."""
    return format(period.normalize(), "f")


def compute_4562(
    tax_year: int,
    assets: list[dict[str, Any]],
    *,
    business_taxable_income: Any | None = None,
    section_179_carryforward_in: Any = 0,
    other_depreciation: Any = 0,
    name: str | None = None,
    business_activity: str | None = None,
) -> dict[str, Any]:
    """Compute a Form 4562 and return the WorkProduct payload.

    Args:
        tax_year: a year supported by BOTH ``configs_depreciation`` (constants)
            and ``f4562_field_map`` (form layout). Raises otherwise.
        assets: list of asset dicts. Each needs ``asset_id``,
            ``placed_in_service`` (ISO date), ``cost`` (dollars),
            ``recovery_period_years``; optional ``description``, ``system``,
            ``business_use_pct``, ``is_listed_property``, ``section_179_elected``,
            ``bonus_elected_out``, ``prior_section_179``, ``prior_bonus``,
            ``convention``, ``source_ref``.
        business_taxable_income: dollars — REQUIRED whenever §179 is elected
            (§179(b)(3)(A)). The engine raises rather than guessing it.

    Returns:
        ``{"inputs": ..., "provenance": ..., "result": ...}``.

    Raises ValueError on an unsupported year or an inconsistent asset.
    """
    config = config_for_year(tax_year)          # raises on an unsupported year
    revision_for_tax_year(tax_year)             # raises on an unmapped layout

    engine_assets: list[AssetInput] = []
    source_refs: dict[str, str] = {}
    for raw in assets:
        asset = AssetInput(
            asset_id=str(raw["asset_id"]),
            description=str(raw.get("description") or ""),
            placed_in_service=raw["placed_in_service"],
            acquired_on=raw.get("acquired_on"),
            cost_cents=_dollars(raw["cost"]),
            recovery_period_years=Decimal(str(raw["recovery_period_years"])),
            system=raw.get("system", "GDS"),
            business_use_pct=Decimal(str(raw.get("business_use_pct", 100))),
            is_listed_property=bool(raw.get("is_listed_property", False)),
            listed_property_type=raw.get("listed_property_type"),
            section_179_elected_cents=_dollars(raw.get("section_179_elected", 0)),
            bonus_elected_out=bool(raw.get("bonus_elected_out", False)),
            prior_section_179_cents=(
                _dollars(raw["prior_section_179"])
                if raw.get("prior_section_179") is not None else None
            ),
            prior_bonus_cents=(
                _dollars(raw["prior_bonus"])
                if raw.get("prior_bonus") is not None else None
            ),
            convention=raw.get("convention"),
        )
        engine_assets.append(asset)
        if raw.get("source_ref"):
            source_refs[asset.description or asset.asset_id] = str(raw["source_ref"])

    result = compute_depreciation(
        DepreciationInputs(
            tax_year=tax_year,
            assets=engine_assets,
            business_taxable_income_cents=(
                _dollars(business_taxable_income)
                if business_taxable_income is not None else None
            ),
            section_179_carryforward_in_cents=_dollars(section_179_carryforward_in),
            other_depreciation_cents=_dollars(other_depreciation),
        ),
        config,
        config_for_year=config_for_year,
        auto_cap_for=passenger_auto_cap,
    )

    inputs = build_4562_inputs(
        result,
        asset_source_refs=source_refs,
        name=name,
        business_activity=business_activity,
    )
    provenance = {
        "form": "4562",
        "tax_year": tax_year,
        "engine": "app.tax.engines.depreciation",
        "form_revision": revision_for_tax_year(tax_year).key,
        "computed": {
            "total_depreciation": _cents_to_dollar_str(result.total_depreciation_cents),
            "section_179": _cents_to_dollar_str(
                result.form_4562.line_12_section_179_deduction
            ),
            "special_allowance": _cents_to_dollar_str(
                result.form_4562.line_14_special_allowance
                + result.form_4562.line_25_listed_special_allowance
            ),
            "amortization": _cents_to_dollar_str(result.total_amortization_cents),
            "section_179_carryforward_out": _cents_to_dollar_str(
                result.section_179_carryforward_out_cents
            ),
            "mid_quarter_applies": result.mid_quarter_applies,
        },
        "citations": result.citations,
        "assumptions": result.assumptions,
        "identifying_number_note": (
            "The taxpayer's EIN/SSN is intentionally left blank for the human."
        ),
    }
    return {"inputs": inputs, "provenance": provenance, "result": result.model_dump(mode="json")}


def _dollars(value: Any) -> int:
    """Whole-dollar (or decimal) input → integer cents."""
    if value is None:
        return 0
    s = str(value).strip().replace(",", "").replace("$", "")
    if s == "":
        return 0
    return int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


__all__ = ["build_4562_inputs", "compute_4562"]
