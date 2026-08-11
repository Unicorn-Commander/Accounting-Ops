"""Shared helpers for Sentinel check functions."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class EntitySummary:
    """A snapshot of one entity used by checks (keeps each check terse)."""
    id: UUID
    slug: str
    display_name: str
    kind: str
    country_code: str
    fiscal_year_start_month: int
    incorporated_at: dt.date | None
    status: str


async def list_active_entities(session: AsyncSession) -> list[EntitySummary]:
    """Fetch all active entities in the current workspace schema."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text, slug, display_name, kind::text,
                       country_code, fiscal_year_start_month,
                       incorporated_at, status::text
                FROM entity
                WHERE status = 'active'
                ORDER BY display_name
                """
            )
        )
    ).mappings().all()
    out: list[EntitySummary] = []
    for r in rows:
        inc = r["incorporated_at"]
        if inc is not None and not isinstance(inc, dt.date):
            try:
                inc = dt.date.fromisoformat(str(inc)[:10])
            except ValueError:
                inc = None
        out.append(
            EntitySummary(
                id=UUID(r["id"]),
                slug=r["slug"],
                display_name=r["display_name"],
                kind=r["kind"],
                country_code=(r["country_code"] or "US").upper(),
                fiscal_year_start_month=int(r["fiscal_year_start_month"] or 1),
                incorporated_at=inc,
                status=r["status"],
            )
        )
    return out


def _roll_off_weekend(d: dt.date) -> dt.date:
    if d.weekday() == 5:
        d += dt.timedelta(days=2)
    elif d.weekday() == 6:
        d += dt.timedelta(days=1)
    return d


# Calendar-year federal return due dates, by entity kind. Flow-throughs
# (1120-S / 1065) are due a full month before 1120 and 1040 filers.
RETURN_FORM_BY_KIND = {
    "c_corp": "1120",
    "s_corp": "1120-S",
    "partnership": "1065",
    "llc": "1065",
    "sole_proprietorship": "1040",
    "personal": "1040",
}

_RETURN_DUE_MONTH = {"1120": 4, "1120-S": 3, "1065": 3, "1040": 4}


def return_form_for_kind(kind: str) -> str | None:
    """Federal return a given entity kind files, or None if we do not prepare one."""
    return RETURN_FORM_BY_KIND.get((kind or "").lower())


def federal_return_deadline(tax_year: int, kind: str) -> dt.date | None:
    """Federal return deadline for a calendar-year filer of this entity kind.

    Returns None for kinds we do not prepare a return for (nonprofit, trust,
    estate, other) rather than implying a date we cannot stand behind.
    """
    form = return_form_for_kind(kind)
    if form is None:
        return None
    return _roll_off_weekend(dt.date(tax_year + 1, _RETURN_DUE_MONTH[form], 15))


def federal_1120_deadline(tax_year: int) -> dt.date:
    """Federal Form 1120 deadline: April 15 of (tax_year + 1), advance off weekends."""
    return _roll_off_weekend(dt.date(tax_year + 1, 4, 15))


def sc1120_deadline(tax_year: int) -> dt.date:
    """SC1120 deadline mirrors federal 1120 for now."""
    return federal_1120_deadline(tax_year)


def estimated_tax_due_dates(year: int) -> list[dt.date]:
    """Federal estimated tax due dates: 4/15, 6/15, 9/15, 1/15+1."""
    return [
        dt.date(year, 4, 15),
        dt.date(year, 6, 15),
        dt.date(year, 9, 15),
        dt.date(year + 1, 1, 15),
    ]


def form_7004_deadline(tax_year: int, kind: str = "c_corp") -> dt.date:
    """Form 7004 (automatic extension) — due on the return's own original due date."""
    return federal_return_deadline(tax_year, kind) or federal_1120_deadline(tax_year)


def days_until(target: dt.date, today: dt.date | None = None) -> int:
    today = today or dt.date.today()
    return (target - today).days


__all__ = [
    "EntitySummary",
    "days_until",
    "estimated_tax_due_dates",
    "federal_1120_deadline",
    "federal_return_deadline",
    "form_7004_deadline",
    "list_active_entities",
    "return_form_for_kind",
    "sc1120_deadline",
    "RETURN_FORM_BY_KIND",
]
