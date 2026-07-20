"""Year-aware depreciation constants — authoritative federal figures.

Mirrors ``configs_1040.py``: pure code (no DB) so AO can compute depreciation
deterministically and offline, and an unsupported year RAISES — we never
substitute another year's constants onto a real return.

EVERY constant below carries its source. They were read directly from the
cited IRS publication, not recalled. Re-verify against the current Pub 946 /
Rev. Proc. before each filing season.

VERIFICATION LOG (2026-07-20)
=============================
§179 dollar limit + investment phase-out threshold — IRS Publication 946,
"What's New" section of each year's revision (the publication states both
figures in one sentence):

  TY2022  $1,080,000 / $2,700,000   Pub 946 (2022) "What's New for 2022"
  TY2023  $1,160,000 / $2,890,000   Pub 946 (2023) "What's New for 2023"
  TY2024  $1,220,000 / $3,050,000   Pub 946 (2024) "What's New for 2024"
  TY2025  $2,500,000 / $4,000,000   Pub 946 (2025) "What's New for 2025"
  TY2026  $2,560,000 / $4,090,000   Pub 946 (2025) "What's New for 2026"

⚠ TY2025 IS NOT THE REV. PROC. 2024-40 FIGURE. Rev. Proc. 2024-40 had set
TY2025 at $1,250,000 / $3,130,000 (and Pub 946 (2024) printed those under
"What's New for 2025"). The One Big Beautiful Bill Act (OBBBA), P.L. 119-21,
enacted 2025-07-04, amended §179(b) and RAISED the TY2025 figures to
$2,500,000 / $4,000,000. Pub 946 (2025) reflects the amended figures. Using the
superseded $1,250,000 would understate the deduction by up to $1.25M.

§168(k) special ("bonus") allowance — the applicable percentage. NOTE that from
2025 this depends on when the property was ACQUIRED, not only when it was placed
in service, so it cannot be expressed as one number per year:

  TY2022  100%   §168(k)(6)(A)(i) — 100% for property placed in service before
                 2023-01-01. Corroborated negatively by Pub 946 (2023), which
                 applies 80% only from 2023-01-01.
  TY2023   80%   Pub 946 (2023) "What's New for 2023": "The special depreciation
                 allowance is 80% for certain qualified property acquired after
                 September 27, 2017, and placed in service after December 31,
                 2022, and before January 1, 2024".
  TY2024   60%   Pub 946 (2024) "What's New for 2024": "The special depreciation
                 allowance is 60% for certain qualified property … placed in
                 service after December 31, 2023, and before January 1, 2025
                 (other than certain property with a long production period and
                 certain aircraft)."
  TY2025   SPLIT by acquisition date:
             • acquired AFTER 2025-01-19 → 100%. Pub 946 (2025): "P.L. 119-21,
               commonly known as the One Big Beautiful Bill Act, reinstated the
               100% special depreciation allowance for certain qualified
               property acquired and placed in service after January 19, 2025."
             • acquired ON OR BEFORE 2025-01-19 → 40%. Pub 946 (2025): property
               placed in service after 2024-12-31 and before 2026-01-01 receives
               a 40% allowance.
  TY2026   acquired AFTER 2025-01-19 → 100% (OBBBA made it permanent).
             • acquired ON OR BEFORE 2025-01-19 but placed in service in 2026:
               DELIBERATELY UNSUPPORTED — see the note on
               ``_LEGACY_RATE_UNVERIFIED`` below. The engine RAISES.

§280F(a) passenger-automobile depreciation caps — IRS Pub 946 ch. 5, table
"Maximum Depreciation Deduction for Passenger Automobiles (Including Trucks and
Vans) Acquired After September 27, 2017". Read from Pub 946 (2025), which covers
vehicles placed in service BEFORE 2026. The cap locks to the vehicle's
PLACED-IN-SERVICE year for the whole life of the vehicle and is NOT monotonic
across years (2025 is lower than 2024). Post-TCJA there is ONE table covering
passenger automobiles, trucks and vans — the separate truck/van table applies
only to pre-9/28/2017 acquisitions and is not encoded.

DELIBERATELY NOT ENCODED HERE (a wrong constant is worse than a missing one):
  * §280F(a) caps for vehicles placed in service in 2026 — Pub 946 (2025)'s
    table stops before 2026 and the governing Rev. Proc. was not verified. A
    2026 placement RAISES. (A vehicle placed in service 2022-2025 still computes
    correctly on a 2026 return, because it uses ITS placement year's row.)
  * §280F(c) lease inclusion amounts.
  * "Deductions after the recovery period" — basis disallowed by the §280F(a)
    cap is recoverable in later years subject to the 4th-and-later cap. The
    engine tracks and reports the disallowed amount but does NOT extend the
    schedule past the recovery period, so it never over-deducts.
  * Long-production-period property and certain aircraft, which get a one-year
    -deferred bonus percentage (80% in 2024, 60% in 2025). ``LONG_PRODUCTION``
    below records the rates but the engine does not classify property into that
    category — a caller must not rely on it.

All money is integer CENTS.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.tax.engines.depreciation import BonusRule, DepreciationConfig

# OBBBA (P.L. 119-21) acquisition cutoff. Property ACQUIRED after this date and
# placed in service after it gets the reinstated 100% allowance.
OBBBA_ACQUISITION_CUTOFF = date(2025, 1, 19)

# §179(b)(1) dollar limit and §179(b)(2) investment threshold, in CENTS.
_SECTION_179: dict[int, tuple[int, int, str]] = {
    2022: (108_000_000, 270_000_000, "IRS Pub 946 (2022), What's New for 2022"),
    2023: (116_000_000, 289_000_000, "IRS Pub 946 (2023), What's New for 2023"),
    2024: (122_000_000, 305_000_000, "IRS Pub 946 (2024), What's New for 2024"),
    2025: (
        250_000_000, 400_000_000,
        "IRS Pub 946 (2025), What's New for 2025 (OBBBA, P.L. 119-21 §70306; "
        "SUPERSEDES the $1,250,000/$3,130,000 of Rev. Proc. 2024-40)",
    ),
    2026: (
        256_000_000, 409_000_000,
        "IRS Pub 946 (2025), What's New for 2026 (inflation-adjusted OBBBA "
        "figures; Rev. Proc. 2025-32)",
    ),
}

# §168(k) applicable percentage.
_BONUS: dict[int, BonusRule] = {
    2022: BonusRule(
        percentage=Decimal("1.00"),
        source="IRC §168(k)(6)(A)(i) — 100% for property placed in service before 2023-01-01",
    ),
    2023: BonusRule(
        percentage=Decimal("0.80"),
        source="IRS Pub 946 (2023), What's New for 2023 (§168(k)(6)(A)(ii))",
    ),
    2024: BonusRule(
        percentage=Decimal("0.60"),
        source="IRS Pub 946 (2024), What's New for 2024 (§168(k)(6)(A)(iii))",
    ),
    2025: BonusRule(
        percentage=Decimal("0.40"),
        acquisition_cutoff=OBBBA_ACQUISITION_CUTOFF,
        percentage_if_acquired_after_cutoff=Decimal("1.00"),
        source=(
            "IRS Pub 946 (2025), What's New for 2025 — 40% for property placed "
            "in service in 2025; 100% for property ACQUIRED and placed in "
            "service after 2025-01-19 (OBBBA, P.L. 119-21)"
        ),
    ),
    2026: BonusRule(
        percentage=None,   # legacy (pre-cutoff acquisition) rate NOT verified
        acquisition_cutoff=OBBBA_ACQUISITION_CUTOFF,
        percentage_if_acquired_after_cutoff=Decimal("1.00"),
        source=(
            "IRS Pub 946 (2025) — OBBBA (P.L. 119-21) made the 100% allowance "
            "permanent for property acquired after 2025-01-19. The residual "
            "TCJA phase-down rate for property acquired ON OR BEFORE that date "
            "but placed in service in 2026 was NOT verified from a primary "
            "source and is therefore left unsupported (the engine raises)."
        ),
    ),
}

# Recorded for reference only — the engine does NOT classify property into the
# long-production-period / certain-aircraft category, so it never uses these.
LONG_PRODUCTION_PERIOD_RATES: dict[int, str] = {
    2024: "80% (Pub 946 (2024), What's New for 2024)",
    2025: "60% (Pub 946 (2025), What's New for 2025)",
}

# §280F(a) passenger-automobile caps, keyed by the vehicle's PLACED-IN-SERVICE
# year (the row locks for the vehicle's whole life). Values in CENTS:
#   (1st year WITH the §168(k) allowance,
#    1st year WITHOUT it (elected out / not qualified property),
#    2nd year, 3rd year, 4th-and-later years)
# Source: IRS Pub 946 (2025) ch. 5, "Maximum Depreciation Deduction for
# Passenger Automobiles (Including Trucks and Vans) Acquired After September 27,
# 2017, and Placed in Service Before 2026" (+ its footnotes for the no-bonus
# first-year figures). Cross-checked against Pub 946 (2024) and Pub 946 (2022).
_AUTO_CAPS: dict[int, tuple[int, int, int, int, int]] = {
    2018: (1_800_000, 1_000_000, 1_600_000, 960_000, 576_000),
    2019: (1_810_000, 1_010_000, 1_610_000, 970_000, 576_000),
    2020: (1_810_000, 1_010_000, 1_610_000, 970_000, 576_000),
    2021: (1_820_000, 1_020_000, 1_640_000, 980_000, 586_000),
    2022: (1_920_000, 1_120_000, 1_800_000, 1_080_000, 646_000),
    2023: (2_020_000, 1_220_000, 1_950_000, 1_170_000, 696_000),
    2024: (2_040_000, 1_240_000, 1_980_000, 1_190_000, 716_000),
    2025: (2_020_000, 1_220_000, 1_960_000, 1_180_000, 706_000),
}

AUTO_CAP_SOURCE = (
    "IRS Pub 946 (2025) ch. 5, Maximum Depreciation Deduction for Passenger "
    "Automobiles (Including Trucks and Vans) Acquired After September 27, 2017"
)

# Years for which BOTH the §179 limits and a usable §168(k) rule are on file.
SUPPORTED_TAX_YEARS_DEPRECIATION: tuple[int, ...] = (2022, 2023, 2024, 2025, 2026)

# Placed-in-service years with a verified §280F(a) cap row.
SUPPORTED_AUTO_CAP_YEARS: tuple[int, ...] = tuple(sorted(_AUTO_CAPS))


def passenger_auto_cap(
    placed_in_service_year: int, year_index: int, *, bonus_claimed: bool
) -> int:
    """§280F(a) cap in cents for one tax year of one passenger automobile.

    ``year_index`` is 0-based from the placed-in-service year (0 = first year).
    Raises for a placement year with no published cap row — never guesses, since
    guessing here means OVER-DEDUCTING on a real return.
    """
    row = _AUTO_CAPS.get(placed_in_service_year)
    if row is None:
        raise ValueError(
            f"No §280F(a) passenger-automobile cap on file for a vehicle placed "
            f"in service in {placed_in_service_year} (published years: "
            f"{SUPPORTED_AUTO_CAP_YEARS}). The cap locks to the placement year "
            "and an uncapped figure would OVER-DEDUCT. Add the verified row from "
            "the governing Rev. Proc. / Pub 946 before preparing this return."
        )
    first_with_bonus, first_no_bonus, second, third, later = row
    if year_index <= 0:
        return first_with_bonus if bonus_claimed else first_no_bonus
    if year_index == 1:
        return second
    if year_index == 2:
        return third
    return later


def config_for_year(year: int) -> DepreciationConfig:
    """Build the authoritative depreciation config for a tax year.

    Raises ValueError for an unsupported year — never guesses constants.
    """
    if year not in _SECTION_179 or year not in _BONUS:
        raise ValueError(
            f"No depreciation constants on file for TY{year} (supported: "
            f"{SUPPORTED_TAX_YEARS_DEPRECIATION}). Add verified, cited §179 "
            "limits and a §168(k) rule before preparing this return — do not "
            "reuse another year's figures."
        )
    limit, threshold, source_179 = _SECTION_179[year]
    return DepreciationConfig(
        tax_year=year,
        section_179_limit_cents=limit,
        section_179_threshold_cents=threshold,
        bonus=_BONUS[year],
        section_179_source=source_179,
        bonus_source=_BONUS[year].source,
    )


def section_179_limits(year: int) -> tuple[int, int]:
    """(dollar limit, investment threshold) in cents. Raises on unsupported year."""
    if year not in _SECTION_179:
        raise ValueError(
            f"No §179 limits on file for TY{year} "
            f"(supported: {SUPPORTED_TAX_YEARS_DEPRECIATION})."
        )
    limit, threshold, _ = _SECTION_179[year]
    return limit, threshold


__all__ = [
    "AUTO_CAP_SOURCE",
    "LONG_PRODUCTION_PERIOD_RATES",
    "OBBBA_ACQUISITION_CUTOFF",
    "SUPPORTED_AUTO_CAP_YEARS",
    "SUPPORTED_TAX_YEARS_DEPRECIATION",
    "config_for_year",
    "passenger_auto_cap",
    "section_179_limits",
]
