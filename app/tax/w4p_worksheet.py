"""Deterministic Form W-4P withholding-worksheet engine.

Form W-4P (Withholding Certificate for Periodic Pension or Annuity Payments) is
given to the PENSION PAYER, not the IRS. The payer computes the actual
withholding from the taxpayer's declared elections using IRS Pub 15-T. This
engine therefore computes ONLY the two deterministic sub-totals a preparer fills
on the face of the form:

  * Step 3 — Credit for dependents:  qualifying_children * CTC_per_child
             + other_dependents * $500, claimed only when income is at/under the
             W-4P Step 3 threshold (above it the phase-out is handled at filing
             on Schedule 8812, so the conservative face value is $0).
  * Step 4(b) — Deductions Worksheet result: estimated deductions (itemized +
             above-the-line adjustments) over the standard deduction, floored at
             zero.

Everything else on the W-4P (name, SSN, filing status, Step 2 other income,
4(a) other income, 4(c) extra withholding) is a TAXPAYER DECLARATION transcribed
verbatim. This engine NEVER computes a withholding amount and NEVER fabricates a
figure: an unsupported tax year RAISES rather than guessing, and every estimate
it relies on is surfaced in ``assumptions``.

Year constants are statutory and verified against irs.gov; see
``~/Documents/w4p-field-and-worksheet-reference.md`` for the citations and the
AcroForm field dump. W-4P amounts are whole dollars (the form rounds to whole
dollars), so this engine uses whole-dollar integers throughout.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class W4PFilingStatus(str, enum.Enum):
    """The three filing-status checkboxes on the W-4P face (Step 1(b))."""

    SINGLE_OR_MFS = "single_or_mfs"          # "Single or Married filing separately"
    MFJ_OR_QSS = "mfj_or_qss"                # "Married filing jointly or Qualifying surviving spouse"
    HEAD_OF_HOUSEHOLD = "head_of_household"  # "Head of household"


@dataclass(frozen=True)
class W4PYearConfig:
    """Year-aware statutory constants. Whole dollars."""

    tax_year: int
    std_single_or_mfs: int
    std_mfj_or_qss: int
    std_hoh: int
    ctc_per_child: int        # qualifying child under 17 (Step 3(a))
    odc_per_other_dep: int    # credit for other dependents (Step 3(b))
    ctc_threshold_single: int  # Step 3 income threshold (Single/MFS/HoH)
    ctc_threshold_mfj: int     # Step 3 income threshold (MFJ/QSS)


# Verified against IRS Rev. Proc. 2024-40 (TY2025) + Rev. Proc. 2025-32 (TY2026)
# and OBBBA (enacted 2025-07-04, retroactive to TY2025) — same authoritative
# source as app/tax/engines/configs_1040.py (kept in sync via the cross-check
# test). 2025 standard deductions reflect OBBBA: 15,750 / 31,500 / 23,625 (NOT
# the pre-OBBBA 15,000 / 30,000 / 22,500). 2024 predates OBBBA (unchanged).
# CTC: $2,000/child for 2024-2025, raised to $2,200 for 2026.
_YEAR_CONFIGS: dict[int, W4PYearConfig] = {
    2024: W4PYearConfig(2024, 14600, 29200, 21900, 2000, 500, 200000, 400000),
    2025: W4PYearConfig(2025, 15750, 31500, 23625, 2000, 500, 200000, 400000),
    2026: W4PYearConfig(2026, 16100, 32200, 24150, 2200, 500, 200000, 400000),
}

SUPPORTED_TAX_YEARS: tuple[int, ...] = tuple(sorted(_YEAR_CONFIGS))


def config_for(tax_year: int) -> W4PYearConfig:
    """Return the statutory constants for ``tax_year``.

    Raises ValueError for an unsupported year — we never substitute another
    year's constants onto a real form (anti-fabrication).
    """
    try:
        return _YEAR_CONFIGS[tax_year]
    except KeyError:
        raise ValueError(
            f"No W-4P statutory constants on file for tax year {tax_year}. "
            f"Supported: {', '.join(map(str, SUPPORTED_TAX_YEARS))}. "
            "Add a verified W4PYearConfig before preparing this year's form."
        ) from None


@dataclass(frozen=True)
class W4PWorksheetInput:
    tax_year: int
    filing_status: W4PFilingStatus
    qualifying_children: int = 0          # under 17, for Step 3(a)
    other_dependents: int = 0             # for Step 3(b)
    other_credits: int = 0               # Step 3(c) — user-entered (e.g. education)
    estimated_annual_income: int | None = None  # whole dollars; gates Step 3
    # Step 4(b) Deductions Worksheet inputs (whole-dollar user estimates):
    estimated_itemized_deductions: int = 0
    estimated_adjustments: int = 0        # above-the-line (student loan int, IRA, …)


@dataclass(frozen=True)
class W4PWorksheetResult:
    step3a_child_credit: int      # 3(a)
    step3b_other_dep_credit: int  # 3(b)
    step3c_other_credits: int     # 3(c)
    step3_total: int              # Step 3 total  -> face line f1_12
    step4b_deductions: int        # Step 4(b)     -> face line f1_14
    standard_deduction: int
    assumptions: list[str] = field(default_factory=list)


def compute_w4p_worksheet(inp: W4PWorksheetInput) -> W4PWorksheetResult:
    """Compute the deterministic Step 3 + Step 4(b) sub-totals for a W-4P.

    Pure function: no I/O, no LLM, no fabrication. Surfaces every estimate it
    relied on in ``assumptions`` so the human can confirm before signing.
    """
    cfg = config_for(inp.tax_year)  # raises on an unsupported year
    assumptions: list[str] = []

    if inp.filing_status == W4PFilingStatus.MFJ_OR_QSS:
        std = cfg.std_mfj_or_qss
        threshold = cfg.ctc_threshold_mfj
    elif inp.filing_status == W4PFilingStatus.HEAD_OF_HOUSEHOLD:
        std = cfg.std_hoh
        threshold = cfg.ctc_threshold_single
    else:
        std = cfg.std_single_or_mfs
        threshold = cfg.ctc_threshold_single

    children = max(0, inp.qualifying_children)
    others = max(0, inp.other_dependents)
    other_credits = max(0, inp.other_credits)

    # --- Step 3: dependents credit -----------------------------------------
    over_threshold = (
        inp.estimated_annual_income is not None
        and inp.estimated_annual_income > threshold
    )
    if over_threshold:
        child_credit = 0
        other_dep_credit = 0
        if children or others:
            assumptions.append(
                f"Estimated income ${inp.estimated_annual_income:,} exceeds the "
                f"${threshold:,} W-4P Step 3 threshold for this filing status; "
                "the dependent credit phases out and is reconciled at filing "
                "(Schedule 8812), so Step 3 is left at $0 on the certificate face."
            )
    else:
        child_credit = children * cfg.ctc_per_child
        other_dep_credit = others * cfg.odc_per_other_dep
        if inp.estimated_annual_income is None and (children or others):
            assumptions.append(
                "No estimated income was provided; assumed at/under the Step 3 "
                f"threshold (${threshold:,}). Confirm before signing — above it "
                "the dependent credit is reduced."
            )

    step3_total = child_credit + other_dep_credit + other_credits

    # --- Step 4(b): Deductions Worksheet result ----------------------------
    estimated = (
        max(0, inp.estimated_itemized_deductions)
        + max(0, inp.estimated_adjustments)
    )
    step4b = max(0, estimated - std)
    if estimated:
        assumptions.append(
            f"Step 4(b) = estimated deductions ${estimated:,} − standard "
            f"deduction ${std:,} ({inp.filing_status.value}, TY{cfg.tax_year}) "
            f"= ${step4b:,}."
        )

    return W4PWorksheetResult(
        step3a_child_credit=child_credit,
        step3b_other_dep_credit=other_dep_credit,
        step3c_other_credits=other_credits,
        step3_total=step3_total,
        step4b_deductions=step4b,
        standard_deduction=std,
        assumptions=assumptions,
    )
