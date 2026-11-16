"""Offline repros for eBay pagination and durable sync positions."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.connectors.ebay.client import EbayCredentialBlob, EbayFinancesClient
from scripts import ebay_sync

from tests.smoke.test_ebay_mapper_unit import _ACCOUNTS, _StubSession


RUN_END = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
PAYOUT_DATE = "2026-10-20T00:00:00.000Z"


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, body: dict[str, Any]):
        self.body = body

    def json(self) -> dict[str, Any]:
        return self.body

    @property
    def text(self) -> str:
        return "ok"


class _EbayHttp:
    """Dynamic eBay response fake that honors offset and request limit."""

    def __init__(self, payouts: list[dict[str, Any]], transactions: dict[str, list[dict[str, Any]]]):
        self.payouts = payouts
        self.transactions = transactions
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_EbayHttp":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        params = kwargs.get("params", {})
        self.calls.append({"method": method, "url": url, "params": dict(params)})
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 200))
        if url.endswith("/payout"):
            rows = self.payouts[offset : offset + limit]
            return _Response({"payouts": rows, "total": len(self.payouts)})
        if url.endswith("/transaction"):
            payout_id = None
            for part in str(params.get("filter", "")).split(","):
                if part.startswith("payoutId:{"):
                    payout_id = part.removeprefix("payoutId:{").removesuffix("}")
            rows = self.transactions.get(payout_id or "", [])
            return _Response({"transactions": rows[offset : offset + limit], "total": len(rows)})
        raise AssertionError(f"unexpected URL: {url}")


def _blob(workspace_id, entity_id) -> EbayCredentialBlob:
    return EbayCredentialBlob(
        workspace_id=workspace_id,
        credential_id=uuid4(),
        label="test",
        client_id="fictional-app",
        client_secret="fictional-secret",
        refresh_token="fictional-refresh",
        environment="production",
        ru_name="fictional-ru",
        granted_scopes=["https://api.ebay.com/oauth/api_scope/sell.finances"],
        access_token="fictional-access",
        access_token_expires_at=4_000_000_000,
        entity_id=str(entity_id),
    )


def _payout(index: int) -> dict[str, Any]:
    return {
        "payoutId": f"PAY-{index:03d}",
        "amount": {"value": "10.00", "currency": "USD"},
        "payoutDate": PAYOUT_DATE,
        "payoutStatus": "SUCCEEDED",
    }


def _txn(index: int, payout_id: str) -> dict[str, Any]:
    return {
        "transactionId": f"TXN-{payout_id}-{index:03d}",
        "transactionType": "SALE",
        "amount": {"value": "1.00", "currency": "USD"},
        "transactionDate": PAYOUT_DATE,
        "orderId": f"ORDER-{index:03d}",
    }


@pytest.mark.asyncio
async def test_ebay_paginators_report_offset_continuation_without_skipping_rows():
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(index) for index in range(201)]
    transactions = [_txn(index, "PAY-000") for index in range(501)]
    http = _EbayHttp(payouts, {"PAY-000": transactions})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)

    payout_page = await client.list_payouts(limit=200)
    transaction_page = await client.list_transactions(payout_id="PAY-000", limit=500)

    assert len(payout_page) == 200
    assert payout_page.exhausted is False
    assert payout_page.continuation_offset == 200
    assert len(transaction_page) == 500
    assert transaction_page.exhausted is False
    assert transaction_page.continuation_offset == 500
    assert transaction_page.items[-1]["transactionId"] == "TXN-PAY-000-499"


@pytest.mark.asyncio
async def test_sync_recovers_above_cap_payouts_and_transactions(monkeypatch):
    """Two capped passes post every row once and finish both continuations."""
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(index) for index in range(201)]
    transactions = {"PAY-000": [_txn(index, "PAY-000") for index in range(501)]}
    http = _EbayHttp(payouts, transactions)
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position: dict[str, Any] = {"last_sync_at": None, "continuation_cursor": None}
    records: list[dict[str, Any]] = []

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return position["last_sync_at"], position["continuation_cursor"]

    async def record_outcome(*args: Any, **kwargs: Any) -> None:
        position["last_sync_at"] = kwargs["last_sync_at"]
        position["continuation_cursor"] = kwargs["continuation_cursor"]
        records.append(dict(kwargs["outcome_summary"]))

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    async def fake_entity(*args: Any, **kwargs: Any):
        return entity_id

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", fake_entity)
    monkeypatch.setattr(ebay_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(ebay_sync, "_record_sync_outcome", record_outcome)
    monkeypatch.setattr(ebay_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(ebay_sync.EbayFinancesClient, "from_credential", classmethod(lambda cls, blob: client))
    monkeypatch.setattr(ebay_sync, "_utcnow", lambda: RUN_END)

    await ebay_sync._sync_one_workspace(workspace_id)
    await ebay_sync._sync_one_workspace(workspace_id)

    external_ids = [entry["source_external_id"] for entry in session.posted_entries]
    expected = {f"PAY-{index:03d}" for index in range(201)} | {
        f"TXN-PAY-000-{index:03d}" for index in range(501)
    }
    assert set(external_ids) == expected
    assert len(external_ids) == len(expected)
    assert position["continuation_cursor"] is None
    assert position["last_sync_at"] == RUN_END
    assert records[-1]["last_outcome"] == "ok"


@pytest.mark.asyncio
async def test_sync_stops_at_failed_mapping_and_keeps_last_good_position(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(index) for index in range(2)]
    http = _EbayHttp(payouts, {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position: dict[str, Any] = {"last_sync_at": None, "continuation_cursor": None}

    async def load_credential(*args: Any, **kwargs: Any) -> EbayCredentialBlob:
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return position["last_sync_at"], position["continuation_cursor"]

    async def record_outcome(*args: Any, **kwargs: Any) -> None:
        position["last_sync_at"] = kwargs["last_sync_at"]
        position["continuation_cursor"] = kwargs["continuation_cursor"]

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    seen: list[str] = []

    async def failing_mapper(session: Any, *, txn: dict[str, Any], **kwargs: Any):
        seen.append(txn["transactionId"])
        if txn["transactionId"] == "PAY-001":
            raise RuntimeError("fictional mapper failure")
        return SimpleNamespace(outcome="posted", fee_journal_entry_id=None)

    async def fake_entity(*args: Any, **kwargs: Any):
        return entity_id

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", fake_entity)
    monkeypatch.setattr(ebay_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(ebay_sync, "_record_sync_outcome", record_outcome)
    monkeypatch.setattr(ebay_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(ebay_sync, "map_and_post_transaction", failing_mapper)
    monkeypatch.setattr(ebay_sync.EbayFinancesClient, "from_credential", classmethod(lambda cls, blob: client))
    monkeypatch.setattr(ebay_sync, "_utcnow", lambda: RUN_END)

    summary = await ebay_sync._sync_one_workspace(workspace_id)

    assert seen == ["PAY-000", "PAY-001"]
    assert summary["last_outcome"] == "processing_error"
    assert position["last_sync_at"] is None
    assert position["continuation_cursor"]["payouts"]["offset"] == 1


@pytest.mark.asyncio
async def test_sync_below_cap_advances_time_cursor_and_clears_continuation(monkeypatch):
    workspace_id = uuid4()
    entity_id = uuid4()
    payouts = [_payout(0)]
    http = _EbayHttp(payouts, {})
    client = EbayFinancesClient(blob=_blob(workspace_id, entity_id), _http_factory=lambda: http)
    session = _StubSession(accounts=_ACCOUNTS, existing_posts=31)
    position: dict[str, Any] = {"last_sync_at": None, "continuation_cursor": None}

    async def load_credential(*args: Any, **kwargs: Any):
        return client.blob

    async def load_position(*args: Any, **kwargs: Any):
        return position["last_sync_at"], position["continuation_cursor"]

    async def record_outcome(*args: Any, **kwargs: Any):
        position["last_sync_at"] = kwargs["last_sync_at"]
        position["continuation_cursor"] = kwargs["continuation_cursor"]

    @asynccontextmanager
    async def fake_workspace_session(*args: Any, **kwargs: Any):
        yield session

    async def fake_entity(*args: Any, **kwargs: Any):
        return entity_id

    async def fake_reconcile(*args: Any, **kwargs: Any) -> dict[str, int]:
        return {"checked_payouts": 0, "matched": 0, "unmatched": 0}

    monkeypatch.setattr(ebay_sync, "load_ebay_credential", load_credential)
    monkeypatch.setattr(ebay_sync, "_entity_for_credential", fake_entity)
    monkeypatch.setattr(ebay_sync, "_load_sync_position", load_position)
    monkeypatch.setattr(ebay_sync, "_record_sync_outcome", record_outcome)
    monkeypatch.setattr(ebay_sync, "workspace_session", fake_workspace_session)
    monkeypatch.setattr(ebay_sync, "reconcile_one_entity", fake_reconcile)
    monkeypatch.setattr(ebay_sync.EbayFinancesClient, "from_credential", classmethod(lambda cls, blob: client))
    monkeypatch.setattr(ebay_sync, "_utcnow", lambda: RUN_END)

    summary = await ebay_sync._sync_one_workspace(workspace_id)

    assert summary["last_outcome"] == "ok"
    assert position["last_sync_at"] == RUN_END
    assert position["continuation_cursor"] is None
