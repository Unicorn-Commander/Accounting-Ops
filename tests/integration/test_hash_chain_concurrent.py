"""Audit 2026-05-31 Bug 9 — hash-chain race must retry, not drop writes.

Pre-fix: any IntegrityError from the journal-entry INSERT was treated as a
``blocked_idempotent`` refusal. A concurrent post advancing the per-entity
hash chain between get_last_chain_hash and INSERT would therefore manifest as
a fake idempotency hit — the legitimate transaction never landed. The audit
called this a "data loss" pattern under concurrent imports.

Post-fix: ``_attempt_chain_insert`` distinguishes between TWO IntegrityError
classes via the constraint name extracted from the asyncpg ``UniqueViolation``:

  * ``uq_je_entity_source_external`` — TRUE (entity_id, source, source_external_id)
    duplicate. The other writer already posted the exact same upstream txn;
    return ``blocked_idempotent`` with the existing_entry_id surfaced.
  * any other constraint (including a hypothetical ``(entity_id, this_hash)``
    race) — re-fetch ``get_last_chain_hash``, recompute ``this_hash`` + a fresh
    ``posted_at``, retry up to ``_MAX_CHAIN_RETRIES`` times.

These tests exercise the retry contract WITHOUT a live database — they patch
``_insert_journal_entry`` to simulate the conflict pattern and assert the
loop's branching. A separate live-DB integration test would need a real
concurrent writer; this test focuses on the bounded-retry guarantee that's
load-bearing for correctness.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.journal.posting import (
    PostingOutcome,
    PostingRefusal,
    _MAX_CHAIN_RETRIES,
    _ChainInsertExhausted,
    _ChainInsertIdempotent,
    _attempt_chain_insert,
    _integrity_constraint_name,
    post_journal_entry,
)


# ---------------------------------------------------------------------------
# Helpers — mimic the smoke-test fake session shape but track per-call state.
# ---------------------------------------------------------------------------


class _FakeUniqueViolation(Exception):
    """Mimics the asyncpg UniqueViolation carried on IntegrityError.orig."""

    def __init__(self, constraint_name: str):
        super().__init__(
            f'duplicate key value violates unique constraint "{constraint_name}"'
        )
        self.constraint_name = constraint_name


def _make_integrity_error(constraint_name: str) -> IntegrityError:
    """Build a SQLAlchemy IntegrityError that carries the asyncpg-style orig."""
    return IntegrityError(
        statement="INSERT INTO journal_entry ...",
        params={},
        orig=_FakeUniqueViolation(constraint_name),
    )


def test_integrity_constraint_name_extracts_from_orig():
    """Constraint name disambiguation is the linchpin of the bug-9 fix —
    the retry decision pivots on this string."""
    err = _make_integrity_error("uq_je_entity_source_external")
    assert _integrity_constraint_name(err) == "uq_je_entity_source_external"


def test_integrity_constraint_name_falls_back_to_message_scrape():
    """asyncpg always sets constraint_name, but defensive parsing covers the
    case where a different driver / future SA version doesn't."""

    class _RawErr(Exception):
        pass

    raw = _RawErr(
        'duplicate key value violates unique constraint "uq_je_entity_source_external"'
    )
    err = IntegrityError(statement="", params={}, orig=raw)
    assert _integrity_constraint_name(err) == "uq_je_entity_source_external"


def test_chain_insert_succeeds_on_first_try():
    """Happy path: no conflict, single attempt, returns None (= clean insert)."""
    insert_call_count = 0

    async def _fake_insert(*args, **kwargs):
        nonlocal insert_call_count
        insert_call_count += 1

    session = _FakeChainSession()
    with patch("app.journal.posting._insert_journal_entry", _fake_insert), patch(
        "app.journal.posting.get_last_chain_hash",
        AsyncMock(return_value=b"\x00" * 32),
    ):
        outcome = asyncio.run(
            _attempt_chain_insert(
                session,
                je_id=uuid4(),
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                amount_cents=10_00,
                effective_date=date(2026, 1, 1),
                debit_account="6210::SaaS",
                credit_account="1010::Cash",
                category="SaaS · AI",
                tier="L",
                source="test",
                source_external_id="ext-1",
                description="x",
                payee=None,
                memo=None,
                reasoning_trace="",
                model_version="",
                identity="alice",
                approving_uid=None,
                receipt_link_id=None,
            )
        )
    assert outcome is None
    assert insert_call_count == 1


