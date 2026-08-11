"""Unit tests for individual Sentinel check helpers.

These check the pure-logic helpers (not the DB-touching coroutines). The
coroutines themselves are exercised by the integration smoke and by
sentinel_actions tests with mocked sessions.
"""
from __future__ import annotations

import datetime as dt

from agents.sentinel.checks._helpers import (
    days_until,
    estimated_tax_due_dates,
    federal_1120_deadline,
    form_7004_deadline,
    sc1120_deadline,
)


def test_1120_deadline_basic_weekday():
    # 2024-04-15 was a Monday.
    assert federal_1120_deadline(2023) == dt.date(2024, 4, 15)


def test_1120_deadline_sunday_pushes_to_monday():
    # 2018-04-15 was a Sunday → 2018-04-16.
    assert federal_1120_deadline(2017) == dt.date(2018, 4, 16)


def test_1120_deadline_saturday_pushes_to_monday():
    # 2017-04-15 was a Saturday → 2017-04-17.
    assert federal_1120_deadline(2016) == dt.date(2017, 4, 17)


def test_sc1120_mirrors_federal():
    for ty in (2020, 2021, 2022, 2023):
        assert sc1120_deadline(ty) == federal_1120_deadline(ty)


def test_7004_extension_mirrors_1120():
    assert form_7004_deadline(2023) == federal_1120_deadline(2023)


def test_estimated_tax_dates_for_current_year():
    dates = estimated_tax_due_dates(2026)
    assert dates == [
        dt.date(2026, 4, 15),
        dt.date(2026, 6, 15),
        dt.date(2026, 9, 15),
        dt.date(2027, 1, 15),
    ]


def test_days_until_positive_today_zero_past_negative():
    today = dt.date(2026, 5, 30)
    assert days_until(dt.date(2026, 6, 14), today=today) == 15
    assert days_until(today, today=today) == 0
    assert days_until(dt.date(2026, 5, 1), today=today) == -29


def test_check_registry_complete():
    from agents.sentinel.checks import CHECK_REGISTRY
    from agents.sentinel.config import ALL_CHECK_TYPES

    # Every documented check_type must have a registered handler.
    for ct in ALL_CHECK_TYPES:
        assert ct in CHECK_REGISTRY, f"Missing handler for check {ct!r}"
    # No mystery handlers (i.e. CHECK_REGISTRY must not contain spurious keys
    # outside the documented vocabulary).
    assert set(CHECK_REGISTRY.keys()) == set(ALL_CHECK_TYPES)


def test_autonomy_level_enum_values():
    from agents.sentinel.config import AutonomyLevel

    assert {a.value for a in AutonomyLevel} == {
        "off", "watch_only", "draft_and_flag", "fully_autonomous"
    }


def test_check_cadence_documented():
    from agents.sentinel.config import ALL_CHECK_TYPES, CHECK_CADENCE

    for ct in ALL_CHECK_TYPES:
        assert ct in CHECK_CADENCE
        assert CHECK_CADENCE[ct] in ("daily", "weekly", "monthly")
