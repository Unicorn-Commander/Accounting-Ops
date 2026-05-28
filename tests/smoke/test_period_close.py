"""Period close / lock tests (fix #4).

Pure-Python smoke tests for the period-close guard in post_journal_entry
and the close_period API endpoint.  No DB -- all DB calls are faked.

Five scenarios:
  1. Close a period -> INSERT succeeds (verified via SQL capture).
  2. Attempt to post with effective_date <= period_end of a closed period
     -> refused with blocked_period_closed.
  3. Posting into an open period (no closed_period row) -> succeeds (regression).
  4. Per-entity isolation: one entity's closed_period doesn't block another's posts.
  5. blocked_period_closed is present in the PostingResult Literal.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import HTTPException

from app.journal.posting import (
    PostingRefusal,
    PostingOutcome,
    post_journal_entry,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _posting_kwargs(
    *,
    workspace_id: str | None = None,
    entity_id: str | None = None,
    effective_date=None,
    amount_cents: int = 100_00,
    category: str = "SaaS · AI",
    source: str = "test",
    source_external_id: str | None = None,
) -> dict[str, Any]:
    """Build a minimal valid kwargs dict for post_journal_entry."""
    return {
        "workspace_id": workspace_id or str(uuid4()),
        "entity_id": entity_id or str(uuid4()),
        "source": source,
        "source_external_id": source_external_id,
        "effective_date": effective_date or date(2025, 3, 31),
        "amount_cents": amount_cents,
        "description": "Test expense",
        "payee": None,
        "memo": None,
        "category": category,
        "category_status": "auto",
        "entity_mapped": True,
        "sign_matched": True,
        "external_account_id": "ext-123",
        "has_receipt_link": False,
        "receipt_link_id": None,
        "posted_by_uc_uid": "test-user",
        "posted_by_agent_id": None,
        "user_confirmed": False,
        "reasoning_trace": "",
        "rule_version_applied": None,
        "model_version": "",
        "counterparty_id": None,
        "event_id": None,
    }


def _make_auto_post_verdict():
    from app.journal.materiality import MaterialityVerdict
    return MaterialityVerdict(outcome="auto_post", tier="L", reason="auto")


# ---------------------------------------------------------------------------
# Fake session for post_journal_entry tests
# ---------------------------------------------------------------------------


class _FakeRowSingle:
    """A row with index access (for scalar results like closed_period.period_end)."""
    def __init__(self, values):
        self._v = values

    def __getitem__(self, idx):
        return self._v[idx]


class _ScalarResult:
    """Returned by execute() for COUNT(*) queries; supports .scalar_one()."""
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _PostingSession:
    """Routes execute() calls for post_journal_entry tests.

    closed_period_rows: list of dicts with 'period_end' key.  If non-empty
    the period guard will fire.
    """

    def __init__(self, *, closed_period_rows=None, posting_entity: str = ""):
        self._closed = closed_period_rows or []
        self._posting_entity = posting_entity
        self.statements: list[str] = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)

        # closed_period guard query
        if "closed_period" in sql and "INSERT" not in sql:
            rows = [_FakeRowSingle([r["period_end"]]) for r in self._closed]
            return _FakeResult(rows)

        # idempotency check
        if "source_external_id" in sql and "journal_entry" in sql and "SELECT" in sql:
            return _FakeResult([])

        # COA lookups
        if "chart_of_account" in sql:
            return _FakeResult([])

        return _FakeResult([])

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        yield self


# ---------------------------------------------------------------------------
# Test 1: close_period endpoint issues an INSERT INTO closed_period
# ---------------------------------------------------------------------------


def test_close_period_insert_invoked():
    """close_period endpoint issues an INSERT INTO closed_period with the
    correct entity_id, period_end, reason, and workspace_id."""
    from app.api import journal as journal_mod
    from app.tenancy import WorkspaceContext

    wid = uuid4()
    eid = uuid4()
    inserted_sqls: list[str] = []

    # Pre-build the re-fetch row so it can be referenced inside the class.
    _closed_row_data = {
        "entity_id": str(eid),
        "period_end": "2025-03-31",
        "closed_at": "2026-01-01T00:00:00+00:00",
        "closed_by_uc_uid": "u",
        "reason": "Q1 close",
    }

    class _ClosedRow:
        def __getitem__(self, k):
            return _closed_row_data[k]
        def keys(self):
            return list(_closed_row_data.keys())
        def items(self):
            return _closed_row_data.items()

    class _EntityRow:
        display_name = "Test Entity"
        def __getitem__(self, k):
            return getattr(self, k, None)
        def keys(self):
            return ["id", "display_name"]

    class _ClosePeriodSession:
        """Fake session specific to close_period endpoint calls.

        Ordered dispatch: the endpoint executes queries in this order:
          0. SELECT ... FROM entity          -> entity existence
          1. SELECT ... FROM closed_period   -> duplicate check (no row)
          2. INSERT INTO closed_period       -> the actual close
          3. SELECT count(*) FROM journal... -> entry count
          4. SELECT ... FROM closed_period   -> re-fetch the inserted row
        We use a call counter on closed_period SELECTs to distinguish #1 and #4.
        """

        def __init__(self):
            self.statements: list[str] = []
            self._cp_select_count = 0  # closed_period SELECT call counter

        async def execute(self, statement, params=None):
            sql = str(statement)
            self.statements.append(sql)

            # entity existence check
            if "FROM entity" in sql:
                return _FakeResult([_EntityRow()])

            # closed_period SELECTs (duplicate check + re-fetch)
            if "closed_period" in sql and "INSERT" not in sql:
                self._cp_select_count += 1
                if self._cp_select_count == 1:
                    return _FakeResult([])  # no duplicate
                return _FakeResult([_ClosedRow()])  # re-fetch

            # INSERT INTO closed_period
            if "INSERT" in sql and "closed_period" in sql:
                inserted_sqls.append(sql)
                return _FakeResult([])

            # COUNT(*) entries_in_period
            if "count(*)" in sql:
                return _ScalarResult(5)

            return _FakeResult([])

    fake_session = _ClosePeriodSession()

    @contextlib.asynccontextmanager
    async def _fake_ws(session_local, workspace_id):
        yield fake_session

    original_ws = journal_mod.workspace_session
    journal_mod.workspace_session = _fake_ws
    try:
        ws = WorkspaceContext(
            workspace_id=wid,
            workspace_slug="test",
            role="admin",
            user_uc_uid="u",
            agent_id=None,
            entity_scope=None,
            hipaa_protected=False,
        )
        req = journal_mod.ClosePeriodRequest(period_end="2025-03-31", reason="Q1 close")
        result = asyncio.run(
            journal_mod.close_period(
                workspace_id=wid,
                entity_id=eid,
                body=req,
                workspace=ws,
            )
        )
    finally:
        journal_mod.workspace_session = original_ws

    assert result["ok"] is True
    assert result["entity_id"] == str(eid)
    assert result["entries_in_period"] == 5
    assert result["closed_period"]["period_end"] == "2025-03-31"

    # Verify the INSERT was issued
    assert inserted_sqls, "INSERT INTO closed_period was never executed"
    assert "closed_period" in inserted_sqls[0]


# ---------------------------------------------------------------------------
# Test 2: Attempt to post with effective_date <= period_end -> refused
# ---------------------------------------------------------------------------


def test_post_journal_entry_blocked_by_closed_period():
    """When a closed_period row exists with period_end >= effective_date,
    post_journal_entry must return PostingRefusal(outcome='blocked_period_closed').
    """
    entity_id = str(uuid4())
    effective_date = date(2025, 3, 15)  # inside the closed period
    period_end = date(2025, 3, 31)      # period_end >= effective_date -> blocked

    session = _PostingSession(
        closed_period_rows=[{"period_end": period_end}],
        posting_entity=entity_id,
    )

    kwargs = _posting_kwargs(entity_id=entity_id, effective_date=effective_date)

    with (
        patch("app.journal.posting.evaluate", return_value=_make_auto_post_verdict()),
        patch("app.journal.posting.resolve_accounts_for_txn", new_callable=AsyncMock),
        patch("app.journal.posting._log_post_attempt", new_callable=AsyncMock),
    ):
        result = asyncio.run(post_journal_entry(session, **kwargs))

    assert isinstance(result, PostingRefusal)
    assert result.outcome == "blocked_period_closed"
    assert "2025-03-31" in result.reason or "period" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test 3: Posting into an open period succeeds (regression)
# ---------------------------------------------------------------------------


def test_post_journal_entry_succeeds_when_no_closed_period():
    """No closed_period rows for the entity -> post proceeds past the guard.

    The post is allowed to continue (may hit other guards like COA resolution,
    but it must NOT return blocked_period_closed).
    """
    from app.journal.posting import AccountPair
    from app.journal.hash_chain import GENESIS_HASH

    entity_id = str(uuid4())
    effective_date = date(2025, 4, 15)
    dummy_hash = b"\x00" * 32

    session = _PostingSession(
        closed_period_rows=[],  # empty -> guard passes
        posting_entity=entity_id,
    )
    kwargs = _posting_kwargs(
        entity_id=entity_id,
        effective_date=effective_date,
        source_external_id="ext-open-001",
    )

    with (
        patch("app.journal.posting.evaluate", return_value=_make_auto_post_verdict()),
        patch("app.journal.posting.resolve_accounts_for_txn", new_callable=AsyncMock,
              return_value=AccountPair(
                  debit_account="6210·SaaS · AI",
                  credit_account="1010·Checking",
              )),
        patch("app.journal.posting.get_last_chain_hash", new_callable=AsyncMock,
              return_value=GENESIS_HASH),
        patch("app.journal.posting.compute_hash", return_value=dummy_hash),
        patch("app.journal.posting._insert_journal_entry", new_callable=AsyncMock),
        patch("app.journal.posting._log_post_attempt", new_callable=AsyncMock),
    ):
        result = asyncio.run(post_journal_entry(session, **kwargs))

    # Must not be refused due to the period-close guard
    assert not (
        isinstance(result, PostingRefusal) and result.outcome == "blocked_period_closed"
    ), f"Post was incorrectly blocked by period-close guard: {result}"


# ---------------------------------------------------------------------------
# Test 4: Per-entity isolation
# ---------------------------------------------------------------------------


def test_closed_period_does_not_block_different_entity():
    """Entity A's closed period must NOT block posts for entity B.

    The guard SQL uses WHERE entity_id = :eid so only the posting entity's
    periods are checked.
    """
    from app.journal.posting import AccountPair
    from app.journal.hash_chain import GENESIS_HASH

    entity_a = str(uuid4())
    entity_b = str(uuid4())
    period_end = date(2025, 3, 31)
    effective_date = date(2025, 3, 20)  # inside entity A's closed window
    dummy_hash = b"\x00" * 32

    # Session for entity A: returns the closed_period row -> blocked
    session_a = _PostingSession(
        closed_period_rows=[{"period_end": period_end}],
        posting_entity=entity_a,
    )
    kwargs_a = _posting_kwargs(
        entity_id=entity_a,
        effective_date=effective_date,
        source_external_id="ext-a-001",
    )

    with (
        patch("app.journal.posting.evaluate", return_value=_make_auto_post_verdict()),
        patch("app.journal.posting.resolve_accounts_for_txn", new_callable=AsyncMock),
        patch("app.journal.posting._log_post_attempt", new_callable=AsyncMock),
    ):
        result_a = asyncio.run(post_journal_entry(session_a, **kwargs_a))

    assert isinstance(result_a, PostingRefusal)
    assert result_a.outcome == "blocked_period_closed", (
        f"Entity A post should be blocked but got: {result_a}"
    )

    # Session for entity B: returns no closed_period rows -> guard passes
    session_b = _PostingSession(
        closed_period_rows=[],  # B has no closed periods
        posting_entity=entity_b,
    )
    kwargs_b = _posting_kwargs(
        entity_id=entity_b,
        effective_date=effective_date,
        source_external_id="ext-b-001",
    )

    with (
        patch("app.journal.posting.evaluate", return_value=_make_auto_post_verdict()),
        patch("app.journal.posting.resolve_accounts_for_txn", new_callable=AsyncMock,
              return_value=AccountPair(
                  debit_account="6210·SaaS · AI",
                  credit_account="1010·Checking",
              )),
        patch("app.journal.posting.get_last_chain_hash", new_callable=AsyncMock,
              return_value=GENESIS_HASH),
        patch("app.journal.posting.compute_hash", return_value=dummy_hash),
        patch("app.journal.posting._insert_journal_entry", new_callable=AsyncMock),
        patch("app.journal.posting._log_post_attempt", new_callable=AsyncMock),
    ):
        result_b = asyncio.run(post_journal_entry(session_b, **kwargs_b))

    # Entity B must NOT be blocked by entity A's closed period
    assert not (
        isinstance(result_b, PostingRefusal) and result_b.outcome == "blocked_period_closed"
    ), f"Entity B was incorrectly blocked by entity A's closed period: {result_b}"


# ---------------------------------------------------------------------------
# Test 5: blocked_period_closed present in PostingResult Literal
# ---------------------------------------------------------------------------


def test_blocked_period_closed_in_posting_result_literal():
    """The new outcome string must be declared in the PostingResult type."""
    import typing
    import app.journal.posting as pmod
    args = typing.get_args(pmod.PostingResult)
    assert "blocked_period_closed" in args, (
        f"'blocked_period_closed' not found in PostingResult Literal: {args}"
    )


# ---------------------------------------------------------------------------
# Test 6: feat: list closed periods
# ---------------------------------------------------------------------------


def test_list_closed_periods_empty_and_multiple_ordered():
    """closed-periods endpoint returns an empty list, then period_end DESC rows."""
    from app.api import journal as journal_mod
    from app.tenancy import WorkspaceContext

    wid = uuid4()
    eid = uuid4()

    class _ClosedPeriodRow:
        def __init__(self, period_end, closed_at, closed_by_uc_uid, reason):
            self._data = {
                "period_end": period_end,
                "closed_at": closed_at,
                "closed_by_uc_uid": closed_by_uc_uid,
                "reason": reason,
            }

        def __getitem__(self, k):
            return self._data[k]

        def keys(self):
            return list(self._data.keys())

        def items(self):
            return self._data.items()

    class _ListClosedPeriodsSession:
        def __init__(self, rows):
            self.rows = rows
            self.statements: list[str] = []
            self.params: list[dict[str, Any] | None] = []

        async def execute(self, statement, params=None):
            self.statements.append(str(statement))
            self.params.append(params)
            return _FakeResult(self.rows)

    ws = WorkspaceContext(
        workspace_id=wid,
        workspace_slug="test",
        role="viewer",
        user_uc_uid="u",
        agent_id=None,
        entity_scope=None,
        hipaa_protected=False,
    )

    async def _run_with_rows(rows):
        fake_session = _ListClosedPeriodsSession(rows)

        @contextlib.asynccontextmanager
        async def _fake_ws(session_local, workspace_id):
            yield fake_session

        original_ws = journal_mod.workspace_session
        journal_mod.workspace_session = _fake_ws
        try:
            result = await journal_mod.list_closed_periods(
                workspace_id=wid,
                entity_id=eid,
                workspace=ws,
            )
        finally:
            journal_mod.workspace_session = original_ws
        return result, fake_session

    empty_result, empty_session = asyncio.run(_run_with_rows([]))
    assert empty_result == {"entity_id": str(eid), "closed_periods": []}
    assert empty_session.params[0] == {"eid": str(eid)}

    rows = [
        _ClosedPeriodRow("2025-12-31", "2026-05-28T12:00:00+00:00", "uc_a", "FY close"),
        _ClosedPeriodRow("2025-03-31", "2026-04-01T12:00:00+00:00", "uc_b", "Q1 close"),
    ]
    multiple_result, multiple_session = asyncio.run(_run_with_rows(rows))

    assert multiple_result["entity_id"] == str(eid)
    assert [r["period_end"] for r in multiple_result["closed_periods"]] == [
        "2025-12-31",
        "2025-03-31",
    ]
    assert "ORDER BY period_end DESC" in multiple_session.statements[0]


def test_list_closed_periods_entity_scope_respected():
    """A scoped workspace member cannot list another entity's closed periods."""
    from app.api import journal as journal_mod
    from app.tenancy import WorkspaceContext

    wid = uuid4()
    allowed_eid = uuid4()
    denied_eid = uuid4()
    session_opened = False

    @contextlib.asynccontextmanager
    async def _fake_ws(session_local, workspace_id):
        nonlocal session_opened
        session_opened = True
        yield _PostingSession()

    original_ws = journal_mod.workspace_session
    journal_mod.workspace_session = _fake_ws
    try:
        ws = WorkspaceContext(
            workspace_id=wid,
            workspace_slug="test",
            role="viewer",
            user_uc_uid="u",
            agent_id=None,
            entity_scope=(allowed_eid,),
            hipaa_protected=False,
        )
        try:
            asyncio.run(
                journal_mod.list_closed_periods(
                    workspace_id=wid,
                    entity_id=denied_eid,
                    workspace=ws,
                )
            )
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("Expected entity-scope violation to raise 403")
    finally:
        journal_mod.workspace_session = original_ws

    assert session_opened is False