def test_chain_insert_retries_on_non_idempotent_conflict_then_succeeds():
    """Bug 9 core contract: a constraint conflict that is NOT the idempotency
    unique key triggers a retry. The second attempt sees a fresh prev_hash
    and succeeds. Returns None (= clean insert)."""
    attempts = {"n": 0}

    async def _fake_insert(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Simulate a chain-race conflict on the first attempt only.
            raise _make_integrity_error("ix_je_entity_chain_hash_race")
        # Second attempt: succeed.

    session = _FakeChainSession()
    # Different prev_hash bytes per call so we can verify we re-fetched.
    prev_hashes = [b"\x01" * 32, b"\x02" * 32, b"\x03" * 32]
    with patch("app.journal.posting._insert_journal_entry", _fake_insert), patch(
        "app.journal.posting.get_last_chain_hash",
        AsyncMock(side_effect=prev_hashes),
    ):
        outcome = asyncio.run(
            _attempt_chain_insert(
                session,
                je_id=uuid4(),
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                amount_cents=10_00,
                effective_date=date(2026, 1, 1),
                debit_account="6210::SaaS",
                credit_account="1010::Cash",
                category="SaaS · AI",
                tier="L",
                source="test",
                source_external_id="ext-1",
                description="x",
                payee=None,
                memo=None,
                reasoning_trace="",
                model_version="",
                identity="alice",
                approving_uid=None,
                receipt_link_id=None,
            )
        )
    assert outcome is None
    assert attempts["n"] == 2, (
        "expected exactly 2 attempts (one chain-race conflict + one success)"
    )


def test_chain_insert_returns_idempotent_on_source_external_id_conflict():
    """A constraint conflict on uq_je_entity_source_external is TRUE
    idempotency — do NOT retry; return _ChainInsertIdempotent with the
    looked-up existing id."""
    existing_id = uuid4()

    async def _always_idempotent(*args, **kwargs):
        raise _make_integrity_error("uq_je_entity_source_external")

    session = _FakeChainSession(existing_entry_id=existing_id)
    with patch("app.journal.posting._insert_journal_entry", _always_idempotent), patch(
        "app.journal.posting.get_last_chain_hash",
        AsyncMock(return_value=b"\x00" * 32),
    ):
        outcome = asyncio.run(
            _attempt_chain_insert(
                session,
                je_id=uuid4(),
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                amount_cents=10_00,
                effective_date=date(2026, 1, 1),
                debit_account="6210::SaaS",
                credit_account="1010::Cash",
                category="SaaS · AI",
                tier="L",
                source="test",
                source_external_id="ext-already-here",
                description="x",
                payee=None,
                memo=None,
                reasoning_trace="",
                model_version="",
                identity="alice",
                approving_uid=None,
                receipt_link_id=None,
            )
        )
    assert isinstance(outcome, _ChainInsertIdempotent)
    assert outcome.existing_entry_id == existing_id


def test_chain_insert_exhausts_after_max_retries():
    """When EVERY attempt hits a non-idempotency conflict, the loop returns
    _ChainInsertExhausted after _MAX_CHAIN_RETRIES attempts (not infinite)."""
    attempts = {"n": 0}

    async def _always_chain_race(*args, **kwargs):
        attempts["n"] += 1
        raise _make_integrity_error("ix_je_entity_chain_hash_race")

    session = _FakeChainSession()
    with patch("app.journal.posting._insert_journal_entry", _always_chain_race), patch(
        "app.journal.posting.get_last_chain_hash",
        AsyncMock(return_value=b"\x00" * 32),
    ):
        outcome = asyncio.run(
            _attempt_chain_insert(
                session,
                je_id=uuid4(),
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                amount_cents=10_00,
                effective_date=date(2026, 1, 1),
                debit_account="6210::SaaS",
                credit_account="1010::Cash",
                category="SaaS · AI",
                tier="L",
                source="test",
                source_external_id="ext-1",
                description="x",
                payee=None,
                memo=None,
                reasoning_trace="",
                model_version="",
                identity="alice",
                approving_uid=None,
                receipt_link_id=None,
            )
        )
    assert isinstance(outcome, _ChainInsertExhausted)
    assert attempts["n"] == _MAX_CHAIN_RETRIES, (
        f"expected exactly {_MAX_CHAIN_RETRIES} attempts before exhaustion, "
        f"got {attempts['n']}"
    )


def test_post_journal_entry_succeeds_after_one_chain_race(monkeypatch):
    """End-to-end: post_journal_entry should NOT report blocked_idempotent
    when the underlying _attempt_chain_insert merely had to retry once. This
    is the literal "two concurrent posts to the same entity" scenario from
    the audit: one wins, the other retries + posts cleanly.

    Patches out everything below the materiality+account-resolution layer so
    the chain-race branch is the only interesting bit under test.
    """
    from app.journal import posting as posting_mod
    from app.journal.materiality import MaterialityVerdict
    from app.journal.posting import AccountPair

    insert_attempts = {"n": 0}

    async def _flaky_insert(*args, **kwargs):
        insert_attempts["n"] += 1
        if insert_attempts["n"] == 1:
            raise _make_integrity_error("ix_je_entity_chain_hash_race")

    session = _FakeChainSession()

    async def _mat_gate(**kwargs):
        return MaterialityVerdict(outcome="auto_post", tier="L", reason="auto")

    async def _resolve_pair(*args, **kwargs):
        return AccountPair(debit_account="6210::SaaS", credit_account="1010::Cash")

    monkeypatch.setattr(posting_mod, "evaluate", lambda **k: MaterialityVerdict(
        outcome="auto_post", tier="L", reason="auto"
    ))
    monkeypatch.setattr(posting_mod, "resolve_accounts_for_txn", _resolve_pair)
    monkeypatch.setattr(posting_mod, "_insert_journal_entry", _flaky_insert)
    monkeypatch.setattr(
        posting_mod,
        "get_last_chain_hash",
        AsyncMock(side_effect=[b"\x01" * 32, b"\x02" * 32]),
    )
    monkeypatch.setattr(
        posting_mod,
        "assert_period_open",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        posting_mod, "_log_post_attempt", AsyncMock(return_value=None)
    )

    result = asyncio.run(
        post_journal_entry(
            session,
            workspace_id=str(uuid4()),
            entity_id=str(uuid4()),
            source="test",
            source_external_id="ext-concurrent-1",
            effective_date=date(2026, 1, 1),
            amount_cents=10_00,
            description="x",
            payee=None,
            memo=None,
            category="SaaS · AI",
            category_status="auto",
            entity_mapped=True,
            sign_matched=True,
            external_account_id="ext-123",
            has_receipt_link=False,
            receipt_link_id=None,
            posted_by_uc_uid="alice",
            posted_by_agent_id=None,
            user_confirmed=False,
            reasoning_trace="",
            rule_version_applied=None,
            model_version="",
        )
    )
    assert isinstance(result, PostingOutcome), (
        f"expected clean post after chain-race retry, got {result!r}"
    )
    assert insert_attempts["n"] == 2, (
        "expected one chain-race retry + one success"
    )


# ---------------------------------------------------------------------------
# Test fake session for the chain-insert helper
# ---------------------------------------------------------------------------


import contextlib
from unittest.mock import MagicMock


class _FakeChainSession:
    """Minimal session shim for _attempt_chain_insert and the supporting
    _lookup_existing_entry_id helper. Returns a configurable existing-id
    on the post-conflict SELECT.
    """

    def __init__(self, *, existing_entry_id=None):
        self._existing_entry_id = existing_entry_id

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        yield self

    async def execute(self, statement, params=None):
        sql = str(statement)
        # _lookup_existing_entry_id queries journal_entry by source_external_id.
        if (
            "journal_entry" in sql
            and "SELECT" in sql
            and "source_external_id" in sql
        ):
            row = MagicMock()
            if self._existing_entry_id is None:
                return _make_empty_result()
            row.id = str(self._existing_entry_id)
            return _make_first_result(row)
        # Any other SQL: empty result.
        return _make_empty_result()


def _make_empty_result():
    r = MagicMock()
    r.first.return_value = None
    r.mappings.return_value.first.return_value = None
    r.mappings.return_value.all.return_value = []
    return r


def _make_first_result(row):
    r = MagicMock()
    r.first.return_value = row
    r.mappings.return_value.first.return_value = row
    r.mappings.return_value.all.return_value = [row]
    return r


# ---------------------------------------------------------------------------
# Issue 2 (audit 2026-05-31): defense-in-depth UNIQUE (entity_id, this_hash)
# constraint must be recognized by _attempt_chain_insert as idempotent (the
# data IS already in the DB) rather than as a chain race to retry.
# ---------------------------------------------------------------------------


def test_chain_insert_returns_idempotent_on_entity_this_hash_conflict():
    """A constraint conflict on uq_je_entity_this_hash (migration 0018) is
    treated as idempotent.

    Rationale: that constraint can only fire when two writers hashed identical
    inputs (same prev_hash, same content, same posted_at micro). The existing
    row IS the same logical post; retrying with a fresh prev_hash would either
    succeed cleanly (different chain position → different hash) OR hit the
    source_external_id idempotency branch. Surfacing as _ChainInsertIdempotent
    here closes the defense-in-depth loop the migration opens.
    """
    existing_id = uuid4()

    async def _always_hash_collision(*args, **kwargs):
        raise _make_integrity_error("uq_je_entity_this_hash")

    session = _FakeChainSession(existing_entry_id=existing_id)
    with patch("app.journal.posting._insert_journal_entry", _always_hash_collision), patch(
        "app.journal.posting.get_last_chain_hash",
        AsyncMock(return_value=b"\x00" * 32),
    ):
        outcome = asyncio.run(
            _attempt_chain_insert(
                session,
                je_id=uuid4(),
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                amount_cents=10_00,
                effective_date=date(2026, 1, 1),
                debit_account="6210::SaaS",
                credit_account="1010::Cash",
                category="SaaS · AI",
                tier="L",
                source="test",
                source_external_id="ext-1",
                description="x",
                payee=None,
                memo=None,
                reasoning_trace="",
                model_version="",
                identity="alice",
                approving_uid=None,
                receipt_link_id=None,
            )
        )
    assert isinstance(outcome, _ChainInsertIdempotent), (
        f"expected idempotent on uq_je_entity_this_hash, got {type(outcome).__name__}"
    )
    assert outcome.existing_entry_id == existing_id


def test_chain_insert_legitimate_posts_unaffected_by_new_constraint():
    """The presence of the new constraint mapping must NOT change the
    happy-path behavior: a post that doesn't conflict still returns None
    (= clean insert) on the first attempt."""
    insert_call_count = 0

    async def _fake_insert(*args, **kwargs):
        nonlocal insert_call_count
        insert_call_count += 1

    session = _FakeChainSession()
    with patch("app.journal.posting._insert_journal_entry", _fake_insert), patch(
        "app.journal.posting.get_last_chain_hash",
        AsyncMock(return_value=b"\xab" * 32),
    ):
        outcome = asyncio.run(
            _attempt_chain_insert(
                session,
                je_id=uuid4(),
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                amount_cents=12_34,
                effective_date=date(2026, 5, 31),
                debit_account="6210::SaaS",
                credit_account="1010::Cash",
                category="SaaS · AI",
                tier="L",
                source="test",
                source_external_id="legitimate-post-1",
                description="legitimate",
                payee=None,
                memo=None,
                reasoning_trace="",
                model_version="",
                identity="alice",
                approving_uid=None,
                receipt_link_id=None,
            )
        )
    assert outcome is None
    assert insert_call_count == 1


def test_chain_insert_other_constraints_still_retry_not_idempotent():
    """A constraint that is NEITHER uq_je_entity_source_external NOR
    uq_je_entity_this_hash must still be treated as a chain race (retryable)
    so the bug-9 fix doesn't regress. Only the two specifically-mapped names
    short-circuit to idempotent.
    """
    attempts = {"n": 0}

    async def _race_then_succeed(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _make_integrity_error("some_other_constraint")
        # Second attempt succeeds.

    session = _FakeChainSession()
    with patch("app.journal.posting._insert_journal_entry", _race_then_succeed), patch(
        "app.journal.posting.get_last_chain_hash",
        AsyncMock(return_value=b"\x00" * 32),
    ):
        outcome = asyncio.run(
            _attempt_chain_insert(
                session,
                je_id=uuid4(),
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                amount_cents=10_00,
                effective_date=date(2026, 1, 1),
                debit_account="6210::SaaS",
                credit_account="1010::Cash",
                category="SaaS · AI",
                tier="L",
                source="test",
                source_external_id="ext-1",
                description="x",
                payee=None,
                memo=None,
                reasoning_trace="",
                model_version="",
                identity="alice",
                approving_uid=None,
                receipt_link_id=None,
            )
        )
    assert outcome is None  # retried + succeeded
    assert attempts["n"] == 2
