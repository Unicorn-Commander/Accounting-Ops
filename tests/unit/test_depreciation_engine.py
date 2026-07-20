"""Golden tests for the depreciation engine (Form 4562).

The MACRS assertions below are HAND-VERIFIABLE against IRS Publication 946
Appendix A. The engine implements Pub 946 ch. 4's "without using the tables"
method (declining balance switching to straight line); the published tables are
that same computation rounded to two decimals and then nudged so each column
sums to exactly 100.00%. So a single year may differ from the printed table by
up to 0.01 percentage point, while the LIFETIME total is exact. Every table
assertion therefore uses TABLE_TOLERANCE_PCT and is paired with an exact
lifetime-total assertion.

Sources cited per test:
  * IRS Pub 946 Appendix A, Tables A-1 … A-6
  * IRC §168(b) (method), §168(d) (conventions), §168(k) (special allowance)
  * IRC §179(b) (dollar + investment limits), §179(b)(3) (income limit/carryover)
  * IRC §280F (listed property)
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.tax.engines.depreciation import (
    AssetInput,
    BonusRule,
    Convention,
    DepreciationConfig,
    DepreciationInputs,
    DepreciationSystem,
    Method,
    compute_depreciation,
    macrs_schedule,
    mid_quarter_applies,
)

# One dollar of basis expressed in cents, scaled so a schedule entry read as a
# percentage has 4 decimal digits of resolution.
BASIS = 100_000_000          # $1,000,000.00 in cents
TABLE_TOLERANCE_PCT = Decimal("0.01")


def pct(schedule: list[int]) -> list[Decimal]:
    return [Decimal(x) * 100 / Decimal(BASIS) for x in schedule]


def assert_matches_table(computed: list[int], table: list[str], label: str) -> None:
    got = pct(computed)
    assert len(got) == len(table), (
        f"{label}: expected {len(table)} recovery years, got {len(got)}: {got}"
    )
    for i, (g, want) in enumerate(zip(got, table), start=1):
        assert abs(g - Decimal(want)) <= TABLE_TOLERANCE_PCT, (
            f"{label} year {i}: computed {g:.4f}% vs Pub 946 table {want}% "
            f"(tolerance {TABLE_TOLERANCE_PCT}%)"
        )
    assert sum(computed) == BASIS, f"{label}: lifetime total must equal basis exactly"


def cfg(
    year: int,
    *,
    limit: int = 100_000_000,
    threshold: int = 250_000_000,
    bonus: str = "0",
) -> DepreciationConfig:
    """An EXPLICIT config, so mechanics tests do not depend on real-year constants."""
    return DepreciationConfig(
        tax_year=year,
        section_179_limit_cents=limit,
        section_179_threshold_cents=threshold,
        bonus=BonusRule(percentage=Decimal(bonus), source="test fixture"),
        section_179_source="test fixture",
        bonus_source="test fixture",
    )


# ---------------------------------------------------------------------------
# Pub 946 Appendix A, Table A-1 — GDS, half-year convention
# ---------------------------------------------------------------------------

TABLE_A1 = {
    "3": (Decimal("3"), Method.DB_200,
          ["33.33", "44.45", "14.81", "7.41"]),
    "5": (Decimal("5"), Method.DB_200,
          ["20.00", "32.00", "19.20", "11.52", "11.52", "5.76"]),
    "7": (Decimal("7"), Method.DB_200,
          ["14.29", "24.49", "17.49", "12.49", "8.93", "8.92", "8.93", "4.46"]),
    "10": (Decimal("10"), Method.DB_200,
           ["10.00", "18.00", "14.40", "11.52", "9.22", "7.37", "6.55", "6.55",
            "6.56", "6.55", "3.28"]),
    "15": (Decimal("15"), Method.DB_150,
           ["5.00", "9.50", "8.55", "7.70", "6.93", "6.23", "5.90", "5.90",
            "5.91", "5.90", "5.91", "5.90", "5.91", "5.90", "5.91", "2.95"]),
}


@pytest.mark.parametrize("klass", sorted(TABLE_A1))
def test_table_a1_half_year(klass: str) -> None:
    """Pub 946 Appendix A Table A-1 (GDS, half-year), full recovery period."""
    period, method, table = TABLE_A1[klass]
    got = macrs_schedule(BASIS, period, method, Convention.HALF_YEAR, date(2024, 6, 1))
    assert_matches_table(got, table, f"Table A-1 {klass}-year")


def test_five_year_half_year_full_life_hand_check() -> None:
    """A 5-year asset, half-year, across its FULL life — the canonical case.

    $10,000 of 5-year property, no §179, no bonus. Pub 946 Table A-1 gives
    20/32/19.20/11.52/11.52/5.76 %, i.e. $2,000 / $3,200 / $1,920 / $1,152 /
    $1,152 / $576, totalling exactly $10,000 over six tax years.
    """
    got = macrs_schedule(
        1_000_000, Decimal("5"), Method.DB_200, Convention.HALF_YEAR, date(2024, 3, 1)
    )
    assert got == [200_000, 320_000, 192_000, 115_200, 115_200, 57_600]
    assert sum(got) == 1_000_000


# ---------------------------------------------------------------------------
# Pub 946 Appendix A, Tables A-2 … A-5 — GDS, mid-quarter convention
# ---------------------------------------------------------------------------

MID_QUARTER_5YR = {
    1: (date(2024, 2, 1), ["35.00", "26.00", "15.60", "11.01", "11.01", "1.38"]),
    2: (date(2024, 5, 1), ["25.00", "30.00", "18.00", "11.37", "11.37", "4.26"]),
    3: (date(2024, 8, 1), ["15.00", "34.00", "20.40", "12.24", "11.30", "7.06"]),
    4: (date(2024, 11, 1), ["5.00", "38.00", "22.80", "13.68", "10.94", "9.58"]),
}


@pytest.mark.parametrize("quarter", sorted(MID_QUARTER_5YR))
def test_tables_a2_a5_mid_quarter_five_year(quarter: int) -> None:
    """Pub 946 Tables A-2..A-5 (GDS, mid-quarter), 5-year property, each quarter."""
    placed, table = MID_QUARTER_5YR[quarter]
    got = macrs_schedule(BASIS, Decimal("5"), Method.DB_200, Convention.MID_QUARTER, placed)
    assert_matches_table(got, table, f"mid-quarter Q{quarter} 5-year")


# ---------------------------------------------------------------------------
# Pub 946 Appendix A, Table A-6 — residential rental, mid-month
# ---------------------------------------------------------------------------

TABLE_A6_YEAR1 = {
    1: "3.485", 2: "3.182", 3: "2.879", 4: "2.576", 5: "2.273", 6: "1.970",
    7: "1.667", 8: "1.364", 9: "1.061", 10: "0.758", 11: "0.455", 12: "0.152",
}


@pytest.mark.parametrize("month", sorted(TABLE_A6_YEAR1))
def test_table_a6_residential_rental_mid_month_year_one(month: int) -> None:
    """Pub 946 Table A-6: 27.5-year residential rental, year-1 % by month placed."""
    got = macrs_schedule(
        BASIS, Decimal("27.5"), Method.SL, Convention.MID_MONTH, date(2024, month, 15)
    )
    year_one = Decimal(got[0]) * 100 / Decimal(BASIS)
    want = Decimal(TABLE_A6_YEAR1[month])
    assert abs(year_one - want) <= Decimal("0.001"), (
        f"27.5-yr month {month}: computed {year_one:.4f}% vs table {want}%"
    )
    assert sum(got) == BASIS


def test_nonresidential_real_39_year_mid_month_mechanics() -> None:
    """39-year nonresidential real property, mid-month (§168(b)(3), §168(d)(2)).

    Year 1 for a January placement is 11.5 months of service out of 12 at the
    straight-line rate 1/39 = 2.5641%/yr -> 2.4573%. (See the report note: the
    published Table A-7a year-1 January figure should be confirmed against
    Pub 946 before this is relied on for a filed return; the MECHANICS asserted
    here — mid-month straight line over 39 years — are what §168 requires.)
    """
    got = macrs_schedule(
        BASIS, Decimal("39"), Method.SL, Convention.MID_MONTH, date(2024, 1, 10)
    )
    year_one = Decimal(got[0]) * 100 / Decimal(BASIS)
    expected = Decimal(1) / Decimal(39) * (Decimal("11.5") / Decimal(12)) * 100
    assert abs(year_one - expected) <= Decimal("0.001")
    # A full middle year is the plain straight-line rate.
    assert abs(Decimal(got[1]) * 100 / Decimal(BASIS) - Decimal(100) / Decimal(39)) <= Decimal("0.001")
    assert sum(got) == BASIS


# ---------------------------------------------------------------------------
# §168(d)(3) mid-quarter TRIGGER test
# ---------------------------------------------------------------------------


def _asset(aid: str, month: int, cost: int, period: str = "5", **kw) -> AssetInput:
    return AssetInput(
        asset_id=aid,
        description=aid,
        placed_in_service=date(2024, month, 15),
        cost_cents=cost,
        recovery_period_years=Decimal(period),
        **kw,
    )


def test_mid_quarter_trigger_fires_above_40_percent() -> None:
    """§168(d)(3): >40% of aggregate basis in the last 3 months triggers MQ."""
    assets = [_asset("early", 3, 50_000_00), _asset("q4", 11, 40_000_01)]
    assert mid_quarter_applies(assets, 2024) is True


def test_mid_quarter_trigger_does_not_fire_at_exactly_40_percent() -> None:
    """The statute says EXCEEDS 40% — exactly 40% does NOT trigger."""
    assets = [_asset("early", 3, 60_000_00), _asset("q4", 11, 40_000_00)]
    assert mid_quarter_applies(assets, 2024) is False


def test_mid_quarter_trigger_excludes_real_property() -> None:
    """§168(d)(3)(B)(i): mid-month real property is excluded from BOTH sides.

    A $1,000,000 December building alongside $100,000 of March equipment must
    NOT drag the equipment into mid-quarter.
    """
    assets = [
        _asset("equip", 3, 100_000_00, "5"),
        _asset("building", 12, 1_000_000_00, "39"),
    ]
    assert mid_quarter_applies(assets, 2024) is False


def test_mid_quarter_trigger_ignores_prior_year_assets() -> None:
    """Only property placed in service DURING the tax year enters the test."""
    prior = AssetInput(
        asset_id="prior", placed_in_service=date(2023, 11, 1),
        cost_cents=900_000_00, recovery_period_years=Decimal("5"),
    )
    assets = [prior, _asset("this_year", 3, 100_000_00)]
    assert mid_quarter_applies(assets, 2024) is False


def test_mid_quarter_case_end_to_end_uses_mq_convention() -> None:
    """A triggered year puts EVERY current-year non-real asset on mid-quarter.

    $30,000 in March + $70,000 in November = 70% in Q4, so both assets use MQ.
    The March asset is Q1 (35.00% year 1), the November asset Q4 (5.00%).
    """
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[_asset("march", 3, 30_000_00), _asset("nov", 11, 70_000_00)],
    )
    result = compute_depreciation(inputs, cfg(2024))
    assert result.mid_quarter_applies is True
    by_id = {a.asset_id: a for a in result.assets}
    assert by_id["march"].convention is Convention.MID_QUARTER
    assert by_id["nov"].convention is Convention.MID_QUARTER
    assert by_id["march"].current_year_macrs_cents == 30_000_00 * 35 // 100
    assert by_id["nov"].current_year_macrs_cents == 70_000_00 * 5 // 100
    assert any("mid_quarter" in a for a in result.assumptions)


# ---------------------------------------------------------------------------
# §179 — dollar limit, investment phase-out, income limit, carryforward
# ---------------------------------------------------------------------------


def test_section_179_investment_phase_out() -> None:
    """§179(b)(2): the limit drops dollar-for-dollar above the threshold.

    Limit $1,000,000, threshold $2,500,000, $2,800,000 of property placed in
    service. Reduction = $300,000, so the dollar limit becomes $700,000.
    """
    config = cfg(2024, limit=1_000_000_00, threshold=2_500_000_00)
    inputs = DepreciationInputs(
        tax_year=2024,
        business_taxable_income_cents=10_000_000_00,
        assets=[
            _asset("big", 3, 2_800_000_00, "7", section_179_elected_cents=800_000_00)
        ],
    )
    form = compute_depreciation(inputs, config).form_4562
    assert form.line_2_total_179_property == 2_800_000_00
    assert form.line_3_threshold == 2_500_000_00
    assert form.line_4_reduction == 300_000_00
    assert form.line_5_dollar_limit == 700_000_00
    # Elected $800k but the limit is $700k -> tentative deduction is $700k.
    assert form.line_9_tentative_deduction == 700_000_00
    assert form.line_12_section_179_deduction == 700_000_00


def test_section_179_phase_out_to_zero() -> None:
    """Property above threshold + limit fully eliminates the §179 deduction."""
    config = cfg(2024, limit=1_000_000_00, threshold=2_500_000_00)
    inputs = DepreciationInputs(
        tax_year=2024,
        business_taxable_income_cents=10_000_000_00,
        assets=[
            _asset("huge", 3, 3_600_000_00, "7", section_179_elected_cents=100_000_00)
        ],
    )
    form = compute_depreciation(inputs, config).form_4562
    assert form.line_4_reduction == 1_100_000_00
    assert form.line_5_dollar_limit == 0
    assert form.line_12_section_179_deduction == 0


def test_section_179_taxable_income_limitation_and_carryforward() -> None:
    """§179(b)(3)(A)/(B): the deduction is capped at business taxable income,
    and the disallowed amount carries forward.

    $200,000 elected, but only $50,000 of business taxable income:
      line 9  tentative        = 200,000
      line 11 income limit     =  50,000
      line 12 deduction        =  50,000
      line 13 carryforward     = 150,000
    """
    config = cfg(2024, limit=1_000_000_00, threshold=2_500_000_00)
    inputs = DepreciationInputs(
        tax_year=2024,
        business_taxable_income_cents=50_000_00,
        assets=[
            _asset("m", 3, 200_000_00, "7", section_179_elected_cents=200_000_00)
        ],
    )
    result = compute_depreciation(inputs, config)
    form = result.form_4562
    assert form.line_9_tentative_deduction == 200_000_00
    assert form.line_11_business_income_limit == 50_000_00
    assert form.line_12_section_179_deduction == 50_000_00
    assert form.line_13_carryover_to_next == 150_000_00
    assert result.section_179_carryforward_out_cents == 150_000_00
    assert any("carryforward" in a for a in result.assumptions)


def test_section_179_carryforward_in_is_deductible_next_year() -> None:
    """The prior year's line 13 becomes this year's line 10 and is deductible
    up to the income limit (Form 4562 line 12 = min(line 9 + line 10, line 11))."""
    config = cfg(2024, limit=1_000_000_00, threshold=2_500_000_00)
    inputs = DepreciationInputs(
        tax_year=2024,
        business_taxable_income_cents=200_000_00,
        section_179_carryforward_in_cents=150_000_00,
        assets=[],
    )
    form = compute_depreciation(inputs, config).form_4562
    assert form.line_10_carryover_from_prior == 150_000_00
    assert form.line_12_section_179_deduction == 150_000_00
    assert form.line_13_carryover_to_next == 0


def test_section_179_requires_business_income_rather_than_guessing() -> None:
    """§179(b)(3)(A) needs taxable income; the engine REFUSES to assume it."""
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[_asset("m", 3, 50_000_00, "7", section_179_elected_cents=10_000_00)],
    )
    with pytest.raises(ValueError, match="business_taxable_income_cents is required"):
        compute_depreciation(inputs, cfg(2024))


def test_section_179_cannot_be_elected_in_a_later_year() -> None:
    """§179(a): the election is only available in the placed-in-service year."""
    prior = AssetInput(
        asset_id="old", placed_in_service=date(2023, 5, 1), cost_cents=50_000_00,
        recovery_period_years=Decimal("7"), section_179_elected_cents=10_000_00,
        prior_bonus_cents=0,
    )
    inputs = DepreciationInputs(
        tax_year=2024, business_taxable_income_cents=100_000_00, assets=[prior]
    )
    with pytest.raises(ValueError, match="may only be elected in the year"):
        compute_depreciation(inputs, cfg(2024))


# ---------------------------------------------------------------------------
# §168(k) bonus depreciation
# ---------------------------------------------------------------------------


def test_bonus_applies_after_179_and_before_macrs() -> None:
    """§168(k)(1)(A): bonus is taken on basis remaining AFTER §179; MACRS then
    runs on what is left.

    $100,000 of 5-year property, $20,000 §179 elected, 60% bonus:
      §179   = 20,000
      bonus  = (100,000 - 20,000) x 60% = 48,000
      MACRS  = (100,000 - 20,000 - 48,000) x 20% (yr 1 HY) = 6,400
      total  = 74,400
    """
    config = cfg(2024, limit=1_000_000_00, threshold=2_500_000_00, bonus="0.60")
    inputs = DepreciationInputs(
        tax_year=2024,
        business_taxable_income_cents=1_000_000_00,
        assets=[_asset("m", 3, 100_000_00, "5", section_179_elected_cents=20_000_00)],
    )
    result = compute_depreciation(inputs, config)
    asset = result.assets[0]
    assert asset.section_179_cents == 20_000_00
    assert asset.bonus_cents == 48_000_00
    assert asset.macrs_basis_cents == 32_000_00
    assert asset.current_year_macrs_cents == 6_400_00
    assert asset.current_year_total_cents == 74_400_00
    assert result.form_4562.line_14_special_allowance == 48_000_00


def test_bonus_100_percent_fully_expenses_and_leaves_no_macrs() -> None:
    config = cfg(2024, bonus="1.00")
    inputs = DepreciationInputs(
        tax_year=2024, assets=[_asset("m", 3, 100_000_00, "5")]
    )
    asset = compute_depreciation(inputs, config).assets[0]
    assert asset.bonus_cents == 100_000_00
    assert asset.macrs_basis_cents == 0
    assert asset.current_year_macrs_cents == 0


def test_bonus_election_out_under_168k7() -> None:
    """§168(k)(7): a taxpayer may irrevocably elect out by class."""
    config = cfg(2024, bonus="0.60")
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[_asset("m", 3, 100_000_00, "5", bonus_elected_out=True)],
    )
    asset = compute_depreciation(inputs, config).assets[0]
    assert asset.bonus_cents == 0
    assert asset.current_year_macrs_cents == 20_000_00   # full 20% year-1 MACRS


def test_prior_year_asset_keeps_its_placement_year_bonus_not_this_years() -> None:
    """A 2023 asset must produce the RIGHT 2026 deduction.

    $100,000 of 5-year property placed in service in 2023 with 80% bonus:
    MACRS basis is $20,000, and the 2026 deduction is year 4 of the half-year
    schedule (11.52%) = $2,304. The current year's bonus percentage is
    irrelevant to it.
    """
    asset = AssetInput(
        asset_id="a2023", description="2023 machine",
        placed_in_service=date(2023, 6, 1), cost_cents=100_000_00,
        recovery_period_years=Decimal("5"), prior_bonus_cents=80_000_00,
    )
    inputs = DepreciationInputs(tax_year=2026, assets=[asset])
    result = compute_depreciation(inputs, cfg(2026, bonus="1.00"))
    got = result.assets[0]
    assert got.macrs_basis_cents == 20_000_00
    assert got.bonus_cents == 0            # not a first year — no new bonus
    assert got.current_year_macrs_cents == 2_304_00   # 20,000 x 11.52%
    # Reported on line 17 (prior-year MACRS), not in Section B.
    assert result.form_4562.line_17_prior_year_macrs == 2_304_00
    assert result.form_4562.part_iii_section_b == {}


def test_prior_year_asset_without_bonus_figure_raises() -> None:
    """The engine refuses to GUESS a prior year's §168(k) allowance."""
    asset = AssetInput(
        asset_id="mystery", placed_in_service=date(2019, 6, 1),
        cost_cents=100_000_00, recovery_period_years=Decimal("5"),
    )
    inputs = DepreciationInputs(tax_year=2024, assets=[asset])
    with pytest.raises(ValueError, match="will not|guess"):
        compute_depreciation(inputs, cfg(2024))


