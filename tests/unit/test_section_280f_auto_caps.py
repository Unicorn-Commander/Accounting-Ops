"""§280F(a) passenger-automobile depreciation caps.

The failure mode being guarded here is OVER-DEDUCTION: without these caps a
$90,000 car placed in service in 2024 with 100% bonus would produce a $90,000
first-year deduction instead of the statutory $20,400. Every figure below comes
from IRS Pub 946 (2025) ch. 5, "Maximum Depreciation Deduction for Passenger
Automobiles (Including Trucks and Vans) Acquired After September 27, 2017".
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.tax.engines.configs_depreciation import (
    SUPPORTED_AUTO_CAP_YEARS,
    config_for_year,
    passenger_auto_cap,
)
from app.tax.engines.depreciation import (
    AssetInput,
    DepreciationInputs,
    ListedPropertyType,
    compute_depreciation,
)

# (placed-in-service year, 1st yr WITH bonus, 1st yr NO bonus, 2nd, 3rd, 4th+)
PUBLISHED_CAPS = [
    (2022, 19_200, 11_200, 18_000, 10_800, 6_460),
    (2023, 20_200, 12_200, 19_500, 11_700, 6_960),
    (2024, 20_400, 12_400, 19_800, 11_900, 7_160),
    (2025, 20_200, 12_200, 19_600, 11_800, 7_060),
]


@pytest.mark.parametrize(
    ("year", "y1_bonus", "y1_nobonus", "y2", "y3", "y4"), PUBLISHED_CAPS
)
def test_caps_match_pub_946(year, y1_bonus, y1_nobonus, y2, y3, y4) -> None:
    assert passenger_auto_cap(year, 0, bonus_claimed=True) == y1_bonus * 100
    assert passenger_auto_cap(year, 0, bonus_claimed=False) == y1_nobonus * 100
    assert passenger_auto_cap(year, 1, bonus_claimed=True) == y2 * 100
    assert passenger_auto_cap(year, 2, bonus_claimed=True) == y3 * 100
    assert passenger_auto_cap(year, 3, bonus_claimed=True) == y4 * 100
    assert passenger_auto_cap(year, 9, bonus_claimed=True) == y4 * 100


def test_caps_are_not_monotonic_across_years() -> None:
    """REGRESSION: 2025's first-year cap is LOWER than 2024's. Anyone tempted to
    'fix' the table by making it increase would be introducing a wrong number."""
    assert passenger_auto_cap(2025, 0, bonus_claimed=True) < passenger_auto_cap(
        2024, 0, bonus_claimed=True
    )


def test_unpublished_placement_year_raises_rather_than_over_deducting() -> None:
    with pytest.raises(ValueError, match="No §280F\\(a\\) passenger-automobile cap"):
        passenger_auto_cap(2026, 0, bonus_claimed=True)
    assert 2026 not in SUPPORTED_AUTO_CAP_YEARS


def _car(cost_cents: int, *, year: int = 2024, business_use: str = "100",
         placed_month: int = 6, elected_out: bool = False) -> AssetInput:
    return AssetInput(
        asset_id="car", description="Company car",
        placed_in_service=date(year, placed_month, 1),
        acquired_on=date(year, placed_month, 1),
        cost_cents=cost_cents, recovery_period_years=Decimal("5"),
        is_listed_property=True,
        listed_property_type=ListedPropertyType.PASSENGER_AUTO,
        business_use_pct=Decimal(business_use),
        bonus_elected_out=elected_out,
    )


def _run(asset: AssetInput, tax_year: int = 2024):
    from app.tax.engines.configs_depreciation import passenger_auto_cap as cap

    return compute_depreciation(
        DepreciationInputs(tax_year=tax_year, assets=[asset]),
        config_for_year(tax_year),
        config_for_year=config_for_year,
        auto_cap_for=cap,
    )


def test_expensive_car_is_capped_at_the_first_year_limit() -> None:
    """THE over-deduction guard.

    A $90,000 car placed in service in 2024, 100% business use, 60% bonus.
    Uncapped that is $54,000 bonus + $7,200 MACRS = $61,200. §280F(a) caps the
    2024 first year at $20,400.
    """
    result = _run(_car(90_000_00))
    asset = result.assets[0]
    assert asset.current_year_total_cents == 20_400_00
    assert asset.section_280f_disallowed_cents == 61_200_00 - 20_400_00
    assert any("280F" in a for a in result.assumptions)


def test_cap_is_prorated_by_business_use_percentage() -> None:
    """§280F(a)(2): the cap itself is reduced to the business-use percentage.

    Same $90,000 car at 60% business use -> cap = $20,400 x 60% = $12,240.
    """
    asset = _run(_car(90_000_00, business_use="60")).assets[0]
    assert asset.current_year_total_cents == 12_240_00


def test_electing_out_of_bonus_uses_the_lower_first_year_cap() -> None:
    """Pub 946 footnote: no special allowance -> the 2024 first-year cap is
    $12,400, not $20,400."""
    asset = _run(_car(90_000_00, elected_out=True)).assets[0]
    assert asset.bonus_cents == 0
    assert asset.current_year_total_cents == 12_400_00


def test_cheap_car_is_not_capped() -> None:
    """A car whose computed deduction is under the cap is untouched."""
    result = _run(_car(15_000_00, elected_out=True))
    asset = result.assets[0]
    # $15,000 of 5-year property, half-year, no bonus -> 20% = $3,000.
    assert asset.current_year_total_cents == 3_000_00
    assert asset.section_280f_disallowed_cents == 0
    assert not any("280F" in a for a in result.assumptions)


def test_later_year_uses_that_year_s_cap_row_from_the_placement_year() -> None:
    """The cap locks to the PLACEMENT year: a 2022 car on a 2024 return uses the
    2022 row's 3rd-year figure ($10,800), not any 2024 number."""
    asset = _car(90_000_00, year=2022)
    asset = asset.model_copy(update={"prior_bonus_cents": 90_000_00 * 0})
    result = _run(asset, tax_year=2024)
    got = result.assets[0]
    assert got.is_first_year is False
    assert got.current_year_total_cents <= 10_800_00


