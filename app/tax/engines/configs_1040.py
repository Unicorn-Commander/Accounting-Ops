"""Year-aware Form 1040 TaxConfig factory — authoritative federal constants.

Extracted VERBATIM from Tax-Planning-Ops' cited bracket seed
(``backend/alembic/versions/0002_phase_1_tax_config.py``), which is verified
against **IRS Rev. Proc. 2024-40 (TY2025)** and **Rev. Proc. 2025-32 (TY2026,
first year reflecting OBBBA)**. 2025 standard deductions reflect OBBBA (enacted
2025-07-04, retroactive to TY2025): 15,750 / 31,500 / 23,625. MFS = MFJ/2 on
brackets; QSS = MFJ.

Pure code (no DB) so AO can compute a return deterministically and offline. An
unsupported year RAISES — we never substitute another year's constants onto a
real return. Re-verify these against the current Rev. Proc. before each filing
season and keep in sync with TPO's seed.

All money is integer CENTS.
"""
from __future__ import annotations

from decimal import Decimal

from app.tax.engines.form1040 import (
    AuxiliaryConfig,
    FilingStatus,
    RateBracket,
    TaxConfig,
)

# Standard deductions (cents). 2025 = OBBBA; 2026 = Rev. Proc. 2025-32.
_STD_DEDUCTION: dict[int, dict[str, int]] = {
    2025: {"single": 1575000, "mfj": 3150000, "mfs": 1575000, "hoh": 2362500, "qss": 3150000},
    2026: {"single": 1610000, "mfj": 3220000, "mfs": 1610000, "hoh": 2415000, "qss": 3220000},
}

# Ordinary-rate brackets (cents; lower inclusive, upper exclusive; open-ended top).
_ORDINARY: dict[int, dict[str, list[tuple[int, int | None, str]]]] = {
    2025: {
        "single": [(0, 1192500, "0.1000"), (1192500, 4847500, "0.1200"), (4847500, 10335000, "0.2200"), (10335000, 19730000, "0.2400"), (19730000, 25052500, "0.3200"), (25052500, 62635000, "0.3500"), (62635000, None, "0.3700")],
        "mfj":    [(0, 2385000, "0.1000"), (2385000, 9695000, "0.1200"), (9695000, 20670000, "0.2200"), (20670000, 39460000, "0.2400"), (39460000, 50105000, "0.3200"), (50105000, 75160000, "0.3500"), (75160000, None, "0.3700")],
        "mfs":    [(0, 1192500, "0.1000"), (1192500, 4847500, "0.1200"), (4847500, 10335000, "0.2200"), (10335000, 19730000, "0.2400"), (19730000, 25052500, "0.3200"), (25052500, 37580000, "0.3500"), (37580000, None, "0.3700")],
        "hoh":    [(0, 1700000, "0.1000"), (1700000, 6485000, "0.1200"), (6485000, 10335000, "0.2200"), (10335000, 19730000, "0.2400"), (19730000, 25050000, "0.3200"), (25050000, 62635000, "0.3500"), (62635000, None, "0.3700")],
        "qss":    [(0, 2385000, "0.1000"), (2385000, 9695000, "0.1200"), (9695000, 20670000, "0.2200"), (20670000, 39460000, "0.2400"), (39460000, 50105000, "0.3200"), (50105000, 75160000, "0.3500"), (75160000, None, "0.3700")],
    },
    2026: {
        "single": [(0, 1240000, "0.1000"), (1240000, 5040000, "0.1200"), (5040000, 10570000, "0.2200"), (10570000, 20177500, "0.2400"), (20177500, 25622500, "0.3200"), (25622500, 64060000, "0.3500"), (64060000, None, "0.3700")],
        "mfj":    [(0, 2480000, "0.1000"), (2480000, 10080000, "0.1200"), (10080000, 21140000, "0.2200"), (21140000, 40355000, "0.2400"), (40355000, 51245000, "0.3200"), (51245000, 76870000, "0.3500"), (76870000, None, "0.3700")],
        "mfs":    [(0, 1240000, "0.1000"), (1240000, 5040000, "0.1200"), (5040000, 10570000, "0.2200"), (10570000, 20177500, "0.2400"), (20177500, 25622500, "0.3200"), (25622500, 38435000, "0.3500"), (38435000, None, "0.3700")],
        "hoh":    [(0, 1770000, "0.1000"), (1770000, 6745000, "0.1200"), (6745000, 10570000, "0.2200"), (10570000, 20177500, "0.2400"), (20177500, 25620000, "0.3200"), (25620000, 64060000, "0.3500"), (64060000, None, "0.3700")],
        "qss":    [(0, 2480000, "0.1000"), (2480000, 10080000, "0.1200"), (10080000, 21140000, "0.2200"), (21140000, 40355000, "0.2400"), (40355000, 51245000, "0.3200"), (51245000, 76870000, "0.3500"), (76870000, None, "0.3700")],
    },
}

