"""Deterministic South Carolina ST-3 (State Sales & Use Tax Return) engine.

SC Form ST-3 is the periodic State Sales and Use Tax return a retailer files
with the SC Department of Revenue (SCDOR). The face of the return is a short,
fully deterministic arithmetic worksheet:

  * Line 1  — Total gross proceeds of sales, rentals, Use Tax, and withdrawals
              for own use (from Item 3 of the Sales and Use Tax Worksheet).
  * Line 2  — Total allowable deductions (from Item 5 of the worksheet) — e.g.
              out-of-state sales, sales for resale, and other statutory
              exemptions the filer itemizes on the worksheet.
  * Line 3  — Net taxable sales and purchases  = line 1 − line 2.
  * Line 4  — State Sales and Use Tax          = line 3 × 6% (the SC state rate).
  * Line 5  — Additional local tax from the ST-389 (Column B). Local option
              taxes (LOST, capital-projects, transportation, education, etc.)
              VARY BY COUNTY and are computed on the separate ST-389; this
              engine NEVER guesses a local rate. A ``county_local_rate`` may be
              supplied (the combined applicable local rate, as a decimal) and is
              applied to the same net-taxable base purely as a convenience
              estimate that the filer must confirm against the ST-389.
  * Line 6  — Total State and Local Taxes due  = line 4 + line 5.

Lines 7–10 (timely-filing discount, penalty/interest, net tax payable, total
amount due) depend on filing dates and payment timing, so this engine stops at
line 6 — the deterministic *tax* computation — and surfaces line 7+ as filer
inputs on the form face. It NEVER fabricates a figure: an unsupported tax year
RAISES rather than substituting another year's rate, and every assumption it
relied on (especially that the local rate must be confirmed per county against
the ST-389) is surfaced in ``assumptions`` for the human to confirm before they
sign.

The SC state Sales and Use Tax rate is **6%** (S.C. Code Ann. § 12-36-910 et
seq.; printed on the ST-3 face itself — "multiply line 3 x 6%"). Source PDF:
SCDOR Form ST-3 (Rev. 3/31/23), https://dor.sc.gov/forms ("ST3.pdf").

ST-3 amounts carry cents on the form face (each money box has a decimal +
2-place cents column). Unlike the whole-dollar W-4P, this engine therefore works
in **integer cents** and formats to ``"<dollars>.<cents>"`` for the form, so the
state-tax line is exact (a 6% multiply is not whole-dollar clean).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ST3YearConfig:
    """Year-aware statutory constants for the SC ST-3. Rates are decimals."""

    tax_year: int
    # SC state Sales and Use Tax rate (ST-3 line 4 = line 3 × this rate).
    state_rate: str  # exact decimal as a string, e.g. "0.06"


# Verified against the SCDOR ST-3 form face ("multiply line 3 x 6%") and
# S.C. Code Ann. § 12-36-910. The 6% state rate has been stable; local option
# taxes are NOT part of this constant — they vary by county and ride the ST-389.
_YEAR_CONFIGS: dict[int, ST3YearConfig] = {
    2023: ST3YearConfig(2023, "0.06"),
    2024: ST3YearConfig(2024, "0.06"),
    2025: ST3YearConfig(2025, "0.06"),
    2026: ST3YearConfig(2026, "0.06"),
}

SUPPORTED_TAX_YEARS: tuple[int, ...] = tuple(sorted(_YEAR_CONFIGS))


def config_for(tax_year: int) -> ST3YearConfig:
    """Return the statutory constants for ``tax_year``.

    Raises ValueError for an unsupported year — we never substitute another
    year's rate onto a real return (anti-fabrication).
    """
    try:
        return _YEAR_CONFIGS[tax_year]
    except KeyError:
        raise ValueError(
            f"No SC ST-3 statutory constants on file for tax year {tax_year}. "
            f"Supported: {', '.join(map(str, SUPPORTED_TAX_YEARS))}. "
            "Add a verified ST3YearConfig before preparing this year's return."
        ) from None


@dataclass(frozen=True)
class ST3WorksheetInput:
    """Caller-provided ST-3 figures (in integer cents).

    ``net_taxable_cents`` may be supplied directly (e.g. straight off the filer's
    ST-389 worksheet); otherwise it is derived as gross − deductions. A supplied
    value takes precedence and is reconciled against the derived value in
    ``assumptions``. ``county_local_rate`` is the combined applicable local rate
    as a decimal string (e.g. "0.01" for a single 1% local option tax) — it must
    be confirmed per county against the ST-389; when omitted, local tax is left
    at $0 on the face (the filer completes the ST-389 and enters line 5).
    """

    tax_year: int
    gross_proceeds_cents: int = 0          # Worksheet Item 3 -> ST-3 line 1
    deductions_cents: int = 0              # Worksheet Item 5 -> ST-3 line 2
    net_taxable_cents: int | None = None   # Item 6 -> line 3 (else gross-deduct)
    county_local_rate: str | None = None   # combined local rate, decimal string


@dataclass(frozen=True)
class ST3WorksheetResult:
    """Deterministic ST-3 line values (integer cents)."""

    net_taxable_cents: int   # line 3
    state_tax_cents: int     # line 4 = line 3 × state_rate
    local_tax_cents: int     # line 5 (0 unless a county_local_rate was supplied)
    total_tax_cents: int     # line 6 = line 4 + line 5
    state_rate: str
    assumptions: list[str] = field(default_factory=list)


def _money(cents: int) -> str:
    """Format integer cents as a dollar-and-cents string for display."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _round_half_up_cents(numerator: int, rate: str) -> int:
    """Multiply ``numerator`` cents by a decimal ``rate`` string, rounding the
    result to whole cents half-up — exact integer arithmetic, no float drift.

    ``rate`` like "0.06" -> scale 100, factor 6. ``numerator`` (cents) × factor,
    then divide by scale with round-half-up.
    """
    if "." in rate:
        int_part, frac_part = rate.split(".", 1)
    else:
        int_part, frac_part = rate, ""
    scale = 10 ** len(frac_part)
    factor = int(int_part or "0") * scale + int(frac_part or "0")
    # round-half-up division of (numerator * factor) by scale
    num = numerator * factor
    if num >= 0:
        return (num + scale // 2) // scale
    return -((-num + scale // 2) // scale)


def compute_st3_worksheet(inp: ST3WorksheetInput) -> ST3WorksheetResult:
    """Compute ST-3 lines 3, 4, 5, 6 from caller-provided figures.

    Pure function: no I/O, no LLM, no fabrication. Surfaces every assumption it
    relied on in ``assumptions`` so the human can confirm before signing. The
    state rate is statutory (raises on an unsupported year); the local rate is
    NEVER guessed — it is $0 unless a county-confirmed rate is supplied.
    """
    cfg = config_for(inp.tax_year)  # raises on an unsupported year
    assumptions: list[str] = []

    gross = inp.gross_proceeds_cents
    deductions = inp.deductions_cents
    derived_net = gross - deductions

    if inp.net_taxable_cents is not None:
        net = inp.net_taxable_cents
        if net != derived_net and (gross or deductions):
            assumptions.append(
                f"Supplied net taxable {_money(net)} does not equal gross "
                f"{_money(gross)} − deductions {_money(deductions)} = "
                f"{_money(derived_net)}; used the supplied figure. Confirm the "
                "Sales and Use Tax Worksheet (Items 1-6) ties out."
            )
    else:
        net = derived_net
        assumptions.append(
            f"Net taxable (line 3) computed as gross proceeds {_money(gross)} "
            f"− deductions {_money(deductions)} = {_money(net)} "
            "(ST-3 worksheet Item 3 − Item 5)."
        )

    if net < 0:
        assumptions.append(
            "Net taxable is negative; the ST-3 instructs DO NOT REPORT NEGATIVE "
            "AMOUNTS — this likely means an over-stated deduction. Review before "
            "filing (a credit/refund is claimed on the ST-14, not a negative ST-3)."
        )

    # Line 4 — State Sales and Use Tax = line 3 × 6%.
    state_tax = _round_half_up_cents(net, cfg.state_rate)
    assumptions.append(
        f"State Sales and Use Tax (line 4) = net taxable {_money(net)} × "
        f"{cfg.state_rate} (SC 6% state rate, TY{cfg.tax_year}) = "
        f"{_money(state_tax)}."
    )

    # Line 5 — Additional local tax from the ST-389. Local option taxes vary by
    # county and are computed on the ST-389; never guessed here.
    if inp.county_local_rate is not None:
        local_tax = _round_half_up_cents(net, inp.county_local_rate)
        assumptions.append(
            f"Local tax (line 5) ESTIMATED as net taxable {_money(net)} × "
            f"{inp.county_local_rate} (caller-supplied combined county rate). "
            "Local option taxes VARY BY COUNTY and must be computed and attached "
            "on the ST-389 — confirm this rate and the ST-389 Column B total per "
            "county before filing; the rate was not validated by this engine."
        )
    else:
        local_tax = 0
        assumptions.append(
            "Local tax (line 5) left at $0 — no county local rate was supplied. "
            "Local option taxes (LOST, capital-projects, transportation, "
            "education, etc.) VARY BY COUNTY and are reported on the separate "
            "ST-389. If any local tax applies to these sales, complete and "
            "attach the ST-389 and enter its Column B total on line 5."
        )

    total_tax = state_tax + local_tax

    return ST3WorksheetResult(
        net_taxable_cents=net,
        state_tax_cents=state_tax,
        local_tax_cents=local_tax,
        total_tax_cents=total_tax,
        state_rate=cfg.state_rate,
        assumptions=assumptions,
    )
