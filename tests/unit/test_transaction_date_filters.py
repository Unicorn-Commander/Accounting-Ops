from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from app.tools import transactions
from app.tools import journal_tools, tax_get_period_journal
from app.tools._date_filters import InvalidDateFilter, parse_iso_date_filter


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-05-01", date(2026, 5, 1)),
        ("2026-05-01T12:34:56", date(2026, 5, 1)),
        ("2026-05-01T12:34:56+00:00", date(2026, 5, 1)),
    ],
)
def test_parse_iso_date_filter_accepts_date_and_datetime(raw, expected):
    assert parse_iso_date_filter(raw, "start_date") == expected


def test_parse_iso_date_filter_accepts_date_objects():
    assert parse_iso_date_filter(date(2026, 5, 1), "start_date") == date(2026, 5, 1)
    assert parse_iso_date_filter(
        datetime(2026, 5, 1, 12, 34, tzinfo=timezone.utc), "start_date"
    ) == date(2026, 5, 1)


def test_parse_iso_date_filter_garbage_has_a_400_style_error_object():
    with pytest.raises(InvalidDateFilter) as exc_info:
        parse_iso_date_filter("not-a-date", "end_date")

    assert exc_info.value.error == {
        "status": 400,
        "code": "invalid_date_filter",
        "field": "end_date",
        "message": (
            "end_date must be an ISO date or datetime "
            "(YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS): 'not-a-date'"
        ),
    }


class _ExecuteResult:
    def mappings(self):
        return self

    def all(self):
        return []

    def scalar_one(self):
        return 0

    def scalars(self):
        return self


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return _ExecuteResult()


@pytest.mark.asyncio
async def test_list_transactions_binds_date_objects_and_inclusive_end(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    session = _FakeSession()

    async def fake_authorize(*_args, **_kwargs):
        return None

    @asynccontextmanager
    async def fake_entity_session(*_args, **_kwargs):
        yield session, entity_id

    monkeypatch.setattr(transactions, "authorize_workspace", fake_authorize)
    monkeypatch.setattr(transactions, "entity_session", fake_entity_session)

    rows = await transactions.list_transactions(
        workspace_id,
        entity_id,
        {
            "start_date": "2026-05-01T00:00:00Z",
            "end_date": "2026-07-09",
            "limit": 200,
        },
    )

    assert rows == []
    sql, params = session.calls[0]
    assert "effective_date >= :start_date" in sql
    assert "effective_date <= :end_date" in sql
    assert params["start_date"] == date(2026, 5, 1)
    assert params["end_date"] == date(2026, 7, 9)
    assert type(params["start_date"]) is date
    assert type(params["end_date"]) is date


@pytest.mark.asyncio
async def test_list_transactions_rejects_invalid_date_before_query(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    session = _FakeSession()

    async def fake_authorize(*_args, **_kwargs):
        return None

    @asynccontextmanager
    async def fake_entity_session(*_args, **_kwargs):
        yield session, entity_id

    monkeypatch.setattr(transactions, "authorize_workspace", fake_authorize)
    monkeypatch.setattr(transactions, "entity_session", fake_entity_session)

    with pytest.raises(InvalidDateFilter, match="start_date must be"):
        await transactions.list_transactions(
            workspace_id,
            entity_id,
            {"start_date": "not-a-date"},
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_get_period_journal_binds_date_objects(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    session = _FakeSession()

    async def fake_authorize(*_args, **_kwargs):
        return None

    @asynccontextmanager
    async def fake_entity_session(*_args, **_kwargs):
        yield session, entity_id

    monkeypatch.setattr(tax_get_period_journal, "authorize_workspace", fake_authorize)
    monkeypatch.setattr(tax_get_period_journal, "entity_session", fake_entity_session)

    result = await tax_get_period_journal.get_period_journal(
        workspace_id,
        entity_id,
        "2026-05-01T00:00:00Z",
        "2026-07-09T23:59:59Z",
        limit=10,
    )

    assert result["entries"] == []
    _sql, params = session.calls[0]
    assert params["start_date"] == date(2026, 5, 1)
    assert params["end_date"] == date(2026, 7, 9)
    assert type(params["start_date"]) is date
    assert type(params["end_date"]) is date


@pytest.mark.asyncio
async def test_list_journal_entries_binds_date_objects(monkeypatch):
    workspace_id = uuid4()
    session = _FakeSession()

    async def fake_authorize(*_args, **_kwargs):
        return object()

    @asynccontextmanager
    async def fake_workspace_session(*_args, **_kwargs):
        yield session

    monkeypatch.setattr(journal_tools, "authorize_workspace", fake_authorize)
    monkeypatch.setattr(journal_tools, "assert_entity_in_scope", lambda *_args: None)
    monkeypatch.setattr(journal_tools, "workspace_session", fake_workspace_session)

    result = await journal_tools.list_journal_entries(
        workspace_id,
        start_date="2026-05-01T00:00:00Z",
        end_date="2026-07-09",
    )

    assert result == {"entries": [], "total": 0, "categories": []}
    _sql, params = session.calls[0]
    assert params["start_date"] == date(2026, 5, 1)
    assert params["end_date"] == date(2026, 7, 9)
    assert type(params["start_date"]) is date
    assert type(params["end_date"]) is date