def test_heavy_vehicle_is_listed_but_not_capped() -> None:
    """A >6,000 lb vehicle is outside §280F(d)(5), so no dollar cap applies."""
    asset = AssetInput(
        asset_id="truck", description="Heavy truck",
        placed_in_service=date(2024, 6, 1), acquired_on=date(2024, 6, 1),
        cost_cents=90_000_00, recovery_period_years=Decimal("5"),
        is_listed_property=True,
        listed_property_type=ListedPropertyType.HEAVY_VEHICLE,
    )
    got = _run(asset).assets[0]
    # 60% bonus on 90,000 = 54,000, plus 20% of the remaining 36,000 = 7,200.
    assert got.current_year_total_cents == 61_200_00
    assert got.section_280f_disallowed_cents == 0


def test_listed_property_without_a_type_is_refused() -> None:
    """The engine must not INFER whether a listed asset is a capped auto."""
    with pytest.raises(ValueError, match="listed_property_type"):
        AssetInput(
            asset_id="mystery", placed_in_service=date(2024, 6, 1),
            cost_cents=90_000_00, recovery_period_years=Decimal("5"),
            is_listed_property=True,
        )


def test_type_without_the_listed_flag_is_refused() -> None:
    with pytest.raises(ValueError, match="contradictory"):
        AssetInput(
            asset_id="x", placed_in_service=date(2024, 6, 1),
            cost_cents=1000, recovery_period_years=Decimal("5"),
            listed_property_type=ListedPropertyType.PASSENGER_AUTO,
        )


def test_passenger_auto_without_a_cap_resolver_raises() -> None:
    """Belt-and-braces: computing a car with no cap resolver must refuse rather
    than silently produce an uncapped figure."""
    with pytest.raises(ValueError, match="no §280F\\(a\\) cap resolver"):
        compute_depreciation(
            DepreciationInputs(tax_year=2024, assets=[_car(90_000_00)]),
            config_for_year(2024),
        )
