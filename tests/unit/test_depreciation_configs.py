"""Tests pinning the year-specific depreciation CONSTANTS to their sources.

These assert the actual statutory/published figures, so a careless edit to
``configs_depreciation`` fails loudly. Every figure here was read from IRS
Publication 946's "What's New" section for the year in question (see the
verification log in configs_depreciation.py).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.tax.engines.configs_depreciation import (
    OBBBA_ACQUISITION_CUTOFF,
    SUPPORTED_TAX_YEARS_DEPRECIATION,
    config_for_year,
    section_179_limits,
)
from app.tax.engines.depreciation import (
    AssetInput,
    DepreciationInputs,
    compute_depreciation,
)

# (year, limit_dollars, threshold_dollars) — IRS Pub 946 "What's New".
SECTION_179_BY_YEAR = [
    (2022, 1_080_000, 2_700_000),
    (2023, 1_160_000, 2_890_000),
    (2024, 1_220_000, 3_050_000),
    (2025, 2_500_000, 4_000_000),   # OBBBA, P.L. 119-21 — NOT Rev. Proc. 2024-40
    (2026, 2_560_000, 4_090_000),
]


@pytest.mark.parametrize(("year", "limit", "threshold"), SECTION_179_BY_YEAR)
def test_section_179_limits_match_pub_946(year: int, limit: int, threshold: int) -> None:
    got_limit, got_threshold = section_179_limits(year)
    assert got_limit == limit * 100, f"TY{year} §179 dollar limit"
    assert got_threshold == threshold * 100, f"TY{year} §179 investment threshold"


def test_2025_uses_obbba_limits_not_the_superseded_rev_proc_figures() -> None:
    """REGRESSION: Rev. Proc. 2024-40 set TY2025 at $1,250,000 / $3,130,000 and
    Pub 946 (2024) printed those. OBBBA superseded them. Using the old figures
    would understate a TY2025 §179 deduction by up to $1.25M."""
    limit, threshold = section_179_limits(2025)
    assert limit == 2_500_000_00
    assert threshold == 4_000_000_00
    assert limit != 1_250_000_00
    assert threshold != 3_130_000_00


@pytest.mark.parametrize(("year", "rate"), [(2022, "1.00"), (2023, "0.80"), (2024, "0.60")])
def test_bonus_percentage_pre_obbba_years(year: int, rate: str) -> None:
    """TCJA phase-down: 100% / 80% / 60% for 2022 / 2023 / 2024.

    These years have NO acquisition-date split, so the rate resolves without an
    acquired_on date.
    """
    rule = config_for_year(year).bonus
    assert rule.acquisition_cutoff is None
    assert rule.rate_for(None, "any") == Decimal(rate)


def test_obbba_cutoff_is_january_19_2025() -> None:
    assert OBBBA_ACQUISITION_CUTOFF == date(2025, 1, 19)


def test_2025_bonus_splits_on_the_obbba_acquisition_date() -> None:
    """Pub 946 (2025): 100% for property ACQUIRED and placed in service after
    2025-01-19; 40% for other property placed in service in 2025."""
    rule = config_for_year(2025).bonus
    assert rule.rate_for(date(2025, 1, 20), "a") == Decimal("1.00")
    assert rule.rate_for(date(2025, 3, 1), "a") == Decimal("1.00")
    # On the cutoff date itself is NOT "after" it.
    assert rule.rate_for(date(2025, 1, 19), "a") == Decimal("0.40")
    assert rule.rate_for(date(2024, 12, 1), "a") == Decimal("0.40")


def test_2025_bonus_without_an_acquisition_date_raises() -> None:
    """The engine must not silently pick 40% or 100%."""
    rule = config_for_year(2025).bonus
    with pytest.raises(ValueError, match="no acquired_on date|acquired_on"):
        rule.rate_for(None, "mystery")


def test_2026_post_cutoff_is_100_percent_and_pre_cutoff_is_unsupported() -> None:
    """OBBBA made 100% permanent for post-cutoff acquisitions. The residual
    TCJA rate for a pre-cutoff acquisition placed in service in 2026 was NOT
    verified, so it must RAISE rather than guess."""
    rule = config_for_year(2026).bonus
    assert rule.rate_for(date(2025, 6, 1), "a") == Decimal("1.00")
    with pytest.raises(ValueError, match="no verified residual TCJA"):
        rule.rate_for(date(2024, 12, 1), "a")


def test_unsupported_year_raises_and_lists_supported_years() -> None:
    with pytest.raises(ValueError, match="No depreciation constants on file"):
        config_for_year(2019)
    with pytest.raises(ValueError, match="No depreciation constants on file"):
        config_for_year(2030)
    with pytest.raises(ValueError):
        section_179_limits(2019)


def test_supported_years_are_exactly_the_ones_with_verified_constants() -> None:
    assert SUPPORTED_TAX_YEARS_DEPRECIATION == (2022, 2023, 2024, 2025, 2026)


def test_every_year_carries_a_citation() -> None:
    """No constant may ship without its source — that is the whole discipline."""
    for year in SUPPORTED_TAX_YEARS_DEPRECIATION:
        config = config_for_year(year)
        assert config.section_179_source.strip()
        assert config.bonus_source.strip()
        assert "Pub 946" in config.section_179_source or "§" in config.section_179_source


# ---------------------------------------------------------------------------
# End-to-end with the REAL constants
# ---------------------------------------------------------------------------


def test_real_2024_return_end_to_end() -> None:
    """A TY2024 return using the real published constants.

    $100,000 of 7-year equipment placed in service 2024-03-01, no §179 elected.
    2024 bonus is 60%, so: bonus $60,000; MACRS on $40,000 at the 7-year
    half-year year-1 rate 14.29% = $5,714.29 (the engine's unrounded 14.2857%
    gives $5,714.29). Total $65,714.29.
    """
    inputs = DepreciationInputs(
        tax_year=2024,
        assets=[
            AssetInput(
                asset_id="lathe", description="CNC lathe",
                placed_in_service=date(2024, 3, 1), cost_cents=100_000_00,
                recovery_period_years=Decimal("7"),
            )
        ],
    )
    result = compute_depreciation(inputs, config_for_year(2024))
    asset = result.assets[0]
    assert asset.bonus_cents == 60_000_00
    assert asset.macrs_basis_cents == 40_000_00
    assert asset.current_year_macrs_cents == 571_429      # $5,714.29
    assert result.form_4562.line_22_total == 60_000_00 + 571_429


def test_real_2025_return_100_percent_bonus_after_obbba() -> None:
    """A TY2025 asset acquired after the OBBBA cutoff is fully expensed."""
    inputs = DepreciationInputs(
        tax_year=2025,
        assets=[
            AssetInput(
                asset_id="press", description="Press",
                placed_in_service=date(2025, 6, 1), acquired_on=date(2025, 5, 1),
                cost_cents=250_000_00, recovery_period_years=Decimal("7"),
            )
        ],
    )
    result = compute_depreciation(inputs, config_for_year(2025))
    assert result.assets[0].bonus_cents == 250_000_00
    assert result.assets[0].macrs_basis_cents == 0
    assert result.form_4562.line_14_special_allowance == 250_000_00


def test_real_2025_return_40_percent_for_pre_cutoff_acquisition() -> None:
    """Same year, same cost — but acquired before the cutoff, so 40%."""
    inputs = DepreciationInputs(
        tax_year=2025,
        assets=[
            AssetInput(
                asset_id="press", description="Press",
                placed_in_service=date(2025, 6, 1), acquired_on=date(2024, 11, 1),
                cost_cents=250_000_00, recovery_period_years=Decimal("7"),
            )
        ],
    )
    result = compute_depreciation(inputs, config_for_year(2025))
    assert result.assets[0].bonus_cents == 100_000_00      # 40%
    assert result.assets[0].macrs_basis_cents == 150_000_00


def test_real_2025_section_179_uses_obbba_ceiling() -> None:
    """$3,000,000 of equipment: under the OBBBA $2.5M limit / $4M threshold there
    is NO phase-out (3.0M < 4.0M), so the full $2,500,000 limit is available."""
    inputs = DepreciationInputs(
        tax_year=2025,
        business_taxable_income_cents=10_000_000_00,
        assets=[
            AssetInput(
                asset_id="line", description="Production line",
                placed_in_service=date(2025, 6, 1), acquired_on=date(2025, 5, 1),
                cost_cents=3_000_000_00, recovery_period_years=Decimal("7"),
                section_179_elected_cents=2_600_000_00,
            )
        ],
    )
    form = compute_depreciation(inputs, config_for_year(2025)).form_4562
    assert form.line_1_max_amount == 2_500_000_00
    assert form.line_3_threshold == 4_000_000_00
    assert form.line_4_reduction == 0
    assert form.line_5_dollar_limit == 2_500_000_00
    assert form.line_12_section_179_deduction == 2_500_000_00
