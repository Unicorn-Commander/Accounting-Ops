"""Focused contracts for durable eBay human decisions."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.connectors.ebay.decision_state import item_decision
from scripts import ebay_sync

from tests.smoke.test_ebay_sync_unit import _EbayHttp, _blob, _install_sync, _payout
from tests.smoke.test_ebay_mapper_unit import _StubSession, _ACCOUNTS


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"rejected_ebay_items": {"TXN-1": {"approval_id": "a"}}}, "rejected"),
        ({"approved_ebay_items": {"TXN-1": {"approval_id": "a"}}}, "approved"),
        (
            {
                "rejected_ebay_items": {},
                "approved_ebay_items": {"TXN-1": {"approval_id": "a"}},
            },
            "approved",
        ),
        ({}, None),
    ],
)
def test_item_decision_reads_only_the_durable_external_id_state(state, expected):
    assert item_decision(state, "TXN-1") == expected
    assert item_decision(state, "TXN-client-payload-field") is None


@pytest.mark.asyncio
async def test_worker_rejects_without_mapper_and_approves_raw_item(
    monkeypatch: pytest.MonkeyPatch,
):
    workspace_id = uuid4()
    entity_id = uuid4()
    payout = _payout(0)
    http = _EbayHttp([payout], {})
    real_client = ebay_sync.EbayFinancesClient(
        blob=_blob(workspace_id, entity_id), _http_factory=lambda: http
    )
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=0)
    position = {"last_sync_at": None, "continuation_cursor": None}
    decisions = {payout["payoutId"]: "rejected"}
    calls: list[dict] = []

    async def mapper(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    monkeypatch.setattr(ebay_sync, "item_decision", lambda state, item_id: decisions.get(item_id))
    _install_sync(
        monkeypatch,
        client=real_client,
        session=session,
        position=position,
        mapper=mapper,
    )
    rejected_summary = await ebay_sync._sync_one_workspace(workspace_id)
    assert rejected_summary["rejected"] == 1
    assert rejected_summary["rejected_items"] == [
        {"id": payout["payoutId"], "decision": "rejected"}
    ]
    assert not calls

    decisions[payout["payoutId"]] = "approved"
    approved_summary = await ebay_sync._sync_one_workspace(workspace_id)
    assert approved_summary["last_outcome"] == "ok"
    assert calls and calls[0]["first_run_confirmed"] is True
