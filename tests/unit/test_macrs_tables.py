"""Pin the Pub 946 Appendix A mid-month real-property tables.

The load-bearing invariant is that **every month column sums to exactly
100.000%**. That is what proves the reconstruction from the publication is
correct: a mistyped or dropped entry breaks it immediately.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.tax.engines.macrs_tables import (
    ADS_MID_MONTH_TABLES,
    MID_MONTH_TABLES,
    TABLE_A6_RESIDENTIAL_27_5,
    TABLE_A7A_NONRESIDENTIAL_39,
    table_for,
)

ALL_TABLES = {
    "A-6 (27.5-yr residential rental)": TABLE_A6_RESIDENTIAL_27_5,
    "A-7a (39-yr nonresidential real)": TABLE_A7A_NONRESIDENTIAL_39,
    "A-13 (ADS 30-yr)": ADS_MID_MONTH_TABLES["30"],
    "A-13a (ADS 40-yr)": ADS_MID_MONTH_TABLES["40"],
}


@pytest.mark.parametrize("name", sorted(ALL_TABLES))
def test_every_month_column_sums_to_exactly_100_percent(name: str) -> None:
    """The reconstruction invariant. Nothing else in this file matters if this
    fails — it would mean a table entry is wrong."""
    table = ALL_TABLES[name]
    assert sorted(table) == list(range(1, 13)), f"{name}: must cover months 1-12"
    for month, column in table.items():
        total = sum(column)
        assert total == Decimal("100.000"), (
            f"{name} month {month}: column sums to {total}, not 100.000"
        )
        assert all(v >= 0 for v in column), f"{name} month {month}: negative entry"


def test_table_a7a_january_year_one_is_exactly_2_461_percent() -> None:
    """THE pinned value.

    IRS Pub 946 Appendix A Table A-7a, 39-year nonresidential real property,
    mid-month, January placement, year 1 = 2.461%.

    The IRS rounded the monthly increment (1/12/39 = 0.21368% -> 0.214%) and
    accumulated from the December half-month (0.107%):

        0.107 + 11 x 0.214 = 2.461

    A first-principles computation gives 11.5/12/39 = 2.4573%, which is a
    DIFFERENT NUMBER and is not the published answer.
    """
    assert TABLE_A7A_NONRESIDENTIAL_39[1][0] == Decimal("2.461")
    assert TABLE_A7A_NONRESIDENTIAL_39[1][0] != Decimal("2.4573")


def test_table_a7a_year_one_row_is_the_published_arithmetic_progression() -> None:
    """Every month of the published year-1 row, and the 0.214% step that
    generated it."""
    published = ["2.461", "2.247", "2.033", "1.819", "1.605", "1.391",
                 "1.177", "0.963", "0.749", "0.535", "0.321", "0.107"]
    for month, want in enumerate(published, start=1):
        assert TABLE_A7A_NONRESIDENTIAL_39[month][0] == Decimal(want)
    # Consecutive months differ by exactly the rounded monthly increment.
    for month in range(1, 12):
        step = (
            TABLE_A7A_NONRESIDENTIAL_39[month][0]
            - TABLE_A7A_NONRESIDENTIAL_39[month + 1][0]
        )
        assert step == Decimal("0.214")


def test_table_a7a_middle_years_are_2_564_and_life_is_40_years() -> None:
    column = TABLE_A7A_NONRESIDENTIAL_39[1]
    assert len(column) == 40
    assert all(v == Decimal("2.564") for v in column[1:39])
    assert column[39] == Decimal("0.107")     # year 40 mirrors year 1


def test_table_a6_year_one_row_matches_publication() -> None:
    published = ["3.485", "3.182", "2.879", "2.576", "2.273", "1.970",
                 "1.667", "1.364", "1.061", "0.758", "0.455", "0.152"]
    for month, want in enumerate(published, start=1):
        assert TABLE_A6_RESIDENTIAL_27_5[month][0] == Decimal(want)


def test_table_a6_alternates_3_636_and_3_637_as_published() -> None:
    """27.5-year property cannot use a single flat middle rate: 1/27.5 =
    3.6364%, so the IRS alternates 3.636/3.637 to keep the column at 100."""
    column = TABLE_A6_RESIDENTIAL_27_5[1]
    assert set(column[1:9]) == {Decimal("3.636")}         # years 2-9 flat
    middle = set(column[9:27])
    assert middle == {Decimal("3.636"), Decimal("3.637")}


def test_table_lookup_only_covers_classes_with_a_published_table() -> None:
    assert table_for("27.5") is TABLE_A6_RESIDENTIAL_27_5
    assert table_for("39") is TABLE_A7A_NONRESIDENTIAL_39
    assert table_for("30", ads=True) is ADS_MID_MONTH_TABLES["30"]
    assert table_for("40", ads=True) is ADS_MID_MONTH_TABLES["40"]
    # 31.5-year legacy nonresidential has NO table on file — must be None so the
    # engine raises rather than computing a substitute.
    assert table_for("31.5") is None
    assert table_for("7") is None
    assert set(MID_MONTH_TABLES) == {"27.5", "39"}
