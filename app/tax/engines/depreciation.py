# Pure deterministic MACRS / §179 / §168(k) depreciation computation.
#
# AUTHORITATIVE SOURCES (cited inline at each rule):
#   * IRC §167, §168 (MACRS), §168(g) (ADS), §168(k) (special/"bonus" allowance),
#     §179 (expensing), §280F (listed property + passenger-auto caps).
#   * IRS Publication 946, "How To Depreciate Property" — Chapter 4 ("Figuring
#     Depreciation Under MACRS") and Appendix A (the percentage tables).
#   * Instructions for Form 4562.
#
# Year-specific dollar limits and the bonus percentage live in
# ``configs_depreciation.py`` (mirrors ``configs_1040.py``). An unsupported year
# RAISES — this engine never substitutes another year's constants onto a real
# return.
#
# Structure/purity mirrors ``form1040.py``: Decimal + pydantic, integer CENTS,
# NO I/O, NO LLM, no DB. Everything here is a pure function of its inputs.
#
# ---------------------------------------------------------------------------
# Why this computes rather than embeds the IRS percentage tables
# ---------------------------------------------------------------------------
# Pub 946 ch. 4 gives taxpayers two equivalent routes: use the Appendix A
# percentage tables, or "figure your MACRS depreciation deduction without using
# the tables". This engine implements the WITHOUT-TABLES method — the declining
# balance computation with a switch to straight line — which is precisely the
# algorithm the IRS used to GENERATE Appendix A.
#
# Rationale: one auditable algorithm instead of ~20 transcribed tables (every
# transcribed digit is a place to silently introduce a wrong number on a real
# return), and it generalizes to any recovery period / convention / short year.
#
# VERIFIED: ``tests/unit/test_depreciation_engine.py`` asserts this algorithm
# reproduces IRS Table A-1 (GDS half-year), Tables A-2..A-5 (mid-quarter, all
# four quarters — these match to the CENT), and A-6 (mid-month residential).
# The published tables round to 2 decimal places and then nudge individual years
# so the column sums to exactly 100.00%; this engine instead carries full Decimal
# precision and allocates the true remaining basis in the final year. Net effect:
# a given year can differ from the published table by up to 0.01% OF BASIS (e.g.
# 7-year property year 5: table 8.93%, computed 8.9249%), while the LIFETIME
# total is exactly the depreciable basis in both routes. Both are permissible
# under Pub 946; see ``PUB946_TABLE_ROUNDING_NOTE`` and the report note.
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from pydantic import BaseModel, Field, model_validator

CENT = Decimal("1")
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")

PUB946_TABLE_ROUNDING_NOTE = (
    "Depreciation is computed under the Pub 946 ch. 4 'without using the tables' "
    "method (declining balance with a switch to straight line). A single year may "
    "differ from the Appendix A percentage tables by up to 0.01% of basis because "
    "the published tables are rounded to two decimals and force the column to sum "
    "to 100.00%; the lifetime total is identical."
)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class DepreciationSystem(str, Enum):
    """§168(a) GDS vs §168(g) ADS."""

    GDS = "GDS"
    ADS = "ADS"


class Convention(str, Enum):
    """§168(d) applicable convention."""

    HALF_YEAR = "HY"        # §168(d)(1)
    MID_QUARTER = "MQ"      # §168(d)(3)
    MID_MONTH = "MM"        # §168(d)(2) — residential rental / nonresidential real


class Method(str, Enum):
    """§168(b) applicable depreciation method."""

    DB_200 = "200DB"        # §168(b)(1)
    DB_150 = "150DB"        # §168(b)(2)
    SL = "S/L"              # §168(b)(3), §168(g)(2)(A)


# NOTE ON FORM ROWS
# -----------------
# The engine deliberately does NOT know which printed row (19a…19j / 20a…20e) a
# class lands on, because THAT MAPPING MOVED between revisions: the 2025 Form
# 4562 inserted a new "50-year property" row at 19h, shifting residential rental
# 27.5 from 19h to 19i and nonresidential real 39 from 19i to 19j (and added a
# 50-year ADS row at 20e). Hard-coding 2024's rows and rendering them onto the
# 2025 form would silently file residential rental depreciation in the 50-year
# row. So the engine keys Section B/C by the RECOVERY PERIOD, and
# ``app.exports.f4562_field_map`` owns the per-revision period -> row mapping.

# GDS recovery periods that are real property and therefore take the mid-month
# convention (§168(d)(2)) and the straight line method (§168(b)(3)).
REAL_PROPERTY_PERIODS = frozenset({Decimal("27.5"), Decimal("39"), Decimal("31.5")})

# §168(b)(1): 200% DB applies to 3-, 5-, 7-, and 10-year property.
# §168(b)(2): 150% DB applies to 15- and 20-year property (and farming property).
_DB200_PERIODS = frozenset({Decimal("3"), Decimal("5"), Decimal("7"), Decimal("10")})
_DB150_PERIODS = frozenset({Decimal("15"), Decimal("20")})


