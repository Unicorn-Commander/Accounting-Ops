"""Business-calendar helpers for Accounting-Ops.

Servers and databases remain on UTC.  These helpers are for date-only
accounting, tax, and scheduling boundaries that belong to the business zone.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BUSINESS_TIMEZONE = "America/New_York"
_BUSINESS_ZONE = ZoneInfo(BUSINESS_TIMEZONE)


def business_today(now: datetime | None = None) -> date:
    """Return the calendar date in the business timezone.

    ``now`` is injectable for deterministic boundary tests.  Naive values are
    interpreted as business-local time; aware values are converted from their
    actual instant.
    """
    current = now or datetime.now(tz=_BUSINESS_ZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_BUSINESS_ZONE)
    else:
        current = current.astimezone(_BUSINESS_ZONE)
    return current.date()


def business_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return a business-local calendar day's half-open UTC bounds.

    The returned values are aware UTC instants.  Using the next local midnight
    as the exclusive end preserves 23-hour and 25-hour DST days.
    """
    if isinstance(day, datetime):
        day = day.date()
    local_start = datetime.combine(day, time.min, tzinfo=_BUSINESS_ZONE)
    local_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=_BUSINESS_ZONE)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


__all__ = ("BUSINESS_TIMEZONE", "business_day_bounds", "business_today")
