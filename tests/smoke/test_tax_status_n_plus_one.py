"""Audit 2026-05-31 Bug 12 — tax-status grouped-query benchmark.

Pre-fix: ``/api/workspaces/{wid}/tax-status`` ran THREE awaited queries per
(entity × year): _queue_summary, _approval_summary, _last_specialist_run. At
50 entities × 5 years that's 750 sequential round-trips, which the audit
called the first dashboard pain point.

Post-fix: ``_bulk_queue_summary``, ``_bulk_approval_summary``, and
``_bulk_last_specialist_run`` each run ONE SQL pass over the full entity-id
× year cartesian product and return a dict keyed (entity_id, tax_year). Total
DB round-trips per request: 3 (independent of N or Y).

This test counts session.execute() invocations against a fake session to prove
the grouped-query contract without needing a live database. A separate live
benchmark in the integration tier can measure wall-clock latency.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

import app.api.tax_status as tax_status_mod


class _CountingFakeSession:
    """Session shim that counts execute() calls + serves canned shapes for
    the grouped-query helpers. The exact returned data doesn't matter — only
    the count of execute() calls.
    """

    def __init__(self):
        self.execute_count = 0

    async def execute(self, statement, params=None):
        self.execute_count += 1
        r = MagicMock()
        r.mappings.return_value.all.return_value = []
        r.mappings.return_value.first.return_value = None
        r.first.return_value = None
        return r


def test_bulk_queue_summary_runs_exactly_one_query():
    """Audit 2026-05-31 Bug 12: queue summary aggregation across N entities ×
    Y years is ONE query, not N×Y queries."""
    session = _CountingFakeSession()
    entity_ids = [str(uuid4()) for _ in range(50)]
    tax_years = [2022, 2023, 2024, 2025, 2026]
    asyncio.run(tax_status_mod._bulk_queue_summary(session, entity_ids, tax_years))
    assert session.execute_count == 1, (
        f"expected 1 grouped query, got {session.execute_count}"
    )


def test_bulk_approval_summary_runs_exactly_one_query():
    session = _CountingFakeSession()
    entity_ids = [str(uuid4()) for _ in range(50)]
    tax_years = [2022, 2023, 2024, 2025, 2026]
    asyncio.run(
        tax_status_mod._bulk_approval_summary(session, entity_ids, tax_years)
    )
    assert session.execute_count == 1


def test_bulk_specialist_run_runs_exactly_one_query():
    session = _CountingFakeSession()
    entity_ids = [str(uuid4()) for _ in range(50)]
    tax_years = [2022, 2023, 2024, 2025, 2026]
    asyncio.run(
        tax_status_mod._bulk_last_specialist_run(session, entity_ids, tax_years)
    )
    assert session.execute_count == 1


def test_bulk_helpers_short_circuit_on_empty_inputs():
    """Empty entity_ids or empty tax_years must NOT issue any SQL — the
    short-circuit keeps an empty-workspace request from doing wasted work."""
    session = _CountingFakeSession()
    asyncio.run(tax_status_mod._bulk_queue_summary(session, [], [2025]))
    asyncio.run(tax_status_mod._bulk_approval_summary(session, [], [2025]))
    asyncio.run(tax_status_mod._bulk_last_specialist_run(session, [], [2025]))
    asyncio.run(
        tax_status_mod._bulk_queue_summary(session, [str(uuid4())], [])
    )
    assert session.execute_count == 0


def test_bulk_helpers_zero_fill_every_requested_cell():
    """Result dict must contain a default-value entry for EVERY (entity, year)
    cell asked about, even when the DB returned no rows for that cell. The
    main endpoint loop relies on .get(key, default) NOT raising and on the
    schema-shaped dict so downstream consumers can read e.g. q_summary['pending']
    unconditionally."""

    class _AlwaysEmptySession:
        async def execute(self, statement, params=None):
            r = MagicMock()
            r.mappings.return_value.all.return_value = []
            return r

    session = _AlwaysEmptySession()
    eids = [str(uuid4()), str(uuid4())]
    yrs = [2024, 2025]

    q = asyncio.run(tax_status_mod._bulk_queue_summary(session, eids, yrs))
    a = asyncio.run(tax_status_mod._bulk_approval_summary(session, eids, yrs))
    s = asyncio.run(tax_status_mod._bulk_last_specialist_run(session, eids, yrs))

    for eid in eids:
        for yr in yrs:
            key = (eid, yr)
            assert key in q
            assert q[key] == {"pending": 0, "reviewed": 0, "resolved": 0, "rejected": 0}
            assert key in a
            assert a[key] == {"pending": 0, "total": 0}
            assert key in s
            assert s[key] is None