def cents(value: Decimal) -> int:
    return int(value.quantize(CENT, rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Year config (built in configs_depreciation.py)
# ---------------------------------------------------------------------------


class BonusRule(BaseModel):
    """The §168(k) applicable percentage for one tax year.

    From 2025 this is NOT a single number: OBBBA (P.L. 119-21) reinstated the
    100% allowance for property ACQUIRED after 2025-01-19, while property
    acquired on or before that date stays on the TCJA phase-down. So the rate
    depends on the acquisition date, not only the placed-in-service date, and an
    asset with no recorded acquisition date CANNOT be resolved — the engine
    raises rather than picking one.

    ``percentage`` may be None, meaning "no verified rate on file for the
    pre-cutoff case" — that path raises too.
    """

    percentage: Decimal | None = None
    acquisition_cutoff: date | None = None
    percentage_if_acquired_after_cutoff: Decimal | None = None
    source: str

    def rate_for(self, acquired_on: date | None, asset_id: str) -> Decimal:
        """Resolve the applicable percentage, or raise with a specific reason."""
        if self.acquisition_cutoff is None:
            if self.percentage is None:
                raise ValueError(
                    f"asset {asset_id!r}: no verified §168(k) percentage on file "
                    "for this year. Refusing to guess."
                )
            return self.percentage
        if acquired_on is None:
            raise ValueError(
                f"asset {asset_id!r}: the §168(k) percentage for this year "
                f"depends on whether the property was ACQUIRED after "
                f"{self.acquisition_cutoff.isoformat()} (OBBBA, P.L. 119-21), "
                "but the asset has no acquired_on date. Supply it — the engine "
                "will not guess between the phase-down rate and 100%."
            )
        if acquired_on > self.acquisition_cutoff:
            if self.percentage_if_acquired_after_cutoff is None:
                raise ValueError(
                    f"asset {asset_id!r}: no post-cutoff §168(k) rate on file."
                )
            return self.percentage_if_acquired_after_cutoff
        if self.percentage is None:
            raise ValueError(
                f"asset {asset_id!r}: acquired {acquired_on.isoformat()} (on or "
                f"before the {self.acquisition_cutoff.isoformat()} OBBBA "
                "cutoff), but no verified residual TCJA phase-down percentage "
                "is on file for this tax year. This return cannot be prepared "
                "automatically — verify the rate against Pub 946 and add it."
            )
        return self.percentage


class DepreciationConfig(BaseModel):
    """Year-specific statutory constants. Built by ``configs_depreciation``."""

    tax_year: int
    # §179(b)(1) dollar limitation and §179(b)(2) investment phase-out threshold.
    section_179_limit_cents: int
    section_179_threshold_cents: int
    # §168(k) applicable percentage rule (acquisition-date aware).
    bonus: BonusRule
    # Citation strings carried onto the result's provenance so a reviewer can
    # trace every constant back to its authority.
    section_179_source: str
    bonus_source: str


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class AssetInput(BaseModel):
    """One depreciable asset.

    Money is integer CENTS. ``recovery_period_years`` is the GDS or ADS recovery
    period (3/5/7/10/15/20/25/27.5/39 for GDS; the ADS class life for ADS).
    """

    asset_id: str
    description: str = ""
    placed_in_service: date
    # When the property was ACQUIRED (contract/purchase date). Required from
    # TY2025 on, because §168(k)'s applicable percentage turns on whether
    # acquisition was after the OBBBA 2025-01-19 cutoff — see ``BonusRule``.
    acquired_on: date | None = None
    cost_cents: int
    recovery_period_years: Decimal
    system: DepreciationSystem = DepreciationSystem.GDS
    # Optional overrides. When omitted they are DERIVED from the recovery period
    # + system per §168(b)/(d) — see ``_default_method`` / ``_default_convention``.
    method: Method | None = None
    convention: Convention | None = None

    # §280F listed property (autos, certain other vehicles/aircraft, …).
    is_listed_property: bool = False
    # Qualified business/investment use percentage (0-100). Depreciable basis is
    # cost x this percentage (§280F(b) / Form 4562 Part V col (e)).
    business_use_pct: Decimal = HUNDRED

    # §179 elected cost for this asset (only meaningful in the year placed in
    # service). The engine applies the §179(b)(1)/(2) limits on top.
    section_179_elected_cents: int = 0
    # §168(k)(7) allows an irrevocable election OUT of bonus by class.
    bonus_elected_out: bool = False

    # For an asset placed in service in a year this engine does NOT support
    # (before the config window), the §179 and bonus amounts actually taken in
    # that year MUST be supplied — the engine will not guess them.
    prior_section_179_cents: int | None = None
    prior_bonus_cents: int | None = None

    @model_validator(mode="after")
    def _validate(self) -> AssetInput:
        if self.cost_cents < 0:
            raise ValueError(f"asset {self.asset_id!r}: cost_cents must be >= 0")
        if self.recovery_period_years <= 0:
            raise ValueError(
                f"asset {self.asset_id!r}: recovery_period_years must be > 0"
            )
        if not (ZERO <= self.business_use_pct <= HUNDRED):
            raise ValueError(
                f"asset {self.asset_id!r}: business_use_pct must be 0-100"
            )
        if self.section_179_elected_cents < 0:
            raise ValueError(
                f"asset {self.asset_id!r}: section_179_elected_cents must be >= 0"
            )
        return self


class DepreciationInputs(BaseModel):
    """Everything needed to produce one tax year's Form 4562."""

    tax_year: int
    assets: list[AssetInput] = Field(default_factory=list)

    # §179(b)(3)(A) taxable-income limitation: aggregate taxable income derived
    # from the active conduct of any trade or business, computed WITHOUT the
    # §179 deduction. Required whenever any §179 is elected.
    business_taxable_income_cents: int | None = None
    # §179(b)(3)(B) carryover of disallowed deduction from the prior year
    # (Form 4562 line 10).
    section_179_carryforward_in_cents: int = 0

    # Form 4562 line 16 — depreciation not computed by this engine (e.g. ACRS,
    # non-MACRS methods). Passed through; must carry its own provenance upstream.
    other_depreciation_cents: int = 0
    # Part VI amortization (line 43: costs that began before this tax year).
    amortization_prior_years_cents: int = 0
    amortization_current_year: list[AmortizationItem] = Field(default_factory=list)

    # Set when the tax year is a short year; not yet supported (raises).
    is_short_tax_year: bool = False


class AmortizationItem(BaseModel):
    """One Form 4562 Part VI line 42 row."""

    description: str
    begins: date
    amortizable_amount_cents: int
    code_section: str
    period_months: int

    @model_validator(mode="after")
    def _validate(self) -> AmortizationItem:
        if self.period_months <= 0:
            raise ValueError("amortization period_months must be > 0")
        return self


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class AssetYear(BaseModel):
    """One tax year of one asset's depreciation schedule."""

    year: int
    deduction_cents: int
    beginning_basis_cents: int
    ending_basis_cents: int


class AssetResult(BaseModel):
    asset_id: str
    description: str
    placed_in_service: date
    system: DepreciationSystem
    method: Method
    convention: Convention
    recovery_period_years: Decimal
    cost_cents: int
    business_use_pct: Decimal
    # cost x business_use_pct — the §280F-limited starting point.
    business_basis_cents: int
    section_179_cents: int
    bonus_cents: int
    # Basis actually run through MACRS (business basis less §179 less bonus).
    macrs_basis_cents: int
    # This tax year's MACRS deduction only (excludes §179 + bonus).
    current_year_macrs_cents: int
    # §179 + bonus + MACRS for this tax year.
    current_year_total_cents: int
    is_first_year: bool
    is_listed_property: bool
    # Form 4562 row this asset reports on (e.g. "19b", "20a", "26").
    form_line: str
    schedule: list[AssetYear] = Field(default_factory=list)


class Form4562Lines(BaseModel):
    """Form 4562 line values, in integer CENTS.

    Keys are the printed line numbers. Section B/C rows are aggregated by
    RECOVERY PERIOD (e.g. ``"5"``, ``"27.5"``) into ``part_iii_section_b`` /
    ``part_iii_section_c`` — NOT by printed row letter, which moved between form
    revisions (see NOTE ON FORM ROWS at the top of this module).
    """

    # Part I — §179
    line_1_max_amount: int
    line_2_total_179_property: int
    line_3_threshold: int
    line_4_reduction: int
    line_5_dollar_limit: int
    line_6_properties: list[Section179Property] = Field(default_factory=list)
    line_7_listed_property: int
    line_8_total_elected_cost: int
    line_9_tentative_deduction: int
    line_10_carryover_from_prior: int
    line_11_business_income_limit: int
    line_12_section_179_deduction: int
    line_13_carryover_to_next: int
    # Part II
    line_14_special_allowance: int
    line_15_section_168f1: int
    line_16_other_depreciation: int
    # Part III
    line_17_prior_year_macrs: int
    part_iii_section_b: dict[str, MacrsClassRow] = Field(default_factory=dict)
    part_iii_section_c: dict[str, MacrsClassRow] = Field(default_factory=dict)
    # Part IV
    line_21_listed_property: int
    line_22_total: int
    # Part V
    line_25_listed_special_allowance: int
    line_26_over_50_pct: list[ListedPropertyRow] = Field(default_factory=list)
    line_27_50_pct_or_less: list[ListedPropertyRow] = Field(default_factory=list)
    line_28_total_listed: int
    line_29_listed_179: int
    # Part VI
    line_42_amortization: list[AmortizationRow] = Field(default_factory=list)
    line_43_amortization_prior: int
    line_44_amortization_total: int


class Section179Property(BaseModel):
    description: str
    cost_cents: int
    elected_cost_cents: int


class MacrsClassRow(BaseModel):
    """A Form 4562 Part III Section B/C row (line 19a-19i / 20a-20d)."""

    basis_cents: int
    recovery_period_years: Decimal
    convention: Convention
    method: Method
    deduction_cents: int
    # Month/year placed in service — only printed for the mid-month real-property
    # rows (19h/19i) per the form's column (b).
    month_year: str | None = None


class ListedPropertyRow(BaseModel):
    """A Form 4562 Part V line 26/27 row."""

    description: str
    placed_in_service: date
    business_use_pct: Decimal
    cost_cents: int
    basis_cents: int
    recovery_period_years: Decimal
    method_convention: str
    deduction_cents: int
    elected_179_cents: int = 0


class AmortizationRow(BaseModel):
    description: str
    begins: date
    amortizable_amount_cents: int
    code_section: str
    period_months: int
    amortization_cents: int


class DepreciationResult(BaseModel):
    tax_year: int
    assets: list[AssetResult]
    form_4562: Form4562Lines
    # Grand total deductible depreciation for the year (Form 4562 line 22),
    # i.e. §179 + bonus + MACRS + other. Excludes amortization (line 44), which
    # is reported separately on the return.
    total_depreciation_cents: int
    total_amortization_cents: int
    section_179_carryforward_out_cents: int
    # Whether the mid-quarter convention was triggered (§168(d)(3)).
    mid_quarter_applies: bool
    assumptions: list[str] = Field(default_factory=list)
    # Citations for every year-specific constant actually used.
    citations: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# §168(b)/(d) defaults
# ---------------------------------------------------------------------------


def _default_method(period: Decimal, system: DepreciationSystem) -> Method:
    """Applicable depreciation method per §168(b) (GDS) / §168(g)(2) (ADS)."""
    if system is DepreciationSystem.ADS:
        return Method.SL          # §168(g)(2)(A): ADS is straight line.
    if period in REAL_PROPERTY_PERIODS:
        return Method.SL          # §168(b)(3): real property is straight line.
    if period in _DB200_PERIODS:
        return Method.DB_200      # §168(b)(1)
    if period in _DB150_PERIODS:
        return Method.DB_150      # §168(b)(2)
    return Method.SL              # e.g. 25-year water utility — §168(b)(3)(F).


def _default_convention(period: Decimal, mid_quarter: bool) -> Convention:
    """Applicable convention per §168(d)."""
    if period in REAL_PROPERTY_PERIODS:
        return Convention.MID_MONTH        # §168(d)(2)
    if mid_quarter:
        return Convention.MID_QUARTER      # §168(d)(3)
    return Convention.HALF_YEAR            # §168(d)(1)


def _first_year_service_fraction(
    convention: Convention, placed: date
) -> Decimal:
    """Fraction of the first tax year the property is deemed in service.

    * Half-year (§168(d)(4)(A)): deemed placed in service at the MIDPOINT of the
      tax year -> 1/2.
    * Mid-quarter (§168(d)(4)(C)): midpoint of the quarter -> 7/8, 5/8, 3/8, 1/8
      for quarters 1-4 respectively.
    * Mid-month (§168(d)(4)(B)): midpoint of the month -> (12 - month + 0.5)/12.
    """
    if convention is Convention.HALF_YEAR:
        return Decimal("0.5")
    if convention is Convention.MID_QUARTER:
        quarter = (placed.month - 1) // 3 + 1
        # Months remaining from the midpoint of the quarter to year end, / 12.
        return (Decimal(12) - (Decimal(quarter) * 3 - Decimal("1.5"))) / Decimal(12)
    # Mid-month.
    return (Decimal(12) - Decimal(placed.month) + Decimal("0.5")) / Decimal(12)


# ---------------------------------------------------------------------------
# The MACRS schedule (Pub 946 ch. 4, "without using the tables")
# ---------------------------------------------------------------------------


def macrs_schedule(
    basis_cents: int,
    recovery_period_years: Decimal,
    method: Method,
    convention: Convention,
    placed_in_service: date,
) -> list[int]:
    """Full-life MACRS deduction schedule, in cents, one entry per tax year.

    Declining balance switching to straight line in the first year the SL amount
    on the remaining basis over the remaining recovery period equals or exceeds
    the DB amount (Pub 946 ch. 4). Reproduces IRS Pub 946 Appendix A.

    The last entry absorbs any residual so the schedule sums EXACTLY to
    ``basis_cents`` (no basis is created or lost to rounding).
    """
    if basis_cents <= 0:
        return []

    n = Decimal(recovery_period_years)
    f1 = _first_year_service_fraction(convention, placed_in_service)
    db_factor = {
        Method.DB_200: Decimal(2),
        Method.DB_150: Decimal("1.5"),
        Method.SL: None,
    }[method]

    basis = Decimal(basis_cents)
    remaining = basis
    service_used = ZERO
    straight_line_mode = db_factor is None
    rate = (db_factor / n) if db_factor is not None else None

    amounts: list[Decimal] = []
    guard = 0
    while remaining > ZERO and guard < 200:
        guard += 1
        remaining_life = n - service_used
        if remaining_life <= ZERO:
            break
        # Service fraction for this tax year: the first year uses the
        # convention's fraction, later years a full year (capped by what is
        # left of the recovery period, which yields the stub final year).
        service = f1 if not amounts else min(ONE, remaining_life)

        sl_amount = remaining / remaining_life * service
        if straight_line_mode:
            amount = sl_amount
        else:
            db_amount = remaining * rate * service
            if sl_amount >= db_amount:
                straight_line_mode = True
                amount = sl_amount
            else:
                amount = db_amount

        amount = min(amount, remaining)
        amounts.append(amount)
        remaining -= amount
        service_used += service

    if not amounts:
        return []

    # Quantize to cents, forcing the total to equal the basis exactly.
    out: list[int] = []
    allocated = 0
    for amount in amounts[:-1]:
        c = cents(amount)
        out.append(c)
        allocated += c
    out.append(basis_cents - allocated)
    # Defensive: never emit a negative final year (possible only if the
    # per-year rounding overshot, which the algorithm above prevents).
    if out[-1] < 0:
        out[-2] += out[-1]
        out[-1] = 0
    return out


# ---------------------------------------------------------------------------
# §168(d)(3) mid-quarter trigger test
# ---------------------------------------------------------------------------


def mid_quarter_applies(assets: list[AssetInput], tax_year: int) -> bool:
    """§168(d)(3): mid-quarter applies when the aggregate basis of property
    placed in service during the LAST 3 MONTHS of the tax year exceeds 40% of
    the aggregate basis of property placed in service during the ENTIRE year.

    §168(d)(3)(B) EXCLUDES from both sides of the test:
      (i)  residential rental and nonresidential real property (the mid-month
           classes), and
      (ii) property placed in service and disposed of in the same tax year.

    Pub 946 ch. 4 further notes the aggregate bases used in this test are the
    UNADJUSTED bases — i.e. before any §179 expense and before the §168(k)
    special allowance. This engine measures the business-use-adjusted cost
    (§280F basis), which is the basis of the property for depreciation purposes.
    """
    total = ZERO
    last_quarter = ZERO
    for asset in assets:
        if asset.placed_in_service.year != tax_year:
            continue
        if asset.recovery_period_years in REAL_PROPERTY_PERIODS:
            continue  # §168(d)(3)(B)(i)
        basis = _business_basis(asset)
        total += basis
        if asset.placed_in_service.month >= 10:
            last_quarter += basis
    if total <= ZERO:
        return False
    return last_quarter > total * Decimal("0.40")


def _business_basis(asset: AssetInput) -> Decimal:
    """Cost x qualified business-use percentage (§280F(b), Form 4562 col (e))."""
    return Decimal(asset.cost_cents) * asset.business_use_pct / HUNDRED


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------


def compute_depreciation(
    inputs: DepreciationInputs,
    config: DepreciationConfig,
    *,
    config_for_year: "ConfigResolver | None" = None,
) -> DepreciationResult:
    """Compute one tax year's depreciation + the Form 4562 line values.

    ``config`` is the CURRENT tax year's config (drives §179 limits).
    ``config_for_year`` resolves the config for an asset's PLACED-IN-SERVICE year
    (drives that asset's bonus percentage, which is fixed at placement — a 2023
    asset keeps its 2023 bonus rate on a 2026 return). When omitted, only assets
    placed in service in the current tax year can have bonus computed; a
    prior-year asset must then supply ``prior_bonus_cents``.

    Raises ValueError on an unsupported configuration rather than guessing.
    """
    if inputs.tax_year != config.tax_year:
        raise ValueError(
            f"config is for TY{config.tax_year} but inputs are for "
            f"TY{inputs.tax_year} — refusing to apply the wrong year's limits"
        )
    if inputs.is_short_tax_year:
        raise ValueError(
            "short tax years are not supported: the §168(d)(4) conventions and "
            "the §179 limits must be prorated, which this engine does not yet "
            "implement. Prepare this return manually."
        )

    assumptions: list[str] = []
    citations: dict[str, str] = {
        "section_179": config.section_179_source,
        "bonus_168k": config.bonus_source,
        "macrs_method": (
            "IRC §168(b)/(d); IRS Pub 946 ch. 4 (computation without the "
            "Appendix A tables)"
        ),
    }

    mq = mid_quarter_applies(inputs.assets, inputs.tax_year)
    if mq:
        assumptions.append(
            "mid_quarter_convention_applies: more than 40% of the aggregate "
            "basis of non-real property was placed in service in the last three "
            "months of the tax year (§168(d)(3)), so ALL such property placed in "
            "service this year uses the mid-quarter convention."
        )

    # ---- Pass 1: resolve per-asset method/convention/basis -----------------
    resolved: list[tuple[AssetInput, Method, Convention, Decimal, bool]] = []
    for asset in inputs.assets:
        first_year = asset.placed_in_service.year == inputs.tax_year
        period = asset.recovery_period_years
        system = asset.system
        business_use = asset.business_use_pct

        # §280F(b)(1): listed property used 50% or less in qualified business
        # use MUST use ADS straight line, and is ineligible for §179
        # (§179(d)(1) via §280F(b)(3)) and for bonus (§168(k)(2)(D)(i)).
        listed_below_threshold = (
            asset.is_listed_property and business_use <= Decimal("50")
        )
        if listed_below_threshold:
            if system is not DepreciationSystem.ADS:
                system = DepreciationSystem.ADS
                assumptions.append(
                    f"asset {asset.asset_id}: listed property at "
                    f"{business_use}% business use (<=50%) — forced to ADS "
                    "straight line per §280F(b)(1); §179 and §168(k) bonus "
                    "disallowed."
                )

        method = asset.method or _default_method(period, system)
        convention = asset.convention or _default_convention(period, mq if first_year else False)
        # A prior-year asset keeps the convention it was placed in service
        # under. The caller must supply it explicitly when that year's
        # mid-quarter status differs from this year's; otherwise we would
        # silently re-derive the wrong convention.
        if not first_year and asset.convention is None and period not in REAL_PROPERTY_PERIODS:
            assumptions.append(
                f"asset {asset.asset_id}: placed in service "
                f"{asset.placed_in_service.isoformat()} (a prior year) with no "
                "explicit convention — assumed half-year. Supply "
                "`convention` if the mid-quarter convention applied in that year."
            )

        resolved.append((asset, method, convention, business_use, first_year))

    # ---- Pass 2: §179 (Form 4562 Part I) -----------------------------------
    # §179(b)(2): the dollar limit is reduced (not below zero) by the amount by
    # which the cost of §179 property placed in service this year exceeds the
    # threshold. Only CURRENT-year property counts.
    line_2 = ZERO
    for asset, _m, _c, _bu, first_year in resolved:
        if not first_year:
            continue
        if asset.is_listed_property and asset.business_use_pct <= Decimal("50"):
            continue  # ineligible property is not §179 property
        line_2 += _business_basis(asset)

    line_1 = Decimal(config.section_179_limit_cents)
    line_3 = Decimal(config.section_179_threshold_cents)
    line_4 = max(ZERO, line_2 - line_3)
    line_5 = max(ZERO, line_1 - line_4)

    # Elected costs, split between line 6 (non-listed) and line 7 (listed).
    line_6_props: list[Section179Property] = []
    elected_listed = ZERO
    elected_total = ZERO
    for asset, _m, _c, _bu, first_year in resolved:
        elected = Decimal(asset.section_179_elected_cents)
        if elected <= ZERO:
            continue
        if not first_year:
            raise ValueError(
                f"asset {asset.asset_id!r} elects §179 but was placed in service "
                f"{asset.placed_in_service.isoformat()}, not in TY{inputs.tax_year}. "
                "§179 may only be elected in the year the property is placed in "
                "service (§179(a))."
            )
        if asset.is_listed_property and asset.business_use_pct <= Decimal("50"):
            raise ValueError(
                f"asset {asset.asset_id!r} elects §179 but is listed property at "
                f"{asset.business_use_pct}% business use. §280F(b)(3) denies §179 "
                "for listed property not used predominantly (>50%) in a qualified "
                "business use."
            )
        business_basis = _business_basis(asset)
        if elected > business_basis:
            raise ValueError(
                f"asset {asset.asset_id!r}: §179 elected cost "
                f"{elected} exceeds the business-use basis {business_basis}."
            )
        elected_total += elected
        if asset.is_listed_property:
            elected_listed += elected
        else:
            line_6_props.append(
                Section179Property(
                    description=asset.description or asset.asset_id,
                    cost_cents=cents(business_basis),
                    elected_cost_cents=cents(elected),
                )
            )

    line_7 = elected_listed
    line_8 = elected_total
    line_9 = min(line_5, line_8)              # tentative deduction
    line_10 = Decimal(inputs.section_179_carryforward_in_cents)

    # §179(b)(3)(A) taxable-income limitation.
    if line_8 > ZERO or line_10 > ZERO:
        if inputs.business_taxable_income_cents is None:
            raise ValueError(
                "business_taxable_income_cents is required when §179 is elected "
                "or a §179 carryforward exists — §179(b)(3)(A) limits the "
                "deduction to taxable income derived from the active conduct of "
                "a trade or business. Refusing to guess it."
            )
        business_income = max(ZERO, Decimal(inputs.business_taxable_income_cents))
    else:
        business_income = ZERO
    # Form 4562 line 11: the smaller of business income or line 5.
    line_11 = min(business_income, line_5)
    # Line 12: add lines 9 and 10, but don't enter more than line 11.
    line_12 = min(line_9 + line_10, line_11)
    # Line 13: carryover to next year = lines 9 + 10 less line 12.
    line_13 = line_9 + line_10 - line_12

    if line_13 > ZERO:
        assumptions.append(
            f"section_179_carryforward: {cents(line_13)} cents of elected §179 "
            "was disallowed by the §179(b)(3)(A) taxable-income limitation and "
            "carries forward to next year (§179(b)(3)(B), Form 4562 line 13)."
        )

    # Allocate the ALLOWED §179 back to assets, in election order, so each
    # asset's remaining basis is right. Only the current year's elections are
    # allocated (the carryforward attaches to no specific asset).
    allowed_current = max(ZERO, min(line_12, line_9))
    section_179_by_asset: dict[str, Decimal] = {}
    remaining_allowance = allowed_current
    for asset, _m, _c, _bu, first_year in resolved:
        elected = Decimal(asset.section_179_elected_cents)
        if elected <= ZERO or not first_year:
            continue
        take = min(elected, remaining_allowance)
        section_179_by_asset[asset.asset_id] = take
        remaining_allowance -= take

    # ---- Pass 3: per-asset bonus + MACRS -----------------------------------
    asset_results: list[AssetResult] = []
    for asset, method, convention, business_use, first_year in resolved:
        business_basis = _business_basis(asset)
        s179 = section_179_by_asset.get(asset.asset_id, ZERO)

        # §168(k) bonus, on the basis remaining after §179 (§168(k)(1)(A) —
        # "after any §179 deduction"). Fixed at the placed-in-service year.
        if first_year:
            if asset.bonus_elected_out:
                bonus = ZERO
            elif asset.is_listed_property and business_use <= Decimal("50"):
                bonus = ZERO   # §168(k)(2)(D)(i) via §280F(b)(1)
            else:
                rate = config.bonus.rate_for(asset.acquired_on, asset.asset_id)
                bonus = (business_basis - s179) * rate
        else:
            # Prior-year asset: bonus was taken in the placement year.
            if asset.prior_bonus_cents is not None:
                bonus = Decimal(asset.prior_bonus_cents)
            elif config_for_year is not None:
                prior_cfg = config_for_year(asset.placed_in_service.year)
                if asset.bonus_elected_out or (
                    asset.is_listed_property and business_use <= Decimal("50")
                ):
                    bonus = ZERO
                else:
                    rate = prior_cfg.bonus.rate_for(asset.acquired_on, asset.asset_id)
                    bonus = (
                        business_basis - Decimal(asset.prior_section_179_cents or 0)
                    ) * rate
            else:
                raise ValueError(
                    f"asset {asset.asset_id!r} was placed in service in "
                    f"{asset.placed_in_service.year} (before the current tax "
                    f"year) but no prior_bonus_cents was supplied and no "
                    "config resolver is available for that year. Supply the "
                    "§168(k) allowance actually taken — the engine will not "
                    "guess it."
                )
            if asset.prior_section_179_cents is not None:
                s179 = Decimal(asset.prior_section_179_cents)

        bonus = max(ZERO, min(bonus, business_basis - s179))
        macrs_basis = business_basis - s179 - bonus

        schedule_cents = macrs_schedule(
            cents(macrs_basis),
            asset.recovery_period_years,
            method,
            convention,
            asset.placed_in_service,
        )
        first_tax_year = asset.placed_in_service.year
        schedule: list[AssetYear] = []
        running = cents(macrs_basis)
        for offset, amount in enumerate(schedule_cents):
            beginning = running
            running -= amount
            schedule.append(
                AssetYear(
                    year=first_tax_year + offset,
                    deduction_cents=amount,
                    beginning_basis_cents=beginning,
                    ending_basis_cents=running,
                )
            )
        current_macrs = next(
            (s.deduction_cents for s in schedule if s.year == inputs.tax_year), 0
        )

        current_total = current_macrs + (
            cents(s179) + cents(bonus) if first_year else 0
        )
        asset_results.append(
            AssetResult(
                asset_id=asset.asset_id,
                description=asset.description,
                placed_in_service=asset.placed_in_service,
                system=asset.system
                if not (asset.is_listed_property and business_use <= Decimal("50"))
                else DepreciationSystem.ADS,
                method=method,
                convention=convention,
                recovery_period_years=asset.recovery_period_years,
                cost_cents=asset.cost_cents,
                business_use_pct=business_use,
                business_basis_cents=cents(business_basis),
                section_179_cents=cents(s179) if first_year else 0,
                bonus_cents=cents(bonus) if first_year else 0,
                macrs_basis_cents=cents(macrs_basis),
                current_year_macrs_cents=current_macrs,
                current_year_total_cents=current_total,
                is_first_year=first_year,
                is_listed_property=asset.is_listed_property,
                form_line=_form_line_for(asset, first_year, method, convention),
                schedule=schedule,
            )
        )

    # ---- Pass 4: assemble the Form 4562 line values ------------------------
    form = _build_form_lines(
        inputs=inputs,
        config=config,
        results=asset_results,
        line_1=line_1, line_2=line_2, line_3=line_3, line_4=line_4, line_5=line_5,
        line_6_props=line_6_props, line_7=line_7, line_8=line_8, line_9=line_9,
        line_10=line_10, line_11=line_11, line_12=line_12, line_13=line_13,
    )

    total_amort = form.line_44_amortization_total
    return DepreciationResult(
        tax_year=inputs.tax_year,
        assets=asset_results,
        form_4562=form,
        total_depreciation_cents=form.line_22_total,
        total_amortization_cents=total_amort,
        section_179_carryforward_out_cents=cents(line_13),
        mid_quarter_applies=mq,
        assumptions=assumptions,
        citations=citations,
    )


def _form_line_for(
    asset: AssetInput, first_year: bool, method: Method, convention: Convention
) -> str:
    """Which Form 4562 SECTION this asset reports in.

    Returns a revision-independent key:
      * ``"26"`` / ``"27"``  — Part V listed property (>50% / <=50% business use)
      * ``"17"``             — Part III Section A line 17 (prior-year MACRS)
      * ``"B:<period>"``     — Part III Section B (GDS), by recovery period
      * ``"C:<period>"``     — Part III Section C (ADS), by recovery period

    The printed row letter (19a…19j, 20a…20e) is resolved per revision by
    ``app.exports.f4562_field_map`` — see the NOTE ON FORM ROWS above.
    """
    if asset.is_listed_property:
        return "26" if asset.business_use_pct > Decimal("50") else "27"
    if not first_year:
        return "17"   # Part III Section A, line 17 (prior-year MACRS)
    period = _period_key(asset.recovery_period_years)
    if asset.system is DepreciationSystem.ADS or (
        asset.is_listed_property and asset.business_use_pct <= Decimal("50")
    ):
        return f"C:{period}"
    return f"B:{period}"


def _period_key(period: Decimal) -> str:
    """Normalize a recovery period to the GDS_CLASS_ROW / ADS_CLASS_ROW key."""
    normalized = period.normalize()
    text = format(normalized, "f")
    return text


def _build_form_lines(
    *,
    inputs: DepreciationInputs,
    config: DepreciationConfig,
    results: list[AssetResult],
    line_1: Decimal, line_2: Decimal, line_3: Decimal, line_4: Decimal,
    line_5: Decimal, line_6_props: list[Section179Property], line_7: Decimal,
    line_8: Decimal, line_9: Decimal, line_10: Decimal, line_11: Decimal,
    line_12: Decimal, line_13: Decimal,
) -> Form4562Lines:
    """Aggregate per-asset results into Form 4562 line values."""
    # Part II line 14 — bonus for non-listed property placed in service this year.
    line_14 = sum(
        r.bonus_cents for r in results if r.is_first_year and not r.is_listed_property
    )
    # Part V line 25 — bonus for qualified LISTED property.
    line_25 = sum(
        r.bonus_cents for r in results if r.is_first_year and r.is_listed_property
    )
    # Part III line 17 — MACRS on assets placed in service in PRIOR years
    # (excludes listed property, which is reported in Part V).
    line_17 = sum(
        r.current_year_macrs_cents
        for r in results
        if not r.is_first_year and not r.is_listed_property
    )

    # Part III Sections B and C — current-year, non-listed assets by class.
    section_b: dict[str, MacrsClassRow] = {}
    section_c: dict[str, MacrsClassRow] = {}
    for r in results:
        if not r.is_first_year or r.is_listed_property:
            continue
        target = section_c if r.form_line.startswith("C:") else section_b
        row_key = r.form_line.split(":", 1)[1]
        row = target.get(row_key)
        month_year = (
            f"{r.placed_in_service.month:02d}/{r.placed_in_service.year}"
            if r.convention is Convention.MID_MONTH
            else None
        )
        if row is None:
            target[row_key] = MacrsClassRow(
                basis_cents=r.macrs_basis_cents,
                recovery_period_years=r.recovery_period_years,
                convention=r.convention,
                method=r.method,
                deduction_cents=r.current_year_macrs_cents,
                month_year=month_year,
            )
        else:
            # Aggregate same-class assets onto one row (the form has one row per
            # class). Keep the first asset's month/year — the form has a single
            # column (b) per row, so mixed months must be split by the caller.
            target[row_key] = MacrsClassRow(
                basis_cents=row.basis_cents + r.macrs_basis_cents,
                recovery_period_years=row.recovery_period_years,
                convention=row.convention,
                method=row.method,
                deduction_cents=row.deduction_cents + r.current_year_macrs_cents,
                month_year=row.month_year,
            )

    # Part V — listed property rows.
    line_26: list[ListedPropertyRow] = []
    line_27: list[ListedPropertyRow] = []
    for r in results:
        if not r.is_listed_property:
            continue
        row = ListedPropertyRow(
            description=r.description or r.asset_id,
            placed_in_service=r.placed_in_service,
            business_use_pct=r.business_use_pct,
            cost_cents=r.cost_cents,
            basis_cents=r.macrs_basis_cents,
            recovery_period_years=r.recovery_period_years,
            method_convention=f"{r.method.value}/{r.convention.value}",
            deduction_cents=r.current_year_macrs_cents,
            elected_179_cents=r.section_179_cents,
        )
        if r.business_use_pct > Decimal("50"):
            line_26.append(row)
        else:
            line_27.append(row)

    # Line 28: add column (h), lines 25 through 27 -> carried to line 21.
    line_28 = line_25 + sum(r.deduction_cents for r in line_26 + line_27)
    # Line 29: add column (i), line 26 -> carried to line 7.
    line_29 = sum(r.elected_179_cents for r in line_26)

    line_21 = line_28
    section_bc_total = sum(r.deduction_cents for r in section_b.values()) + sum(
        r.deduction_cents for r in section_c.values()
    )
    # Line 22: add line 12, lines 14 through 17, lines 19 and 20 col (g), line 21.
    line_22 = (
        cents(line_12)
        + line_14
        + 0                                  # line 15 (§168(f)(1)) — not computed
        + inputs.other_depreciation_cents    # line 16
        + line_17
        + section_bc_total
        + line_21
    )

    # Part VI — amortization.
    amort_rows: list[AmortizationRow] = []
    for item in inputs.amortization_current_year:
        # Straight line over the period, from the month amortization begins
        # (the §197/§195 convention: a full month in the month it begins).
        months_this_year = 12 - item.begins.month + 1
        monthly = Decimal(item.amortizable_amount_cents) / Decimal(item.period_months)
        amount = cents(monthly * Decimal(min(months_this_year, item.period_months)))
        amort_rows.append(
            AmortizationRow(
                description=item.description,
                begins=item.begins,
                amortizable_amount_cents=item.amortizable_amount_cents,
                code_section=item.code_section,
                period_months=item.period_months,
                amortization_cents=amount,
            )
        )
    line_43 = inputs.amortization_prior_years_cents
    line_44 = sum(r.amortization_cents for r in amort_rows) + line_43

    return Form4562Lines(
        line_1_max_amount=cents(line_1),
        line_2_total_179_property=cents(line_2),
        line_3_threshold=cents(line_3),
        line_4_reduction=cents(line_4),
        line_5_dollar_limit=cents(line_5),
        line_6_properties=line_6_props,
        line_7_listed_property=cents(line_7),
        line_8_total_elected_cost=cents(line_8),
        line_9_tentative_deduction=cents(line_9),
        line_10_carryover_from_prior=cents(line_10),
        line_11_business_income_limit=cents(line_11),
        line_12_section_179_deduction=cents(line_12),
        line_13_carryover_to_next=cents(line_13),
        line_14_special_allowance=line_14,
        line_15_section_168f1=0,
        line_16_other_depreciation=inputs.other_depreciation_cents,
        line_17_prior_year_macrs=line_17,
        part_iii_section_b=section_b,
        part_iii_section_c=section_c,
        line_21_listed_property=line_21,
        line_22_total=line_22,
        line_25_listed_special_allowance=line_25,
        line_26_over_50_pct=line_26,
        line_27_50_pct_or_less=line_27,
        line_28_total_listed=line_28,
        line_29_listed_179=line_29,
        line_42_amortization=amort_rows,
        line_43_amortization_prior=line_43,
        line_44_amortization_total=line_44,
    )


# Type alias for the optional prior-year config resolver.
ConfigResolver = "callable[[int], DepreciationConfig]"

DepreciationInputs.model_rebuild()

__all__ = [
    "AmortizationItem",
    "AmortizationRow",
    "AssetInput",
    "AssetResult",
    "AssetYear",
    "BonusRule",
    "Convention",
    "DepreciationConfig",
    "DepreciationInputs",
    "DepreciationResult",
    "DepreciationSystem",
    "Form4562Lines",
    "ListedPropertyRow",
    "MacrsClassRow",
    "Method",
    "PUB946_TABLE_ROUNDING_NOTE",
    "Section179Property",
    "cents",
    "compute_depreciation",
    "macrs_schedule",
    "mid_quarter_applies",
]
