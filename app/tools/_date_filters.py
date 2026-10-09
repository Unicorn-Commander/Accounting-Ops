"""Shared parsing for ISO date filters used by journal-backed MCP tools."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any


class InvalidDateFilter(ValueError):
    """A caller supplied a date filter that cannot be bound to a DATE column."""

    status_code = 400
    code = "invalid_date_filter"

    def __init__(self, field: str, value: Any) -> None:
        self.field = field
        self.value = value
        self.error = {
            "status": self.status_code,
            "code": self.code,
            "field": field,
            "message": (
                f"{field} must be an ISO date or datetime "
                f"(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): {value!r}"
            ),
        }
        super().__init__(self.error["message"])


def parse_iso_date_filter(value: Any, field: str) -> date:
    """Parse a date or full ISO datetime for a journal ``DATE`` predicate.

    Date-only values remain dates. Datetime values are reduced to their
    calendar date because the journal predicate is against ``effective_date``
    (a PostgreSQL DATE), and range endpoints are inclusive at that granularity.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value).date()
            except ValueError:
                pass
    raise InvalidDateFilter(field, value)


__all__ = ["InvalidDateFilter", "parse_iso_date_filter"]
