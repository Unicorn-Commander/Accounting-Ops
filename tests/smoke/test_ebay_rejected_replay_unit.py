"""A human-rejected eBay item must never be re-proposed on replay.

Audit c1360430 F3: the proposal insert in the eBay mapper
(``_enqueue_for_review``) only deduped against PENDING approvals, so when
the import worker replayed the same source item (the one-day window
overlap) after a human rejection, a fresh approval was created for the
same (entity, source, source_external_id). The proposal insert must honour
a rejected proposal: skip it and log one line with ids only.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest

from app.connectors.ebay import mapper as ebay_mapper
from tests.smoke.test_ebay_mapper_unit import (
    _ACCOUNTS,
    _Row,
    _StubResult,
    _StubSession,
    _txn,
)

_REJECTED_APPROVAL_ID = "11111111-1111-1111-1111-111111111111"


class _ReplayStubSession(_StubSession):
    """Stub session that can hold a decided approval for one external id."""

    def __init__(
        self,
        *,
        latest_decision: tuple[str, str] | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.latest_decision = latest_decision

    async def execute(self, query, params=None):  # type: ignore[no-untyped-def]
        sql = str(query)
        if "FROM approval_queue" in sql and "status IN ('approved', 'rejected')" in sql:
            if self.latest_decision is None:
                return _StubResult([])
            approval_id, status = self.latest_decision
            return _StubResult([_Row({"id": approval_id, "status": status})])
        return await super().execute(query, params)


@pytest.mark.asyncio
async def test_rejected_item_replay_creates_no_new_proposal(caplog):
    """Reject then replay: the proposal insert must skip, not re-propose."""
    session = _ReplayStubSession(
        accounts=_ACCOUNTS,
        existing_posts=0,
        latest_decision=(_REJECTED_APPROVAL_ID, "rejected"),
    )
    txn = _txn("TXN-REJECTED-1", "SALE", "9.99")
    with caplog.at_level(logging.INFO, logger="acctops.ebay_mapper"):
        result = await ebay_mapper.map_and_post_transaction(
            session,
            workspace_id="wid",
            entity_id="eid",
            txn=txn,
            actor_uc_uid="agent:ebay_sync",
        )
    assert result.outcome == "human_rejected", result.reason
    assert result.approval_id is None
    assert session.approvals == [], "rejected item was re-proposed"
    assert session.posted_entries == []
    skip_lines = [
        r.getMessage()
        for r in caplog.records
        if "re-proposal blocked" in r.getMessage()
    ]
    assert len(skip_lines) == 1
    line = skip_lines[0]
    # ids only: entity, source, source_external_id, rejected approval id.
    assert "eid" in line
    assert "ebay_finances" in line
    assert "TXN-REJECTED-1" in line
    assert _REJECTED_APPROVAL_ID in line
    assert "9.99" not in line and "999" not in line


@pytest.mark.asyncio
async def test_never_seen_item_is_still_proposed():
    """A never-seen item is proposed exactly as today."""
    session = _ReplayStubSession(accounts=_ACCOUNTS, existing_posts=0)
    txn = _txn("TXN-NEW-1", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session,
        workspace_id="wid",
        entity_id="eid",
        txn=txn,
        actor_uc_uid="agent:ebay_sync",
    )
    assert result.outcome == "queued_first_run", result.reason
    assert result.approval_id is not None
    assert len(session.approvals) == 1


@pytest.mark.asyncio
async def test_later_approval_wins_over_older_rejection():
    """A newer APPROVED decision for the item is not blocked by an old row."""
    session = _ReplayStubSession(
        accounts=_ACCOUNTS,
        existing_posts=0,
        latest_decision=(_REJECTED_APPROVAL_ID, "approved"),
    )
    txn = _txn("TXN-REAPPROVED-1", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session,
        workspace_id="wid",
        entity_id="eid",
        txn=txn,
        actor_uc_uid="agent:ebay_sync",
    )
    assert result.outcome == "queued_first_run", result.reason
    assert len(session.approvals) == 1


@pytest.mark.asyncio
async def test_approved_item_replay_stays_idempotent():
    """An approved (posted) item replayed is unchanged: idempotent as today."""
    session = _ReplayStubSession(accounts=_ACCOUNTS, existing_posts=31)
    session.idempotent_hit = True
    txn = _txn("TXN-APPROVED-1", "SALE", "9.99")
    result = await ebay_mapper.map_and_post_transaction(
        session,
        workspace_id="wid",
        entity_id="eid",
        txn=txn,
        actor_uc_uid="agent:ebay_sync",
    )
    assert result.outcome == "blocked_idempotent", result.reason
    assert session.approvals == []
    assert session.posted_entries == []