# LTCG / qualified-dividend 0/15/20 breakpoints (cents).
_LTCG: dict[int, dict[str, list[tuple[int, int | None, str]]]] = {
    2025: {
        "single": [(0, 4835000, "0.0000"), (4835000, 53340000, "0.1500"), (53340000, None, "0.2000")],
        "hoh":    [(0, 6475000, "0.0000"), (6475000, 56670000, "0.1500"), (56670000, None, "0.2000")],
        "mfj":    [(0, 9670000, "0.0000"), (9670000, 60005000, "0.1500"), (60005000, None, "0.2000")],
        "qss":    [(0, 9670000, "0.0000"), (9670000, 60005000, "0.1500"), (60005000, None, "0.2000")],
        "mfs":    [(0, 4835000, "0.0000"), (4835000, 30002500, "0.1500"), (30002500, None, "0.2000")],
    },
    2026: {
        "single": [(0, 4945000, "0.0000"), (4945000, 54550000, "0.1500"), (54550000, None, "0.2000")],
        "hoh":    [(0, 6620000, "0.0000"), (6620000, 57960000, "0.1500"), (57960000, None, "0.2000")],
        "mfj":    [(0, 9890000, "0.0000"), (9890000, 61370000, "0.1500"), (61370000, None, "0.2000")],
        "qss":    [(0, 9890000, "0.0000"), (9890000, 61370000, "0.1500"), (61370000, None, "0.2000")],
        "mfs":    [(0, 4945000, "0.0000"), (4945000, 30685000, "0.1500"), (30685000, None, "0.2000")],
    },
}

# Statutory (not inflation-indexed) NIIT + Additional-Medicare thresholds (cents).
_AUX_THRESHOLD = {"single": 20000000, "hoh": 20000000, "mfj": 25000000, "qss": 25000000, "mfs": 12500000}

SUPPORTED_TAX_YEARS_1040: tuple[int, ...] = (2025, 2026)


def _brackets(rows: list[tuple[int, int | None, str]]) -> list[RateBracket]:
    return [RateBracket(lower_bound_cents=lo, upper_bound_cents=hi, rate=Decimal(r)) for lo, hi, r in rows]


def standard_deduction_for(year: int, filing_status: str) -> int:
    """Standard deduction in cents. Raises on an unsupported year/status."""
    try:
        return _STD_DEDUCTION[year][filing_status]
    except KeyError:
        raise ValueError(
            f"No standard deduction on file for {filing_status} TY{year} "
            f"(supported: {SUPPORTED_TAX_YEARS_1040}). Verify the current Rev. Proc."
        ) from None


def tax_config_for(year: int, filing_status: FilingStatus) -> TaxConfig:
    """Build the authoritative federal TaxConfig for a year + filing status.

    Raises ValueError for an unsupported year/status — never guesses constants.
    """
    if year not in _ORDINARY or filing_status not in _ORDINARY[year]:
        raise ValueError(
            f"No 1040 tax config on file for {filing_status} TY{year} "
            f"(supported years: {SUPPORTED_TAX_YEARS_1040}). Add a verified, cited "
            "bracket set before preparing this return."
        )
    threshold = _AUX_THRESHOLD[filing_status]
    aux = AuxiliaryConfig(
        ltcg_brackets=_brackets(_LTCG[year][filing_status]),
        niit_threshold_cents=threshold,
        niit_rate=Decimal("0.0380"),
        additional_medicare_threshold_cents=threshold,
        additional_medicare_rate=Decimal("0.0090"),
        capital_loss_limit_cents=150000 if filing_status == "mfs" else 300000,
        qbi_limit_rate=Decimal("0.20"),
    )
    return TaxConfig(
        tax_year=year,
        filing_status=filing_status,
        standard_deduction_cents=standard_deduction_for(year, filing_status),
        ordinary_brackets=_brackets(_ORDINARY[year][filing_status]),
        auxiliary=aux,
    )
