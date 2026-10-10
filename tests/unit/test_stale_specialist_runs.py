"""Unit coverage for stale specialist-run maintenance without a database."""
from __future__ import annotations

import datetime as dt

import pytest

from scripts.maintain_stale_specialist_runs import maintain_workspace, maintenance_note


NOW = dt.datetime(2026, 10, 9, 15, 30, tzinfo=dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(hours=24)


class _Result:
    def __init__(self, rows=None, *, rowcount: int = 0):
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        if sql.lstrip().startswith("SELECT"):
            return _Result(self.rows)
        return _Result(rowcount=len(self.rows))


@pytest.mark.asyncio
async def test_dry_run_lists_candidates_without_update():
    row = {
        "id": "run-1",
        "workspace_id": "workspace-1",
        "specialist_id": "tax-1120-preparer",
        "model": "claude-sonnet-4-6",
        "started_at": NOW - dt.timedelta(days=3),
    }
    session = _FakeSession([row])

    result = await maintain_workspace(
        session,
        cutoff=CUTOFF,
        now=NOW,
        apply=False,
    )

    assert result["candidates"] == [row]
    assert result["marked"] == 0
    assert len(session.calls) == 1
    assert "status = 'running'" in session.calls[0][0]
    assert session.calls[0][1]["cutoff"] == CUTOFF


@pytest.mark.asyncio
async def test_apply_marks_candidates_with_dated_note():
    row = {
        "id": "run-1",
        "workspace_id": "workspace-1",
        "specialist_id": "tax-1120-preparer",
        "model": "claude-sonnet-4-6",
        "started_at": NOW - dt.timedelta(days=3),
    }
    session = _FakeSession([row])

    result = await maintain_workspace(
        session,
        cutoff=CUTOFF,
        now=NOW,
        apply=True,
    )

    assert result["marked"] == 1
    assert len(session.calls) == 2
    update_sql, params = session.calls[1]
    assert update_sql.lstrip().startswith("UPDATE specialist_run")
    assert "status = 'failed'" in update_sql
    assert params == {
        "cutoff": CUTOFF,
        "finished_at": NOW,
        "note": "marked stale by maintenance 2026-10-09",
    }


def test_maintenance_note_uses_utc_date_value():
    assert maintenance_note(NOW) == "marked stale by maintenance 2026-10-09"
