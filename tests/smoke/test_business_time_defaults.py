"""Regression tests for America/New_York business-date defaults."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from decimal import Decimal

from agents.sentinel.checks._helpers import EntitySummary


def test_business_today_and_fall_back_day_bounds_are_dst_aware():
    from app.core.business_time import (
        BUSINESS_TIMEZONE,
        business_day_bounds,
        business_today,
    )

    assert BUSINESS_TIMEZONE == "America/New_York"
    assert business_today(dt.datetime(2027, 1, 1, 2, tzinfo=dt.timezone.utc)) == dt.date(2026, 12, 31)

    start, end = business_day_bounds(dt.date(2026, 11, 1))
    assert start == dt.datetime(2026, 11, 1, 4, tzinfo=dt.timezone.utc)
    assert end == dt.datetime(2026, 11, 2, 5, tzinfo=dt.timezone.utc)
    assert end - start == dt.timedelta(hours=25)


def test_report_relative_and_as_of_defaults_use_business_date(monkeypatch):
    from app.api import reports

    expected = dt.date(2026, 10, 15)
    monkeypatch.setattr(reports, "business_today", lambda: expected)

    for period in ("mtd", "qtd", "ytd"):
        assert reports._parse_period(period)[1] == expected
    assert reports._default_as_of(None) == expected
    assert reports._default_as_of("2026-10-01") == dt.date(2026, 10, 1)


def test_cash_history_default_uses_business_date(monkeypatch):
    from app.api import cash_history

    expected = dt.date(2026, 10, 15)
    monkeypatch.setattr(cash_history, "business_today", lambda: expected)
    assert cash_history._default_as_of() == expected


def test_tax_status_missing_or_invalid_as_of_uses_business_date(monkeypatch):
    from app.api import tax_status

    expected = dt.date(2026, 12, 31)
    monkeypatch.setattr(tax_status, "business_today", lambda: expected)
    assert tax_status._as_of_date(None) == expected
    assert tax_status._as_of_date("not-a-date") == expected
    assert tax_status._as_of_date("2026-12-30") == dt.date(2026, 12, 30)


@pytest.mark.asyncio
async def test_filing_deadlines_uses_business_date_for_tax_windows(monkeypatch):
    from agents.sentinel.checks import filing_deadlines

    today = dt.date(2026, 12, 31)
    entity = EntitySummary(
        id=uuid4(),
        slug="acme",
        display_name="Acme",
        kind="c_corp",
        country_code="US",
        fiscal_year_start_month=1,
        incorporated_at=dt.date(2020, 1, 1),
        status="active",
    )
    flagged: list[dict] = []

    async def fake_flag(_ctx, _entity, **kwargs):
        flagged.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(filing_deadlines, "business_today", lambda: today)
    monkeypatch.setattr(
        filing_deadlines,
        "list_active_entities",
        AsyncMock(return_value=[entity]),
    )
    monkeypatch.setattr(filing_deadlines, "_flag_deadline", fake_flag)

    result = await filing_deadlines.run(SimpleNamespace(session=object()))

    assert result["actions"] == len(flagged)
    assert any(
        item["form"] == "estimated-tax" and item["due"] == dt.date(2027, 1, 15)
        for item in flagged
    )


@pytest.mark.asyncio
async def test_filing_deadline_dedupe_starts_at_business_midnight():
    from agents.sentinel.checks import filing_deadlines

    class _Result:
        def first(self):
            return None

    class _Session:
        def __init__(self):
            self.params = None

        async def execute(self, _statement, params):
            self.params = params
            return _Result()

    session = _Session()
    ctx = SimpleNamespace(session=session)
    await filing_deadlines._existing_memo_today(
        ctx,
        uuid4(),
        "topic",
        business_date=dt.date(2026, 11, 1),
    )

    assert session.params["business_day_start"] == dt.datetime(
        2026, 11, 1, 4, tzinfo=dt.timezone.utc
    )


@pytest.mark.asyncio
async def test_cash_runway_uses_business_date_in_sql_and_month_key(monkeypatch):
    from agents.sentinel.checks import cash_runway

    class _Result:
        def first(self):
            return SimpleNamespace(net=-90)

    class _Session:
        def __init__(self):
            self.statement = ""

        async def execute(self, statement, _params):
            self.statement = str(statement)
            return _Result()

    session = _Session()
    await cash_runway._burn_rate_monthly(session, "entity-id")
    assert "(now() AT TIME ZONE 'America/New_York')::date" in session.statement
    assert "current_date" not in session.statement.lower()

    today = dt.date(2026, 10, 31)
    entity = EntitySummary(
        id=uuid4(),
        slug="acme",
        display_name="Acme",
        kind="c_corp",
        country_code="US",
        fiscal_year_start_month=1,
        incorporated_at=dt.date(2020, 1, 1),
        status="active",
    )
    actions: list[dict] = []

    async def fake_action(_ctx, **kwargs):
        actions.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cash_runway, "business_today", lambda: today)
    monkeypatch.setattr(cash_runway, "list_active_entities", AsyncMock(return_value=[entity]))
    monkeypatch.setattr(cash_runway, "_cash_balance", AsyncMock(return_value=Decimal("0")))
    monkeypatch.setattr(cash_runway, "_burn_rate_monthly", AsyncMock(return_value=Decimal("-100")))
    monkeypatch.setattr(cash_runway, "execute_action", fake_action)

    await cash_runway.run(SimpleNamespace(session=object()))

    assert actions[0]["payload"]["topic"] == "sentinel-runway-acme-2026-10"
    assert actions[0]["payload"]["tax_year"] == 2026
