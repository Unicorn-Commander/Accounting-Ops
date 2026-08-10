"""Server-side confirmation derivation on the MCP posting tool.

Pure-Python smoke tests, no DB — all DB calls are faked (same style as
test_period_close.py). External report 2026-08-10: ``user_confirmed`` on the
MCP surface is agent-supplied, so a prompt-injected agent could set it and
bypass the materiality gate. The fix derives the attestation server-side: the
flag is honored only when an APPROVED approval_queue row matches the exact
(entity, source_external_id, amount), and the deciding human is stamped as
the approver.

Four scenarios:
  1. user_confirmed=True with NO approved row -> primitive called with
     user_confirmed=False / no approver; refusal reason explains derivation.
  2. user_confirmed=True WITH an approved row -> primitive called with
     user_confirmed=True and approving_human_uc_uid = the deciding human.
  3. user_confirmed=False -> approval_queue is never even queried.
  4. _verified_confirmation matches amount by canonical string and requires
     status='approved' + a non-null decider (fail closed).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.journal.materiality import MaterialityVerdict
from app.journal.posting import PostingOutcome, PostingRefusal
from app.tools import journal_tools
from app.tools.journal_tools import _verified_confirmation


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeResult:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def first(self) -> _Row | None:
        return self._row


class _FakeSession:
    """Captures every execute; answers the approval_queue lookup."""

    def __init__(self, approval_row: _Row | None = None) -> None:
        self.approval_row = approval_row
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append((sql, params))
        if "approval_queue" in sql:
            return _FakeResult(self.approval_row)
        return _FakeResult(None)

    def approval_queries(self) -> list[tuple[str, dict[str, Any] | None]]:
        return [(s, p) for s, p in self.executed if "approval_queue" in s]


class _Ctx:
    user_uc_uid = "agent-caller"
    agent_id = "test-agent"


def _run_tool(
    *,
    session: _FakeSession,
    journal_post: AsyncMock,
    user_confirmed: bool,
) -> dict[str, Any]:
    @contextlib.asynccontextmanager
    async def fake_ws_session(_factory: Any, _wid: Any):
        yield session

    with (
        patch.object(journal_tools, "authorize_workspace", AsyncMock(return_value=_Ctx())),
        patch.object(journal_tools, "workspace_session", fake_ws_session),
        patch.object(journal_tools, "_journal_post", journal_post),
    ):
        return asyncio.run(
            journal_tools.post_journal_entry(
                workspace_id=uuid4(),
                entity_id=uuid4(),
                source="bofa-statement",
                source_external_id="txn-123",
                effective_date="2026-08-01",
                amount_cents=-250_000,
                description="Wire transfer",
                category="Software · SaaS",
                user_confirmed=user_confirmed,
            )
        )


def test_unverified_claim_is_not_honored() -> None:
    session = _FakeSession(approval_row=None)
    journal_post = AsyncMock(
        return_value=PostingRefusal(
            outcome="blocked_materiality",
            reason="Amount ≥ $1,000.00 requires explicit human confirmation (not auto-postable).",
        )
    )
    out = _run_tool(session=session, journal_post=journal_post, user_confirmed=True)

    kwargs = journal_post.call_args.kwargs
    assert kwargs["user_confirmed"] is False
    assert kwargs["approving_human_uc_uid"] is None
    assert out["ok"] is False
    assert out["outcome"] == "blocked_materiality"
    assert "derived server-side" in out["reason"]
    assert len(session.approval_queries()) == 1


def test_verified_claim_stamps_deciding_human() -> None:
    session = _FakeSession(approval_row=_Row(decided_by_uc_uid="cfo-jane"))
    journal_post = AsyncMock(
        return_value=PostingOutcome(
            journal_entry_id=uuid4(),
            materiality=MaterialityVerdict(
                tier="H", outcome="auto_post", reason="User explicitly confirmed the posting."
            ),
        )
    )
    out = _run_tool(session=session, journal_post=journal_post, user_confirmed=True)

    kwargs = journal_post.call_args.kwargs
    assert kwargs["user_confirmed"] is True
    assert kwargs["approving_human_uc_uid"] == "cfo-jane"
    assert out["ok"] is True
    assert out["outcome"] == "posted"


def test_no_claim_skips_approval_lookup() -> None:
    session = _FakeSession(approval_row=_Row(decided_by_uc_uid="cfo-jane"))
    journal_post = AsyncMock(
        return_value=PostingRefusal(
            outcome="queued_for_confirm",
            reason="Amount in M tier ($75.00 - $1,000.00); awaits human confirm.",
        )
    )
    out = _run_tool(session=session, journal_post=journal_post, user_confirmed=False)

    kwargs = journal_post.call_args.kwargs
    assert kwargs["user_confirmed"] is False
    assert kwargs["approving_human_uc_uid"] is None
    assert session.approval_queries() == []
    assert "derived server-side" not in out["reason"]


def test_verification_query_matches_exact_transaction() -> None:
    session = _FakeSession(approval_row=_Row(decided_by_uc_uid="cfo-jane"))
    eid = uuid4()
    uid = asyncio.run(
        _verified_confirmation(
            session,
            entity_id=eid,
            source_external_id="txn-123",
            amount_cents=-250_000,
        )
    )
    assert uid == "cfo-jane"

    [(sql, params)] = session.approval_queries()
    assert params == {"eid": str(eid), "ext": "txn-123", "amt": "-250000"}
    assert "status = 'approved'" in sql
    assert "subject_type = 'bank_transaction_import'" in sql
    assert "decided_by_uc_uid IS NOT NULL" in sql

    # No matching row -> fail closed.
    assert (
        asyncio.run(
            _verified_confirmation(
                _FakeSession(approval_row=None),
                entity_id=eid,
                source_external_id="txn-123",
                amount_cents=-250_000,
            )
        )
        is None
    )