# ---------------------------------------------------------------------------
# §280F listed property
# ---------------------------------------------------------------------------


def test_listed_property_business_use_reduces_basis() -> None:
    """§280F: only the business/investment portion of cost is depreciable.

    A $50,000 vehicle used 60% for business: depreciable basis $30,000, and with
    no bonus the year-1 5-year half-year MACRS is $6,000. It reports on Part V
    line 26 (used MORE than 50% in a qualified business use).
    """
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[
            _asset("van", 3, 50_000_00, "5",
                   is_listed_property=True, business_use_pct=Decimal("60"))
        ],
    )
    result = compute_depreciation(inputs, cfg(2024))
    asset = result.assets[0]
    assert asset.business_basis_cents == 30_000_00
    assert asset.current_year_macrs_cents == 6_000_00
    assert asset.form_line == "26"
    rows = result.form_4562.line_26_over_50_pct
    assert len(rows) == 1 and rows[0].business_use_pct == Decimal("60")
    assert result.form_4562.line_27_50_pct_or_less == []


def test_listed_property_at_or_below_50_percent_forced_to_ads_straight_line() -> None:
    """§280F(b)(1): <=50% qualified business use requires ADS straight line.

    A $50,000 vehicle at 40% business use: basis $20,000, ADS 5-year straight
    line half-year => year 1 = 20,000 / 5 * 0.5 = $2,000. It reports on line 27.
    """
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[
            _asset("van", 3, 50_000_00, "5",
                   is_listed_property=True, business_use_pct=Decimal("40"))
        ],
    )
    result = compute_depreciation(inputs, cfg(2024, bonus="0.60"))
    asset = result.assets[0]
    assert asset.system is DepreciationSystem.ADS
    assert asset.method is Method.SL
    assert asset.business_basis_cents == 20_000_00
    assert asset.bonus_cents == 0            # §168(k)(2)(D)(i) denies bonus
    assert asset.current_year_macrs_cents == 2_000_00
    assert asset.form_line == "27"
    assert len(result.form_4562.line_27_50_pct_or_less) == 1
    assert any("280F" in a or "ADS" in a for a in result.assumptions)


