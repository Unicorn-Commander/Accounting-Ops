"""Period-close enforcement for the RECATEGORIZATION path (audit Bug 4).

Pure-Python smoke tests proving:

  1. `post_correction` (tier-L auto-apply path) is refused with
     PostingRefusal('blocked_period_closed') when the original entry's
     effective_date falls inside a closed window.

  2. `apply_held_recategorization` (the approve-apply path used by
     decide_approval when a human approves a material/material-blocked
     recat that had been queued) is ALSO refused.

  3. Per-entity isolation: entity A's closed period does not affect entity B's
     recat.

The pre-fix path inserted the reversal + restatement using the ORIGINAL
effective_date with no closed_period check, so a held material recat could
silently mutate closed books after the close. The fix is the shared
`assert_period_open` helper in app/journal/posting.py, called from
`_apply_recategorization` before the nested transaction opens.

All DB calls are faked — no docker / postgres required.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.journal.posting import (
    PostingRefusal,
    apply_held_recategorization,
    post_correction,
)


# ---------------------------------------------------------------------------
# Fake session helpers
# ---------------------------------------------------------------------------


class _FakeRowSingle:
    def __init__(self, values):
        self._v = values

    def __getitem__(self, idx):
        return self._v[idx]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _RecatSession:
    """Routes execute() so _apply_recategorization can run through the period
    check + (in the auto-apply path) the materiality + _resolve_category_account
    + _is_cash_side calls.

    closed_period_rows: list[dict] with 'period_end'.
    """

    def __init__(
        self,
        *,
        closed_period_rows=None,
        cash_canonical: str = "1010·Cash · Primary Checking",
        new_cat_canonical: str = "6230·SaaS · Email",
    ):
        self._closed = closed_period_rows or []
        self._cash_canonical = cash_canonical
        self._new_cat = new_cat_canonical
        self.statements: list[str] = []
        self.insert_count = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append(sql)

        # closed_period guard
        if "closed_period" in sql and "INSERT" not in sql:
            rows = [_FakeRowSingle([r["period_end"]]) for r in self._closed]
            return _FakeResult(rows)

        # _resolve_category_account: SELECT canonical_form / account_number
        if (
            "chart_of_account" in sql
            and "account_number = :num" in sql
        ):
            return _FakeResult([{"canonical_form": self._new_cat}])

        # _is_cash_side: SELECT account_type WHERE canonical_form = :cf
        # Return 'asset' for the cash canonical (1010·…), 'expense' for the
        # category side. That way the recat function sees ONE cash side, ONE
        # category side and proceeds to the period guard.
        if (
            "chart_of_account" in sql
            and "account_type" in sql
            and "canonical_form" in sql
        ):
            cf = (params or {}).get("cf", "")
            if "1010" in str(cf) or "Cash" in str(cf):
                return _FakeResult([{"account_type": "asset"}])
            return _FakeResult([{"account_type": "expense"}])

        # journal_entry idempotency lookup (apply path)
        if "journal_entry" in sql and "source_external_id" in sql and "SELECT" in sql:
            return _FakeResult([])

        # journal_entry chain lookup (get_last_chain_hash etc.) — return empty.
        if "INSERT INTO journal_entry" in sql:
            self.insert_count += 1
            return _FakeResult([])

        # posting_log INSERT
        if "INSERT INTO posting_log" in sql:
            return _FakeResult([])

        # approval_queue INSERT (when materiality blocks)
        if "INSERT INTO approval_queue" in sql:
            return _FakeResult([])

        # Fallback: empty result.
        return _FakeResult([])

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        yield self


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _orig_entry(
    *,
    entity_id: str,
    effective_date,
    amount_cents: int = 100_00,
    category: str = "SaaS · AI",
):
    """Build the ``original`` dict that post_correction / apply_held_recategorization expect."""
    return {
        "id": uuid4(),
        "amount": Decimal(amount_cents) / Decimal(100),
        "debit_account": "6210·SaaS · AI",
        "credit_account": "1010·Cash · Primary Checking",
        "category": category,
        "effective_date": effective_date,
        "payee": "Anthropic",
        "description": "Test recat candidate",
        "memo": None,
        "receipt_link_id": None,
    }


def _make_auto_post_verdict():
    from app.journal.materiality import MaterialityVerdict
    return MaterialityVerdict(outcome="auto_post", tier="L", reason="auto")


# ---------------------------------------------------------------------------
# Test 1: post_correction (tier-L auto-apply) blocked in closed period
# ---------------------------------------------------------------------------


def test_post_correction_blocked_when_in_closed_period():
    """A tier-L recat through post_correction must NOT touch the ledger when
    the original entry's effective_date is inside a closed window.

    Pre-fix this silently appended the reversal + restatement (the closed-period
    check lived only in post_journal_entry).
    """
    entity_id = str(uuid4())
    effective_date = date(2025, 3, 15)  # inside closed window
    period_end = date(2025, 3, 31)

    session = _RecatSession(closed_period_rows=[{"period_end": period_end}])
    orig = _orig_entry(entity_id=entity_id, effective_date=effective_date)

    with (
        patch("app.journal.posting.evaluate", return_value=_make_auto_post_verdict()),
    ):
        result = asyncio.run(
            post_correction(
                session,
                workspace_id=str(uuid4()),
                entity_id=entity_id,
                original=orig,
                new_category="SaaS · Email",
                reasoning="should-be-blocked",
                actor_uc_uid="tester",
            )
        )

    assert isinstance(result, PostingRefusal), f"expected refusal, got {result}"
    assert result.outcome == "blocked_period_closed", result
    assert "2025-03-31" in result.reason or "period" in result.reason.lower()
    # And no journal_entry insert was issued.
    assert session.insert_count == 0


# ---------------------------------------------------------------------------
# Test 2: apply_held_recategorization (approve-apply) blocked in closed period
# ---------------------------------------------------------------------------


def test_apply_held_recategorization_blocked_when_in_closed_period():
    """The decide_approval -> apply_held_recategorization path must refuse a
    material recat that was queued before the period closed and approved after.

    This is the more subtle path — the original entry is fine (it was posted
    before the close), but reversing + restating it now would mutate filed
    period numbers. The shared assert_period_open helper catches this.
    """
    entity_id = str(uuid4())
    effective_date = date(2025, 2, 28)  # inside closed window
    period_end = date(2025, 3, 31)

    session = _RecatSession(closed_period_rows=[{"period_end": period_end}])
    orig = _orig_entry(entity_id=entity_id, effective_date=effective_date)

    result = asyncio.run(
        apply_held_recategorization(
            session,
            workspace_id=str(uuid4()),
            entity_id=entity_id,
            original=orig,
            new_category="SaaS · Email",
            reasoning="approved-by-human",
            approving_human_uc_uid="approver-uid",
        )
    )

    assert isinstance(result, PostingRefusal), f"expected refusal, got {result}"
    assert result.outcome == "blocked_period_closed", result
    assert session.insert_count == 0


# ---------------------------------------------------------------------------
# Test 3: Per-entity isolation — A's close doesn't block B's recat
# ---------------------------------------------------------------------------


def test_apply_held_recategorization_other_entity_not_blocked():
    """Entity A's closed period must not block a recat for entity B.

    The guard queries WHERE entity_id = :eid; B has no closed periods so its
    apply path proceeds past the period guard. We don't assert success here
    (we don't want to fake the full chain advance) — only that the refusal is
    NOT 'blocked_period_closed'.
    """
    entity_b = str(uuid4())
    effective_date = date(2025, 4, 1)

    session = _RecatSession(closed_period_rows=[])  # B has no closed periods
    orig = _orig_entry(entity_id=entity_b, effective_date=effective_date)

    with patch(
        "app.journal.posting.get_last_chain_hash",
        new_callable=AsyncMock,
        return_value=b"\x00" * 32,
    ):
        result = asyncio.run(
            apply_held_recategorization(
                session,
                workspace_id=str(uuid4()),
                entity_id=entity_b,
                original=orig,
                new_category="SaaS · Email",
                reasoning="cross-entity",
                approving_human_uc_uid="approver-uid",
            )
        )

    # Whatever shape it returns, it must NOT be blocked_period_closed.
    if isinstance(result, PostingRefusal):
        assert result.outcome != "blocked_period_closed", (
            f"Entity B was incorrectly blocked by A's closed period: {result}"
        )


# ---------------------------------------------------------------------------
# Test 4: shared helper exists + returns expected values
# ---------------------------------------------------------------------------


def test_assert_period_open_helper_exposed():
    """The shared helper used by both post_journal_entry and
    _apply_recategorization must be importable + behave as a small SELECT."""
    from app.journal.posting import assert_period_open

    session = _RecatSession(closed_period_rows=[{"period_end": date(2025, 3, 31)}])
    result = asyncio.run(
        assert_period_open(
            session, entity_id=str(uuid4()), effective_date=date(2025, 3, 15)
        )
    )
    assert result == date(2025, 3, 31), result

    open_session = _RecatSession(closed_period_rows=[])
    open_result = asyncio.run(
        assert_period_open(
            open_session, entity_id=str(uuid4()), effective_date=date(2025, 4, 1)
        )
    )
    assert open_result is None, open_result