def test_listed_property_at_or_below_50_percent_cannot_elect_179() -> None:
    """§280F(b)(3) denies §179 for listed property not used predominantly."""
    inputs = DepreciationInputs(
        tax_year=2024,
        business_taxable_income_cents=1_000_000_00,
        assets=[
            _asset("van", 3, 50_000_00, "5", is_listed_property=True,
                   business_use_pct=Decimal("40"), section_179_elected_cents=10_000_00)
        ],
    )
    with pytest.raises(ValueError, match="280F"):
        compute_depreciation(inputs, cfg(2024))


def test_listed_property_bonus_lands_on_line_25_not_line_14() -> None:
    """Listed-property bonus belongs in Part V line 25, not Part II line 14."""
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[
            _asset("car", 3, 40_000_00, "5",
                   is_listed_property=True, business_use_pct=Decimal("100")),
            _asset("lathe", 3, 40_000_00, "7"),
        ],
    )
    form = compute_depreciation(inputs, cfg(2024, bonus="0.60")).form_4562
    assert form.line_25_listed_special_allowance == 24_000_00
    assert form.line_14_special_allowance == 24_000_00
    assert form.line_28_total_listed == form.line_21_listed_property


# ---------------------------------------------------------------------------
# Form 4562 assembly + guards
# ---------------------------------------------------------------------------


def test_section_b_is_keyed_by_recovery_period_not_form_row() -> None:
    """The engine must NOT bake in a printed row letter — those moved in 2025."""
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[
            _asset("a", 3, 10_000_00, "5"),
            _asset("b", 3, 10_000_00, "7"),
            _asset("c", 3, 300_000_00, "27.5"),
        ],
    )
    form = compute_depreciation(inputs, cfg(2024)).form_4562
    assert set(form.part_iii_section_b) == {"5", "7", "27.5"}


def test_same_class_assets_aggregate_onto_one_row() -> None:
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[_asset("a", 3, 10_000_00, "5"), _asset("b", 4, 30_000_00, "5")],
    )
    form = compute_depreciation(inputs, cfg(2024)).form_4562
    row = form.part_iii_section_b["5"]
    assert row.basis_cents == 40_000_00
    assert row.deduction_cents == 8_000_00       # 20% of 40,000


def test_line_22_total_is_the_sum_of_its_components() -> None:
    """Form 4562 line 22 = line 12 + lines 14-17 + Sections B/C col (g) + line 21."""
    inputs = DepreciationInputs(
        tax_year=2024,
        business_taxable_income_cents=1_000_000_00,
        other_depreciation_cents=1_234_00,
        assets=[
            _asset("m", 3, 100_000_00, "7", section_179_elected_cents=10_000_00),
            _asset("car", 3, 40_000_00, "5",
                   is_listed_property=True, business_use_pct=Decimal("80")),
        ],
    )
    result = compute_depreciation(inputs, cfg(2024, bonus="0.60"))
    f = result.form_4562
    section_bc = sum(r.deduction_cents for r in f.part_iii_section_b.values()) + sum(
        r.deduction_cents for r in f.part_iii_section_c.values()
    )
    expected = (
        f.line_12_section_179_deduction
        + f.line_14_special_allowance
        + f.line_16_other_depreciation
        + f.line_17_prior_year_macrs
        + section_bc
        + f.line_21_listed_property
    )
    assert f.line_22_total == expected
    assert result.total_depreciation_cents == f.line_22_total


def test_ads_reports_in_section_c() -> None:
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[
            _asset("srv", 3, 60_000_00, "12", system=DepreciationSystem.ADS)
        ],
    )
    form = compute_depreciation(inputs, cfg(2024)).form_4562
    assert set(form.part_iii_section_c) == {"12"}
    assert form.part_iii_section_b == {}
    # ADS is straight line: 60,000 / 12 * 0.5 (half-year) = 2,500.
    assert form.part_iii_section_c["12"].deduction_cents == 2_500_00


def test_config_year_mismatch_raises() -> None:
    inputs = DepreciationInputs(tax_year=2024, assets=[])
    with pytest.raises(ValueError, match="refusing to apply the wrong year"):
        compute_depreciation(inputs, cfg(2025))


def test_short_tax_year_raises_rather_than_prorating_wrongly() -> None:
    inputs = DepreciationInputs(tax_year=2024, assets=[], is_short_tax_year=True)
    with pytest.raises(ValueError, match="short tax years are not supported"):
        compute_depreciation(inputs, cfg(2024))


def test_amortization_part_vi() -> None:
    """Part VI: straight-line over the stated period from the month it begins."""
    from app.tax.engines.depreciation import AmortizationItem

    inputs = DepreciationInputs(
        tax_year=2024,
        amortization_prior_years_cents=5_000_00,
        amortization_current_year=[
            AmortizationItem(
                description="Startup costs", begins=date(2024, 7, 1),
                amortizable_amount_cents=18_000_00, code_section="195",
                period_months=180,
            )
        ],
    )
    form = compute_depreciation(inputs, cfg(2024)).form_4562
    # 18,000 / 180 = 100/month; July-December = 6 months = 600.
    assert form.line_42_amortization[0].amortization_cents == 600_00
    assert form.line_43_amortization_prior == 5_000_00
    assert form.line_44_amortization_total == 5_600_00
    # Amortization is NOT part of the depreciation total (line 22).
    assert form.line_22_total == 0
